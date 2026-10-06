# SPDX-License-Identifier: Apache-2.0
"""Tables for the day-2 round-2 breakdown (breakdown3.sh), server 0c9703931.

Run 3: ib_write_bw -s 524288 -t 2 at 16, 24, 32 and 64 QPs.
Runs 1, 2 and 5: kvlayers' ``stream:`` rate and the median of the run's 3
highest-rate stats lines (0c9703931's format, with ``busy qps X of Y``).
Run 5: Soft-RoCE workers (every ``kworker``) in the 5 s profile: samples
(``-F 999``, about 1 ms each), cores busy (samples / 5000), and ms per GiB
landed in the window: the 5 one-second stats lines after the profile's start
mark (the end mark is written after perf report, so it is too late); plus
mpstat's busy CPUs.
Run 4: lw 8k c=1 fetch rate from the E3 per-layer timeline.

Usage: breakdown3_report.py <breakdown3-dir>
"""

# Standard
from pathlib import Path
import re
import statistics
import sys

# Local
from breakdown2_report import kind, perf_rows, perftest_gib_s
from breakdown_report import read_marks, timeline_gib

SAMPLE_MS = 1000 / 999
PROFILE_S = 5
STATS3_RE = re.compile(
    r"kv-sink: stats: (?P<writes>\d+) writes (?P<gib>[\d.]+) GiB/s"
    r" failed (?P<failed>\d+)"
    r" \| us/write queued (?P<queued>\d+) placer-wait (?P<pwait>\d+) read (?P<read>\d+)"
    r" copy (?P<copy>\d+) post (?P<post>\d+) wire (?P<wire>\d+) reply (?P<reply>\d+)"
    r" \| in flight (?P<ifw>[\d.]+) writes (?P<ifm>[\d.]+) MiB,"
    r" busy qps (?P<busy>[\d.]+) of (?P<qps>[\d.]+),"
    r" placer queue (?P<pq>[\d.]+), starved (?P<starved>\d+)%"
)
STREAM_RE = re.compile(r"^stream: .* ([\d.]+) GiB/s", re.M)
KVL_RUNS = [
    ("1", "R1_qp16_1", "kvl_qp16_1.txt"),
    ("1", "R1_qp16_2", "kvl_qp16_2.txt"),
    ("2", "R2_qp24", "kvl_qp24.txt"),
    ("2", "R2_qp32", "kvl_qp32.txt"),
    ("5", "R5", "kvl_qp16_R5.txt"),
]


def stats_rows(step: Path, start: str, end: str) -> list[dict[str, float]]:
    """The 0c9703931 stats lines between two marks.

    Args:
        step: Directory with marks.txt and the server log.
        start: Mark name at the slice start.
        end: Mark name at the slice end.

    Returns:
        One dict of stats fields per line, in log order.
    """
    marks = read_marks(step)
    lines = (step / "asd-kvsink-bp-perf.log").read_text(errors="replace")
    out = []
    for line in lines.splitlines()[marks[start] : marks[end]]:
        m = STATS3_RE.search(line)
        if m:
            out.append({k: float(v) for k, v in m.groupdict().items()})
    return out


def mpstat_busy(path: Path) -> float:
    """Average busy CPUs over mpstat's per-CPU lines.

    Args:
        path: ``mpstat -P ALL 1 5`` output.

    Returns:
        Sum over CPUs of (100 - %idle) / 100 from the ``Average:`` block.
    """
    busy = 0.0
    for line in path.read_text(errors="replace").splitlines():
        f = line.split()
        if len(f) > 2 and f[0] == "Average:" and f[1].isdigit():
            busy += (100.0 - float(f[-1])) / 100.0
    return busy


def main(root: str) -> None:
    """Print the run 3, runs 1/2/5, run 5 CPU and run 4 tables.

    Args:
        root: The breakdown3 directory.
    """
    base = Path(root)
    print("## Run 3: ib_write_bw -s 524288 -t 2\n")
    print("| QPs | GiB/s |")
    print("|---|---|")
    raw = {}
    for q in (16, 24, 32, 64):
        raw[q] = perftest_gib_s(base / "raw" / f"q{q}_t2_client.txt")
        print(f"| {q} | {raw[q]:.2f} |")
    mem = base / "mem"
    print("\n## Runs 1, 2, 5: kvlayers, memory namespace, server defaults\n")
    print(
        "| run | QPs | stream GiB/s | of raw | stats GiB/s | busy qps | in flight "
        "| placer queue | placer-wait | copy | wire (us/write) | starved |"
    )
    print("|" + "---|" * 12)
    for num, run, out in KVL_RUNS:
        m = STREAM_RE.search((mem / out).read_text(errors="replace"))
        stream = float(m.group(1)) if m else 0.0
        top = sorted(
            stats_rows(mem, f"{run}_start", f"{run}_end"),
            key=lambda r: r["gib"],
            reverse=True,
        )[:3]
        if not top:
            print(f"| {num} | - | {stream:.2f} | no stats |" + " |" * 8)
            continue

        def med(field: str, rows: list[dict[str, float]] = top) -> float:
            return statistics.median(r[field] for r in rows)

        q = int(med("qps"))
        frac = f"{stream / raw[q]:.0%}" if raw.get(q) else "-"
        print(
            f"| {num} | {q} | {stream:.2f} | {frac} | {med('gib'):.2f} "
            f"| {med('busy'):.1f} of {q} | {med('ifw'):.0f} | {med('pq'):.1f} "
            f"| {med('pwait'):.0f} | {med('copy'):.0f} | {med('wire'):.0f} "
            f"| {med('starved'):.0f}% |"
        )
    window = stats_rows(mem, "R5_prof_start", "R5_end")[:PROFILE_S]
    gib = sum(r["gib"] for r in window)
    kinds: dict[str, int] = {}
    for n, comm, _dso, _sym in perf_rows(mem / "R5" / "perf_comm_dso.txt"):
        kinds[kind(comm)] = kinds.get(kind(comm), 0) + n
    rxe = kinds.get("kworker", 0)
    print("\n## Run 5: CPU during kvlayers --qps 16\n")
    print(f"- GiB landed in the window: {gib:.1f}")
    print(
        f"- rxe workers: {rxe} samples, "
        f"{rxe * SAMPLE_MS / (PROFILE_S * 1000):.1f} cores busy, "
        f"{rxe * SAMPLE_MS / gib if gib else 0.0:.0f} ms/GiB"
    )
    print(f"- mpstat: {mpstat_busy(mem / 'R5' / 'mpstat.txt'):.1f} CPUs busy")
    print("\n| thread kind | samples | ms/GiB |")
    print("|---|---|---|")
    for k, n in sorted(kinds.items(), key=lambda x: -x[1])[:8]:
        print(f"| {k} | {n} | {n * SAMPLE_MS / gib if gib else 0.0:.0f} |")
    print("\n## Run 4: lw 8k c=1, 16 QPs (LMCache's maximum)\n")
    print(f"- per-layer timeline: {timeline_gib(mem, '16'):.2f} GiB/s")


if __name__ == "__main__":
    main(sys.argv[1])
