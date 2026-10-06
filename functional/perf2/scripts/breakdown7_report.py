# SPDX-License-Identifier: Apache-2.0
"""Off-CPU tables for breakdown7.sh (Sriram, 2026-10-06 15:29 PT).

Subcommands (run on the box by ``breakdown7.sh``, except ``report``):

- ``roles <script.txt>...``: ``tid role`` lines from ``perf script`` output of
  ``sched:sched_switch`` with call graphs. A thread is a ``placer`` if any of
  its stacks has ``run_placer``, else a ``poller`` (``run_poller``), else a
  ``service`` thread (``thr_tsvc``, ``epoll_wait`` or ``ep_poll``).
- ``stacks <tid_roles.txt>``: markdown, the top 5 switch-out stacks per role by
  count, from ``perf script`` text on stdin (the dwarf capture: user frames).
- ``sched <cfg-dir> <timehist.txt> <sched_script.txt>``: markdown, per role
  off-CPU time, sleeps per write, wakeup -> run delay, wakers, and the top 5
  blocking stacks by total sleep time. Sleep lengths come from ``perf sched
  timehist -w -g --state``. A switch-out at time T in state S or D lasts until
  the thread's next timehist line, whose ``wait time`` (switch-out to
  switch-in, including ``sch delay``, the wakeup -> run part) is the time off
  CPU. The stack for the switch-out at T comes from ``perf script`` of the
  same ``sched.data``, joined on (tid, T).
- ``report <breakdown7-dir>``: the per-config markdown, concatenated.
"""

# Standard
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, TextIO
import re
import sys

ROLES = ("placer", "poller", "service", "other")
SLEEP_STATES = ("S", "D", "I")
HEADER_RE = re.compile(r"^(\S.*?)\s+(\d+)\s+\[(\d+)\]\s+(\d+\.\d+):\s+(.*)$")
PREV_STATE_RE = re.compile(r"\[\d+\] (\S+) ==> ")
FRAME_RE = re.compile(r"^\s+([0-9a-f]+)\s+(.*)$")
SWITCH_RE = re.compile(
    r"^\s*(\d+\.\d+)\s+\[(\d+)\]\s+(\S+?)\[(\d+)(?:/\d+)?\]\s+"
    r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\S+)\s*(.*)$"
)
WAKE_RE = re.compile(
    r"^\s*(\d+\.\d+)\s+\[(\d+)\]\s+(\S+?)(?:\[(\d+)(?:/\d+)?\])?\s+"
    r"awakened:\s+(\S+?)\[(\d+)(?:/\d+)?\]"
)
STATS_RE = re.compile(r"^(\w{3} \d\d \d{4} \d\d:\d\d:\d\d) GMT: .*stats: (\d+) writes")
STREAM_RE = re.compile(r"^stream: .*$", re.M)
SKIP_FRAMES = {
    "__schedule",
    "schedule",
    "x64_sys_call",
    "do_syscall_64",
    "entry_SYSCALL_64_after_hwframe",
    "[unknown]",
}


@dataclass
class Switch:
    """One ``sched_switch`` record from ``perf script``.

    Attributes:
        tid: The thread switched out.
        time: The timestamp as printed (``%.6f`` seconds, perf clock).
        state: The state the thread left in (``S``, ``D``, ``R``, ...).
        kernel: Kernel frames, innermost first.
        user: User frames, innermost first.
    """

    tid: int
    time: str
    state: str
    kernel: list[str] = field(default_factory=list)
    user: list[str] = field(default_factory=list)


@dataclass
class RoleStats:
    """Per-role aggregates over the timehist window.

    Attributes:
        threads: Thread ids with this role seen in the window.
        off_ms: Total time off CPU (sleeps and preemptions), ms.
        sleeps: Switch-outs in a sleep state with a following run.
        sleep_ms: Total time asleep (timehist wait time), ms.
        preempts: Switch-outs in state R with a following run.
        preempt_ms: Total time preempted, ms.
        wake_delays: Wakeup -> run delays after sleeps, ms.
        stacks: Sleep time (ms) and count per blocking stack.
        wakers: Wakeups of this role's threads, per waker label.
        same_cpu: Wakeups where the thread then ran on the waker's CPU.
        woken: Wakeups resolved to a following run.
    """

    threads: set[int] = field(default_factory=set)
    off_ms: float = 0.0
    sleeps: int = 0
    sleep_ms: float = 0.0
    preempts: int = 0
    preempt_ms: float = 0.0
    wake_delays: list[float] = field(default_factory=list)
    stacks: dict[str, list[float]] = field(default_factory=dict)
    wakers: Counter[str] = field(default_factory=Counter)
    same_cpu: Counter[str] = field(default_factory=Counter)
    woken: int = 0


def classify(frames: Iterable[str]) -> str:
    """Role of a stack: ``placer``, ``poller``, ``service`` or ``other``.

    Args:
        frames: Symbol names (kernel and user, any order).

    Returns:
        The first matching role in priority order, else ``other``. ``place``
        (placer-only) also marks a placer: with the queue never empty, placer
        stacks rarely reach ``cf_queue_pop``, and the 8 KiB dwarf stack copy
        can end before ``run_placer``.
    """
    names = {f.removesuffix(" (inlined)") for f in frames}
    if names & {"run_placer", "place"}:
        return "placer"
    if "run_poller" in names:
        return "poller"
    if names & {"thr_tsvc", "epoll_wait", "ep_poll"}:
        return "service"
    return "other"


def parse_switches(lines: Iterable[str]) -> Iterator[Switch]:
    """``sched_switch`` records from ``perf script`` text with call graphs.

    Records of other events (wakeups, migrations) are skipped.

    Args:
        lines: ``perf script -F comm,tid,cpu,time[,event],trace,ip,sym`` lines.

    Yields:
        One ``Switch`` per ``sched_switch`` record, in input order.
    """
    cur: Switch | None = None
    for line in lines:
        line = line.rstrip("\n")
        h = HEADER_RE.match(line)
        if h:
            if cur:
                yield cur
            m = PREV_STATE_RE.search(h.group(5)) if " ==> " in h.group(5) else None
            cur = Switch(int(h.group(2)), h.group(4), m.group(1)) if m else None
            continue
        f = FRAME_RE.match(line)
        if f and cur:
            sym = f.group(2).split("+0x")[0].strip()
            (cur.kernel if f.group(1).startswith("ffff") else cur.user).append(sym)
    if cur:
        yield cur


def stack_key(sw: Switch) -> str:
    """Compact one-line stack: up to 5 kernel then 8 user frames, innermost first.

    Scheduler and syscall-entry frames and ``[unknown]`` are dropped.
    """
    k = [s for s in sw.kernel if s not in SKIP_FRAMES][:5]
    u = [s for s in sw.user if s not in SKIP_FRAMES][:8]
    return f"k: {' <- '.join(k) or '-'} | u: {' <- '.join(u) or '-'}"


def read_roles(path: Path) -> dict[int, str]:
    """``tid -> role`` from a ``tid_roles.txt`` written by ``roles``."""
    out: dict[int, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            tid, role = line.split()
            out[int(tid)] = role
    return out


def _pct(values: list[float], q: float) -> float:
    """The ``q`` quantile (0..1) by nearest rank; 0.0 for no values."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def _waker_label(comm: str, tid: int, roles: dict[int, str]) -> str:
    """``asd:<role>`` for asd threads, else the comm up to the first ``/:-``."""
    if comm == "asd":
        return f"asd:{roles.get(tid, 'other')}"
    return re.split(r"[/:\-]", comm)[0] or comm


def _times(cfg: Path) -> dict[str, float]:
    """Named epoch timestamps from ``times.txt``."""
    out: dict[str, float] = {}
    for line in (cfg / "times.txt").read_text().splitlines():
        name, value = line.split()
        out[name] = float(value)
    return out


def writes_per_s(cfg: Path) -> float:
    """Mean writes/s over the 1 s stats lines inside the sched window.

    Args:
        cfg: Config directory with ``times.txt`` and ``stats_lines.txt``.

    Returns:
        The mean, or 0.0 if no stats line falls inside the window.
    """
    t = _times(cfg)
    counts: list[int] = []
    path = cfg / "stats_lines.txt"
    if not path.exists():
        return 0.0
    for line in path.read_text(errors="replace").splitlines():
        m = STATS_RE.match(line)
        if not m:
            continue
        stamp = datetime.strptime(m.group(1), "%b %d %Y %H:%M:%S")
        ts = stamp.replace(tzinfo=timezone.utc).timestamp()
        if t["sched_start"] + 0.5 < ts <= t["sched_end"] + 0.5:
            counts.append(int(m.group(2)))
    return sum(counts) / len(counts) if counts else 0.0


def cmd_roles(paths: list[str], out: TextIO) -> None:
    """Write ``tid role`` lines for every classified thread in ``paths``."""
    rank = {r: i for i, r in enumerate(ROLES)}
    best: dict[int, str] = {}
    for p in paths:
        with open(p, errors="replace") as fh:
            for sw in parse_switches(fh):
                role = classify(sw.kernel + sw.user)
                if role != "other" and rank[role] < rank[best.get(sw.tid, "other")]:
                    best[sw.tid] = role
    for tid in sorted(best):
        out.write(f"{tid} {best[tid]}\n")


def cmd_stacks(roles_path: Path, src: TextIO, out: TextIO) -> None:
    """Write the top 5 switch-out stacks per role by count (markdown)."""
    roles = read_roles(roles_path)
    counts: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for sw in parse_switches(src):
        role = roles.get(sw.tid) or classify(sw.kernel + sw.user)
        counts[role][(sw.state, stack_key(sw))] += 1
    out.write("Top switch-out stacks by count (dwarf capture, 1 s, all states):\n\n")
    out.write("| role | count | % of role | state | stack |\n|---|---|---|---|---|\n")
    for role in ROLES:
        total = sum(counts[role].values())
        for (state, key), n in counts[role].most_common(5):
            out.write(f"| {role} | {n} | {100 * n / total:.1f} | {state} | `{key}` |\n")
    out.write("\n")


def aggregate(
    timehist: TextIO, script: TextIO, roles: dict[int, str]
) -> tuple[dict[str, RoleStats], float]:
    """Per-role off-CPU aggregates from timehist and the joined stacks.

    Args:
        timehist: ``perf sched timehist -w -g --state -p <asd>`` output.
        script: ``perf script`` of the same ``sched.data`` (asd pid only).
        roles: ``tid -> role`` from the off-CPU captures; threads missing from
            it are classified from their sched.data stacks, else ``other``.

    Returns:
        ``(stats per role, window length in seconds)``.
    """
    stacks: dict[tuple[int, str], str] = {}
    roles = dict(roles)
    for sw in parse_switches(script):
        stacks[(sw.tid, sw.time)] = stack_key(sw)
        if sw.tid not in roles:
            role = classify(sw.kernel + sw.user)
            if role != "other":
                roles[sw.tid] = role
    stats = {r: RoleStats() for r in ROLES}
    prev: dict[int, tuple[str, str]] = {}
    pending: dict[int, tuple[str, int]] = {}
    first = last = 0.0
    for line in timehist:
        w = WAKE_RE.match(line)
        if w:
            if w.group(5) == "asd":
                tid = int(w.group(4)) if w.group(4) else 0
                label = _waker_label(w.group(3), tid, roles)
                pending[int(w.group(6))] = (label, int(w.group(2)))
            continue
        m = SWITCH_RE.match(line)
        if not m or m.group(3) != "asd":
            continue
        t, cpu, tid = float(m.group(1)), int(m.group(2)), int(m.group(4))
        wait, sch = float(m.group(5)), float(m.group(6))
        first = first or t
        last = t
        role = roles.get(tid, "other")
        rs = stats[role]
        rs.threads.add(tid)
        if tid in prev:
            state, key = prev[tid]
            off = wait
            rs.off_ms += off
            if state in SLEEP_STATES:
                rs.sleeps += 1
                rs.sleep_ms += off
                rs.wake_delays.append(sch)
                acc = rs.stacks.setdefault(key, [0.0, 0.0])
                acc[0] += off
                acc[1] += 1
            else:
                rs.preempts += 1
                rs.preempt_ms += off
        if tid in pending:
            label, wcpu = pending.pop(tid)
            rs.woken += 1
            rs.wakers[label] += 1
            rs.same_cpu[label] += int(wcpu == cpu)
        key = stacks.get((tid, m.group(1)), "(no stack)")
        prev[tid] = (m.group(8), key)
    return stats, max(last - first, 1e-9)


def cmd_sched(cfg: Path, timehist: Path, script: Path, out: TextIO) -> None:
    """Write the per-role tables for one config (markdown)."""
    roles = read_roles(cfg / "tid_roles.txt")
    with open(timehist, errors="replace") as th, open(script, errors="replace") as sc:
        stats, win = aggregate(th, sc, roles)
    wps = writes_per_s(cfg)
    out.write(
        f"sched window {win:.2f} s, {wps:.0f} writes/s (stats lines in the "
        "window). Off-CPU = sleeps + preemptions; per thread = role total / "
        "threads.\n\n"
    )
    out.write(
        "| role | threads | off-CPU s/s (total) | per thread | sleeps/s | "
        "sleeps per write | mean sleep ms | wake->run p50 / p99 ms | "
        "preempts/s | preempted s/s |\n|---|---|---|---|---|---|---|---|---|---|\n"
    )
    for role in ROLES:
        rs = stats[role]
        n = len(rs.threads)
        if not n:
            continue
        off = rs.off_ms / 1000 / win
        per_write = rs.sleeps / win / wps if wps else 0.0
        mean = rs.sleep_ms / rs.sleeps if rs.sleeps else 0.0
        out.write(
            f"| {role} | {n} | {off:.2f} | {off / n:.3f} | {rs.sleeps / win:.0f} | "
            f"{per_write:.2f} | {mean:.3f} | {_pct(rs.wake_delays, 0.5):.3f} / "
            f"{_pct(rs.wake_delays, 0.99):.3f} | {rs.preempts / win:.0f} | "
            f"{rs.preempt_ms / 1000 / win:.3f} |\n"
        )
    out.write(
        "\nWakers (who woke each role, and whether it then ran on the waker's CPU):\n\n"
    )
    out.write("| role | waker | wakeups/s | % of role wakeups | same CPU % |\n")
    out.write("|---|---|---|---|---|\n")
    for role in ROLES:
        rs = stats[role]
        for label, n in rs.wakers.most_common(4):
            out.write(
                f"| {role} | {label} | {n / win:.0f} | {100 * n / rs.woken:.1f} | "
                f"{100 * rs.same_cpu[label] / n:.1f} |\n"
            )
    out.write(
        "\nTop 5 blocking stacks per role by total sleep time (sched.data, "
        "frame pointers; placer user frames stop in libc):\n\n"
    )
    out.write("| role | sleep s/s | % of role sleep | sleeps/s | mean ms | stack |\n")
    out.write("|---|---|---|---|---|---|\n")
    for role in ROLES:
        rs = stats[role]
        top = sorted(rs.stacks.items(), key=lambda kv: -kv[1][0])[:5]
        for key, (ms, cnt) in top:
            share = 100 * ms / (rs.sleep_ms or 1)
            out.write(
                f"| {role} | {ms / 1000 / win:.3f} | {share:.1f} "
                f"| {cnt / win:.0f} | {ms / cnt:.3f} | `{key}` |\n"
            )
    out.write("\n")


def cmd_report(root: Path, out: TextIO) -> None:
    """Concatenate the per-config markdown under ``root``."""
    cfgs = sorted(
        (p for p in root.iterdir() if (p / "tables.md").exists()),
        key=lambda p: p.stat().st_mtime,
    )
    for cfg in cfgs:
        kvl = cfg / "kvl.txt"
        m = STREAM_RE.search(kvl.read_text(errors="replace")) if kvl.exists() else None
        out.write(f"## {cfg.name}\n\n`kvlayers` {m.group(0) if m else '-'}\n\n")
        out.write((cfg / "tables.md").read_text())
        stacks = cfg / "dwarf_stacks.md"
        if stacks.exists():
            out.write(stacks.read_text())


def main() -> None:
    """Dispatch on ``sys.argv[1]``; see the module docstring."""
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "roles":
        cmd_roles(args, sys.stdout)
    elif cmd == "stacks":
        cmd_stacks(Path(args[0]), sys.stdin, sys.stdout)
    elif cmd == "sched":
        cmd_sched(Path(args[0]), Path(args[1]), Path(args[2]), sys.stdout)
    elif cmd == "report":
        cmd_report(Path(args[0]), sys.stdout)
    else:
        raise ValueError(f"unknown subcommand {cmd}")


if __name__ == "__main__":
    main()
