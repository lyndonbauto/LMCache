#!/usr/bin/env python3
"""Task C report: per point, TTFT p50, time between tokens p50, max gap, wall.

Usage: report_c.py <results dir> <session> [<session> ...]
Reads <dir>/<session>/<session>_<point>.json (perf_client.py output).
"""

# Standard
from pathlib import Path
import json
import re
import statistics
import sys


def point_stats(path: Path) -> dict[str, float]:
    """Summarize one point file: TTFT p50 and p90, TBT p50, max gap, wall, errors."""
    d = json.loads(path.read_text())
    ok = [r for r in d["requests"] if not r["error"]]
    ttft = sorted(r["ttft_s"] for r in ok)
    tbt = [(r["total_s"] - r["ttft_s"]) / (r["out_tokens"] - 1) for r in ok if r["out_tokens"] > 1]
    return {
        "ttft50": statistics.median(ttft) if ttft else float("nan"),
        "ttft90": ttft[min(len(ttft) - 1, int(0.9 * len(ttft)))] if ttft else float("nan"),
        "tbt50": statistics.median(tbt) if tbt else float("nan"),
        "gap": max((r["max_gap_s"] for r in ok), default=float("nan")),
        "wall": d["wall_s"],
        "err": len(d["requests"]) - len(ok),
    }


def main() -> None:
    root = Path(sys.argv[1])
    sessions = sys.argv[2:]
    points: dict[str, dict[str, dict[str, float]]] = {}
    for s in sessions:
        for f in (root / s).glob(f"{s}_*.json"):
            name = f.stem[len(s) + 1 :]
            if name.startswith(("store", "warm")):
                continue
            points.setdefault(name, {})[s] = point_stats(f)

    def key(n: str) -> tuple[int, int, int, int, str]:
        m = re.match(r"(?:([PM])(\d+)_)?L(\d+)_c(\d+)(.*)", n)
        if not m:
            return (9, 0, 0, 0, n)
        kind = {None: 0, "P": 1, "M": 2}[m.group(1)]
        return (kind, int(m.group(2) or 0), int(m.group(3)), int(m.group(4)), m.group(5))

    cols = ["ttft50", "tbt50", "gap", "wall"]
    print("point".ljust(24) + "".join(f"| {s:<34}" for s in sessions))
    print(" " * 24 + "".join("| TTFT50  TBT50ms  gap_ms   wall_s  " for _ in sessions))
    for n in sorted(points, key=key):
        row = n.ljust(24)
        for s in sessions:
            st = points[n].get(s)
            if st is None:
                row += "| " + "-".ljust(34)
                continue
            e = f" E{st['err']}" if st["err"] else ""
            row += (
                f"| {st['ttft50']:6.3f} {st['tbt50'] * 1e3:7.1f} {st['gap'] * 1e3:8.0f} "
                f"{st['wall']:8.2f}{e:<4}"
            )
        print(row)
    _ = cols


if __name__ == "__main__":
    main()
