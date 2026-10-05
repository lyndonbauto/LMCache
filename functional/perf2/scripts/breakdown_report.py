# SPDX-License-Identifier: Apache-2.0
"""One row per run of the perf2 breakdown (breakdown.sh).

For each step directory: B rows from kvlayers' ``stream:`` lines, C rows from
the E3 per-layer timeline in the lw session's LMCache log, and for both the
kv-sink server's ``kv-sink: stats:`` lines between the run's marks
(``marks.txt``: ``<name> <time> <log line count>``). The steady-state stats are
the median of the run's 3 highest-rate lines (C's traffic comes in bursts, one
retrieve per request, so its quieter seconds are not steady state).

C's fetch rate: per retrieve (generation), 31 layers of 32 MiB over the time
from layer 0 to layer 31 resident; the median retrieve.

Usage: breakdown_report.py <breakdown-dir> [step ...]
"""

# Standard
from pathlib import Path
import re
import statistics
import sys

LAYER_MIB = 32
STATS_RE = re.compile(
    r"kv-sink: stats: (?P<writes>\d+) writes (?P<gib>[\d.]+) GiB/s"
    r" failed (?P<failed>\d+)"
    r" \| us/write queued (?P<queued>\d+) placer-wait (?P<pwait>\d+) read (?P<read>\d+)"
    r" copy (?P<copy>\d+) post (?P<post>\d+) wire (?P<wire>\d+) reply (?P<reply>\d+)"
    r" \| in flight (?P<ifw>[\d.]+) writes (?P<ifm>[\d.]+) MiB, placer queue"
    r" (?P<pq>[\d.]+), starved (?P<starved>\d+)%"
)
FIELDS = [
    "gib",
    "queued",
    "pwait",
    "read",
    "copy",
    "post",
    "wire",
    "reply",
    "ifw",
    "pq",
    "starved",
]
E3_RE = re.compile(r"E3 layer (\d+) resident \+([\d.]+) ms gen=(\d+)")
STREAM_RE = re.compile(r"^stream: .* ([\d.]+) GiB/s")


def read_marks(step: Path) -> dict[str, int]:
    """Map each mark name to the server log's line count at that mark.

    Args:
        step: A step directory holding marks.txt.

    Returns:
        Mark name to line count.
    """
    marks = {}
    for line in (step / "marks.txt").read_text().splitlines():
        name, _, count = line.split()
        marks[name] = int(count)
    return marks


def steady_stats(log_lines: list[str], start: int, end: int) -> dict[str, float]:
    """Median of the 3 highest-rate stats lines in a log slice.

    Args:
        log_lines: The server log's lines.
        start: First line index of the run.
        end: Line index after the run.

    Returns:
        Field name to median value; empty if the run logged no stats.
    """
    rows = []
    for line in log_lines[start:end]:
        m = STATS_RE.search(line)
        if m:
            rows.append({k: float(v) for k, v in m.groupdict().items()})
    rows = sorted(rows, key=lambda r: r["gib"], reverse=True)[:3]
    if not rows:
        return {}
    return {f: statistics.median(r[f] for r in rows) for f in FIELDS}


def timeline_gib(step: Path, qp: str) -> float:
    """C's fetch rate from the E3 per-layer timeline, median retrieve.

    Args:
        step: The step directory.
        qp: Queue pairs of the C session.

    Returns:
        GiB/s, or 0.0 when the log has no complete timeline.
    """
    logs = list((step / f"E_timeline_qp{qp}").glob("lmcache_*.log"))
    gens: dict[str, dict[int, float]] = {}
    for log in logs:
        for m in E3_RE.finditer(log.read_text(errors="replace")):
            gens.setdefault(m.group(3), {})[int(m.group(1))] = float(m.group(2))
    rates = []
    for layers in gens.values():
        if 0 in layers and 31 in layers and layers[31] > layers[0]:
            rates.append(31 * LAYER_MIB / 1024 / ((layers[31] - layers[0]) / 1000))
    return statistics.median(rates) if rates else 0.0


def main(root: str, steps: list[str]) -> None:
    """Print one markdown row per run.

    Args:
        root: The breakdown directory.
        steps: Step directories to report; all with a marks.txt if empty.
    """
    base = Path(root)
    dirs = [base / s for s in steps] or sorted(
        p.parent for p in base.glob("*/marks.txt")
    )
    print(
        "| step | run | fetch GiB/s | stats GiB/s | queued | placer-wait | read "
        "| copy | post | wire | reply (us/write) | in flight | placer queue "
        "| starved |"
    )
    print("|" + "---|" * 14)
    for step in dirs:
        log_lines = (step / "asd-kvsink-bp-perf.log").read_text().splitlines()
        marks = read_marks(step)
        for name in marks:
            if not name.endswith("_start"):
                continue
            run = name[: -len("_start")]
            end = marks.get(f"{run}_end", len(log_lines))
            st = steady_stats(log_lines, marks[name], end)
            qp = run.rsplit("qp", 1)[1]
            if run.startswith("C_"):
                fetch = timeline_gib(step, qp)
            else:
                suffix = "_E" if run.startswith("E_") else ""
                out = (step / f"kvl_qp{qp}{suffix}.txt").read_text()
                m = next(
                    (
                        STREAM_RE.match(x)
                        for x in out.splitlines()
                        if STREAM_RE.match(x)
                    ),
                    None,
                )
                fetch = float(m.group(1)) if m else 0.0
            if not st:
                print(f"| {step.name} | {run} | {fetch:.2f} | no stats |" + " |" * 10)
                continue
            print(
                f"| {step.name} | {run} | {fetch:.2f} | {st['gib']:.2f} "
                f"| {st['queued']:.0f} | {st['pwait']:.0f} | {st['read']:.0f} "
                f"| {st['copy']:.0f} | {st['post']:.0f} | {st['wire']:.0f} "
                f"| {st['reply']:.0f} | {st['ifw']:.1f} | {st['pq']:.1f} "
                f"| {st['starved']:.0f}% |"
            )


def perf_groups(path: Path, top: int = 15) -> None:
    """Print perf samples summed by thread kind and by (kind, dso, symbol).

    Thread names lose their numbering (``kworker/u46:2-rxe`` -> ``kworker``),
    so per-thread rows of one kind add up.

    Args:
        path: A perf_full.txt (``perf report --sort comm,dso,symbol``).
        top: Rows to print per table.
    """
    by_comm: dict[str, float] = {}
    by_sym: dict[tuple[str, str, str], float] = {}
    for line in path.read_text().splitlines():
        f = line.split()
        if len(f) < 4 or not f[0].endswith("%"):
            continue
        pct = float(f[0].rstrip("%"))
        comm = re.sub(r"[/:].*$|\d+$", "", f[1])
        sym = " ".join(f[4:5]) if f[3] in ("[.]", "[k]") else f[3]
        by_comm[comm] = by_comm.get(comm, 0.0) + pct
        key = (comm, f[2], sym)
        by_sym[key] = by_sym.get(key, 0.0) + pct
    print(f"{path}: total {sum(by_comm.values()):.1f}% of samples")
    for comm, pct in sorted(by_comm.items(), key=lambda x: -x[1])[:top]:
        print(f"  {pct:6.2f}%  {comm}")
    syms = sorted(by_sym.items(), key=lambda x: -x[1])
    for (comm, dso, sym), pct in syms[:top]:
        print(f"  {pct:6.2f}%  {comm}  {dso}  {sym}")


if __name__ == "__main__":
    if sys.argv[1] == "perf":
        for p in sys.argv[2:]:
            perf_groups(Path(p))
    else:
        main(sys.argv[1], sys.argv[2:])
