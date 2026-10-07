# SPDX-License-Identifier: Apache-2.0
"""Tables for breakdown9.sh (Sriram, 2026-10-06 16:39 PT, per-QP depth sweep).

Reads ``<breakdown9-dir>/<nn>-<point>/`` (``point.txt``, ``stats_lines.txt``,
``clean/`` with ``times.txt``, ``kvl.txt`` and ``mpstat.txt``, and the lw
session in ``E_timeline_qp16/``) and prints, per point in session order:

1. ``kvlayers`` GiB/s over 25 s, the mean of the stats lines in the 10 s
   window, fields from the highest-rate stats line, and mpstat CPUs busy.
2. lw 8k c=1 at 16 QPs: TTFT p50 from the point line, and the median layer 0
   and layer 31 residency times after ``begin_fetch`` (E3 timeline).
3. Two steady stats lines per point.

Usage: breakdown9_report.py <breakdown9-dir>
"""

# Standard
from pathlib import Path
import re
import statistics
import sys

# First Party
from breakdown6_report import FIELD_RES, STREAM_RE, window_lines
from breakdown8_report import layer_arrivals

TTFT_RE = re.compile(r"ttft_p50~([\d.]+)s")


def _mpstat_busy(path: Path) -> float:
    """CPUs busy, summed over CPUs (mpstat's Average block); 0.0 if missing."""
    busy = 0.0
    if not path.exists():
        return busy
    for line in path.read_text(errors="replace").splitlines():
        f = line.split()
        if len(f) > 10 and f[0] == "Average:" and f[1].isdigit():
            busy += (100 - float(f[-1])) / 100
    return busy


def _ttft(point: Path) -> str:
    """TTFT p50 (s) from the lw session's point line, or ``-``."""
    for s in (point / "E_timeline_qp16").glob("session_*.txt"):
        m = TTFT_RE.search(s.read_text(errors="replace"))
        if m:
            return m.group(1)
    return "-"


def main() -> None:
    """Print the tables for the breakdown9 directory in ``sys.argv[1]``."""
    root = Path(sys.argv[1])
    points = sorted(p for p in root.iterdir() if (p / "point.txt").exists())
    print(
        "| point | settings | kvlayers GiB/s | stats GiB/s | "
        + " | ".join(FIELD_RES)
        + " | CPUs busy | lw TTFT p50 s | layer 0 ms | layer 31 ms |"
    )
    print("|---|---|---|---|" + "---|" * len(FIELD_RES) + "---|---|---|---|")
    steady_out: list[str] = []
    for p in points:
        kvl = p / "clean" / "kvl.txt"
        m = STREAM_RE.search(kvl.read_text(errors="replace")) if kvl.exists() else None
        rate = f"{float(m.group(1)):.2f}" if m else "-"
        rows = (
            window_lines(p, p / "clean") if (p / "clean" / "times.txt").exists() else []
        )
        mean = statistics.mean(r[0] for r in rows) if rows else 0.0
        steady = sorted(rows, key=lambda r: -r[0])[:2]
        top = steady[0][1] if steady else ""
        fields = []
        for rx in FIELD_RES.values():
            fm = rx.search(top)
            fields.append(fm.group(1) if fm else "-")
        first, last, _ = layer_arrivals(p)
        settings = (p / "point.txt").read_text().strip().replace("KV_SINK_", "")
        print(
            f"| {p.name} | `{settings}` | {rate} | {mean:.2f} | "
            + " | ".join(fields)
            + f" | {_mpstat_busy(p / 'clean' / 'mpstat.txt'):.1f} | {_ttft(p)} | "
            f"{first:.1f} | {last:.1f} |"
        )
        steady_out.append(f"{p.name}:")
        steady_out.extend("  " + r[1].split("kv-sink: stats: ")[-1] for r in steady)
    print("\nSteady stats lines (the 2 highest-rate lines in each 10 s window):\n")
    print("```")
    print("\n".join(steady_out))
    print("```")


if __name__ == "__main__":
    main()
