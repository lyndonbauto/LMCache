# SPDX-License-Identifier: Apache-2.0
"""Parsers and tables for breakdown4.sh (Sriram, 2026-10-06 11:08 PT).

Where Soft-RoCE work runs and waits for perftest (P2: ``-q 16 -t 2``, P8:
``-q 16 -t 8``) and the sink (S: ``kvlayers --qps 16``, S-hot: an 8 MiB working
set). The rxe module on the box is built from Linux v6.11: each QP has a send
task (``rxe_sender``: ``rxe_requester`` then ``rxe_completer``) and a receive
task (``rxe_receiver``, the responder), run inline by the posting thread when
idle (``rxe_run_task``) or as ``do_work`` items on ``rxe_wq``.

Subcommands:

- ``prof`` (stdin: ``perf script -F comm,tid,cpu,ip,sym,dso`` of
  ``perf record -a -g -F 999``): a sample is rxe work if any frame is an
  ``rxe_*`` function, ``do_task`` or ``do_work`` (module symbols resolve under
  ``[kernel.kallsyms]``); its task type is the innermost of ``rxe_rcv`` (packet
  receive), ``rxe_receiver`` / ``rxe_responder`` (responder),
  ``rxe_completer``, ``rxe_requester``, else "rxe other". Counts by type, by
  (type, thread kind), and rxe samples by CPU. JSON on stdout.
- ``wq`` (stdin: ``perf script`` of the workqueue tracepoints, recorded with
  ``-g``): per ``do_work`` run, queue -> start latency and run duration; each
  work item's task type from its ``queue_work`` call chains
  (``rxe_resp_queue_pkt`` -> receive task; ``rxe_comp_queue_pkt``,
  ``rxe_post_send``, ``rxe_sender``, ``rxe_requester`` -> send task). JSON.
- ``sched`` (stdin: ``perf sched latency``): rows summed by thread kind. JSON.
- ``gib <cfg-dir> <source>``: GiB moved in each capture window. JSON.
- ``report <breakdown4-dir>``: the tables.

Thread kinds drop the numbering (``kworker/u46:2-r`` -> ``kworker``), except
that unbound kworkers running ``rxe_wq`` work (``kworker/u<n>:<m>-r...``, or
``+r...`` while busy) are
``kworker-rxe``.

Usage: breakdown4_report.py prof|wq|sched < perf-script-output
       breakdown4_report.py gib <cfg-dir> <server-log | perftest-client-output>
       breakdown4_report.py report <breakdown4-dir>
"""

# Standard
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import statistics
import sys

SAMPLE_MS = 1000 / 999
PROFILE_S = 5
NCPU = 20
CONFIGS = ["P2", "P8", "S", "S-hot"]
WINDOW_S = {"prof": 5.0, "wq": 2.0}
RXE_FUNCS = {"do_task", "do_work"}
HEADER_RE = re.compile(r"^(\S.*?)\s+(\d+)\s+\[(\d+)\]")
FRAME_RE = re.compile(r"^\s+[0-9a-f]+\s+(\S+?)(?:\+0x[0-9a-f]+)?\s+\((.*)\)\s*$")
WQ_RE = re.compile(
    r"^\s*(?P<comm>\S.*?)\s+(?P<tid>\d+)\s+\[(?P<cpu>\d+)\]\s+(?P<t>[\d.]+):\s+"
    r"workqueue:(?P<ev>workqueue_\w+):\s+work struct[= ](?P<work>(?:0x)?[0-9a-f]+)"
    r"(?P<rest>.*)$"
)
SCHED_RE = re.compile(
    r"^\s*(?P<task>\S.*?)\s*\|\s*(?P<run>[\d.]+) ms\s*\|\s*(?P<sw>\d+)\s*\|"
    r"\s*avg:\s*(?P<avg>[\d.]+) ms\s*\|\s*max:\s*(?P<max>[\d.]+) ms"
)
STATS_T_RE = re.compile(
    r"^(\w{3} \d{2} \d{4} \d{2}:\d{2}:\d{2}) GMT: .*kv-sink: stats: "
    r"\d+ writes ([\d.]+) GiB/s"
)
PT_RE = re.compile(r"^ *524288 +\d+ +[\d.]+ +([\d.]+)", re.M)
TYPE_FUNCS = [
    ("receive (rxe_rcv)", ("rxe_rcv", "rxe_udp_encap_recv")),
    ("responder", ("rxe_receiver", "rxe_responder")),
    ("completer", ("rxe_completer",)),
    ("requester", ("rxe_requester",)),
]
SEND_FUNCS = {"rxe_comp_queue_pkt", "rxe_post_send", "rxe_sender", "rxe_requester"}


def kind(comm: str) -> str:
    """Thread kind: the comm without its numbering.

    Args:
        comm: A thread name, for example ``kworker/u46:2-r``.

    Returns:
        ``kworker-rxe`` for unbound kworkers running ``rxe_wq`` work, else the
        name up to its first ``/`` or ``:`` with trailing digits removed.
    """
    if re.match(r"^kworker/u\d+:\d+[-+]r", comm):
        return "kworker-rxe"
    return re.sub(r"[/:].*$|\d+$", "", comm)


def _is_rxe(sym: str) -> bool:
    """Whether a frame is rxe code (module symbols resolve as kallsyms)."""
    return sym.startswith("rxe_") or sym in RXE_FUNCS


def _task_type(frames: list[tuple[str, str]]) -> str:
    """Innermost rxe task function of a call chain (leaf first)."""
    for sym, _dso in frames:
        base = sym.split(".")[0]
        for name, funcs in TYPE_FUNCS:
            if base in funcs:
                return name
    return "rxe other"


def _add(d: dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1


def parse_prof(lines: list[str]) -> dict[str, dict[str, int]]:
    """Count the samples of a ``perf script`` dump with call chains.

    Args:
        lines: The dump's lines.

    Returns:
        ``total`` (thread kind -> samples), ``type`` (task type -> rxe
        samples), ``type_kind`` (``"<type>|<kind>"`` -> rxe samples) and
        ``cpu`` (CPU number -> rxe samples).
    """
    out: dict[str, dict[str, int]] = {
        "total": {},
        "type": {},
        "type_kind": {},
        "cpu": {},
    }
    samples: list[tuple[str, str, list[tuple[str, str]]]] = []
    for line in lines:
        m = HEADER_RE.match(line)
        if m and not line[0].isspace():
            samples.append((m.group(1), str(int(m.group(3))), []))
            continue
        f = FRAME_RE.match(line)
        if f and samples:
            samples[-1][2].append((f.group(1), f.group(2)))
    for comm, cpu, frames in samples:
        k = kind(comm)
        _add(out["total"], k)
        if not any(_is_rxe(s) for s, _dso in frames):
            continue
        t = _task_type(frames)
        _add(out["type"], t)
        _add(out["type_kind"], f"{t}|{k}")
        _add(out["cpu"], cpu)
    return out


def parse_wq(lines: list[str]) -> dict[str, object]:
    """Latencies and durations of ``do_work`` runs from workqueue tracepoints.

    Args:
        lines: ``perf script`` of queue_work / execute_start / execute_end
            (recorded with ``-g``, so queue_work events carry call chains).

    Returns:
        ``lat`` and ``dur``: ``[work pointer, us]`` per run; ``types``: work
        pointer -> task type (``send task`` / ``receive task``); ``t0`` and
        ``t1``: first and last ``do_work`` event times (s).
    """
    pend: dict[str, float] = {}
    run: dict[str, float] = {}
    votes: dict[str, dict[str, int]] = {}
    lat: list[list[object]] = []
    dur: list[list[object]] = []
    t0 = t1 = 0.0
    chains: list[tuple[str, set[str]]] = []
    for line in lines:
        m = WQ_RE.match(line)
        if m:
            if "do_work" not in m["rest"]:
                continue
            ts, w, ev = float(m["t"]), m["work"], m["ev"]
            t0 = t0 or ts
            t1 = ts
            if ev == "workqueue_queue_work":
                pend[w] = ts
                chains.append((w, set()))
            elif ev == "workqueue_execute_start":
                if w in pend:
                    lat.append([w, (ts - pend.pop(w)) * 1e6])
                run[w] = ts
            elif ev == "workqueue_execute_end" and w in run:
                dur.append([w, (ts - run.pop(w)) * 1e6])
            continue
        f = FRAME_RE.match(line)
        if f and chains:
            chains[-1][1].add(f.group(1).split(".")[0])
    for w, syms in chains:
        if "rxe_resp_queue_pkt" in syms:
            _add(votes.setdefault(w, {}), "receive task")
        elif syms & SEND_FUNCS:
            _add(votes.setdefault(w, {}), "send task")
    types = {w: max(v, key=lambda k: v[k]) for w, v in votes.items()}
    return {"lat": lat, "dur": dur, "types": types, "t0": t0, "t1": t1}


def parse_sched(lines: list[str]) -> dict[str, dict[str, float]]:
    """Sum ``perf sched latency`` rows by thread kind.

    Args:
        lines: The command's output.

    Returns:
        Thread kind -> ``runtime_ms``, ``switches``, ``avg_ms`` (the rows'
        average delays weighted by switches) and ``max_ms``.
    """
    out: dict[str, dict[str, float]] = {}
    for line in lines:
        m = SCHED_RE.match(line)
        if not m:
            continue
        k = kind(m["task"].rsplit(":", 1)[0].strip())
        r = out.setdefault(k, {"runtime_ms": 0.0, "switches": 0.0, "wsum": 0.0})
        sw = float(m["sw"])
        r["runtime_ms"] += float(m["run"])
        r["switches"] += sw
        r["wsum"] += float(m["avg"]) * sw
        r["max_ms"] = max(r.get("max_ms", 0.0), float(m["max"]))
    for r in out.values():
        r["avg_ms"] = r.pop("wsum") / r["switches"] if r["switches"] else 0.0
    return out


def window_gib(cfg: Path, source: Path) -> dict[str, float]:
    """GiB moved in each capture window of a config.

    Args:
        cfg: Config directory with times.txt (``<window>_start|_end <epoch>``).
        source: The kv-sink server log (stats lines, one per second; a line
            stamped T covers (T-1, T]) or ib_write_bw's client output (its
            average rate times the window).

    Returns:
        Window name (``prof``, ``wq``) -> GiB.
    """
    times: dict[str, float] = {}
    for line in (cfg / "times.txt").read_text().splitlines():
        name, value = line.split()
        times[name] = float(value)
    text = source.read_text(errors="replace")
    pt = PT_RE.search(text)
    out: dict[str, float] = {}
    for w, seconds in WINDOW_S.items():
        if f"{w}_start" not in times:
            continue
        a, b = times[f"{w}_start"], times[f"{w}_end"]
        if pt:
            out[w] = float(pt.group(1)) * 1e9 / 8 / 2**30 * seconds
            continue
        rates: list[float] = []
        for line in text.splitlines():
            m = STATS_T_RE.match(line)
            if not m:
                continue
            stamp = datetime.strptime(m.group(1), "%b %d %Y %H:%M:%S")
            ts = stamp.replace(tzinfo=timezone.utc).timestamp()
            if a + 0.5 < ts <= b + 0.5:
                rates.append(float(m.group(2)))
        out[w] = statistics.mean(rates) * seconds if rates else 0.0
    return out


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def _mpstat(path: Path) -> tuple[float, float, float]:
    """Busy CPUs, %sys and %soft summed over CPUs (mpstat's Average block)."""
    busy = sys_ = soft = 0.0
    if not path.exists():
        return 0.0, 0.0, 0.0
    for line in path.read_text(errors="replace").splitlines():
        f = line.split()
        if len(f) > 10 and f[0] == "Average:" and f[1].isdigit():
            busy += (100 - float(f[-1])) / 100
            sys_ += float(f[4]) / 100
            soft += float(f[7]) / 100
    return busy, sys_, soft


def _cores(n: int) -> float:
    return n * SAMPLE_MS / (PROFILE_S * 1000)


def _print_prof(root: Path, cfgs: list[str], gib: dict[str, dict[str, float]]) -> None:
    data = {c: json.loads((root / c / "prof.json").read_text()) for c in cfgs}
    print("## 1. rxe work by task type (5 s, -F 999): ms/GiB, cores\n")
    print("GiB in the window: " + ", ".join(f"{c} {gib[c]['prof']:.1f}" for c in cfgs))
    print()
    print("| task type | " + " | ".join(f"{c} ms/GiB | {c} cores" for c in cfgs) + " |")
    print("|---|" + "---|---|" * len(cfgs))
    for t in [n for n, _f in TYPE_FUNCS] + ["rxe other", "all rxe"]:
        cells = []
        for c in cfgs:
            types = data[c]["type"]
            n = sum(types.values()) if t == "all rxe" else types.get(t, 0)
            g = gib[c]["prof"]
            per = f"{n * SAMPLE_MS / g:.0f}" if g else "-"
            cells.append(f"{per} | {_cores(n):.2f}")
        print(f"| {t} | " + " | ".join(cells) + " |")
    print("\n### rxe work by task type and thread kind (cores)\n")
    for c in cfgs:
        tk = sorted(data[c]["type_kind"].items(), key=lambda x: -x[1])[:10]
        parts = [f"{k.replace('|', ' in ')} {_cores(n):.2f}" for k, n in tk]
        print(f"- {c}: " + "; ".join(parts))
    print("\n## 3. rxe work by CPU (cores busy with rxe work)\n")
    print("| config | " + " | ".join(str(i) for i in range(NCPU)) + " | max / mean |")
    print("|---|" + "---|" * (NCPU + 1))
    for c in cfgs:
        cores = [_cores(data[c]["cpu"].get(str(i), 0)) for i in range(NCPU)]
        mean = statistics.mean(cores) or 1.0
        row = " | ".join(f"{x:.2f}" for x in cores)
        print(f"| {c} | {row} | {max(cores) / mean:.1f} |")


def _print_wq(root: Path, cfgs: list[str], gib: dict[str, dict[str, float]]) -> None:
    print("\n## 2. rxe_wq do_work runs (2 s)\n")
    print(
        "| config | task | runs | runs/GiB | queue->start p50 / p99 us "
        "| run p50 / p99 us | running at once | work items |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for c in cfgs:
        p = root / c / "wq.json"
        if not p.exists():
            continue
        wq = json.loads(p.read_text())
        types: dict[str, str] = wq["types"]
        span = (wq["t1"] - wq["t0"]) or 2.0
        g = gib[c].get("wq", 0.0)
        for task in ("all", "send task", "receive task", "unknown"):
            sel = [
                (w, us)
                for w, us in wq["dur"]
                if task == "all" or types.get(w, "unknown") == task
            ]
            lats = [
                us
                for w, us in wq["lat"]
                if task == "all" or types.get(w, "unknown") == task
            ]
            if not sel:
                continue
            durs = [us for _w, us in sel]
            items = len({w for w, _us in sel})
            per = f"{len(durs) / g:.0f}" if g else "-"
            print(
                f"| {c} | {task} | {len(durs)} | {per} | "
                f"{_pct(lats, 50):.0f} / {_pct(lats, 99):.0f} | "
                f"{_pct(durs, 50):.0f} / {_pct(durs, 99):.0f} | "
                f"{sum(durs) / 1e6 / span:.2f} | {items} |"
            )


def report(root: Path) -> None:
    """Print one table per item.

    Args:
        root: The breakdown4 directory (one subdirectory per config).
    """
    cfgs = [c for c in CONFIGS if (root / c / "prof.json").exists()]
    gib = {c: json.loads((root / c / "gib.json").read_text()) for c in cfgs}
    _print_prof(root, cfgs, gib)
    _print_wq(root, cfgs, gib)
    print("\n## 4. perf sched latency (2 s), by thread kind (top 6 by runtime)\n")
    print("| config | kind | runtime ms | switches | avg delay ms | max delay ms |")
    print("|---|---|---|---|---|---|")
    for c in cfgs:
        p = root / c / "sched.json"
        if not p.exists():
            continue
        sc = json.loads(p.read_text())
        for k, r in sorted(sc.items(), key=lambda x: -x[1]["runtime_ms"])[:6]:
            print(
                f"| {c} | {k} | {r['runtime_ms']:.0f} | {r['switches']:.0f} | "
                f"{r['avg_ms']:.3f} | {r['max_ms']:.3f} |"
            )
    print("\n## 5. mpstat (5 s average, summed over CPUs)\n")
    print("| config | busy CPUs | sys | soft |")
    print("|---|---|---|---|")
    for c in cfgs:
        b, s, so = _mpstat(root / c / "mpstat.txt")
        print(f"| {c} | {b:.1f} | {s:.1f} | {so:.1f} |")


def main(argv: list[str]) -> None:
    """Dispatch a subcommand (see the module docstring).

    Args:
        argv: Command-line arguments without the program name.

    Raises:
        ValueError: For an unknown subcommand.
    """
    cmd = argv[0]
    if cmd == "report":
        report(Path(argv[1]))
    elif cmd == "gib":
        print(json.dumps(window_gib(Path(argv[1]), Path(argv[2]))))
    elif cmd in ("prof", "wq", "sched"):
        lines = sys.stdin.read().splitlines()
        parsers = {"prof": parse_prof, "wq": parse_wq, "sched": parse_sched}
        print(json.dumps(parsers[cmd](lines)))
    else:
        raise ValueError(f"unknown subcommand {cmd!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
