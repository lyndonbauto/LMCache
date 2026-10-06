# SPDX-License-Identifier: Apache-2.0
"""Tables for breakdown6.sh (Sriram, 2026-10-06 14:00 PT).

Reads ``<breakdown6-dir>/<config>/`` (``stats_lines.txt``, ``clean/`` and ``bt/``
runs with ``times.txt`` and ``kvl.txt``, ``clean/mpstat.txt``,
``bt/bpftrace.json``) and prints, per config:

- GiB/s (``kvlayers`` over 25 s, and the mean of the stats lines inside the
  clean run's 10 s window), and 3 steady stats lines from that window.
- ``rxe_requester`` exits per GiB from the bt run (GiB = ``sent_packet`` /
  262,144) and as % of exits.
- mpstat CPUs busy (10 s average, summed over CPUs).

Usage: breakdown6_report.py <breakdown6-dir>
"""

# Standard
from datetime import datetime, timezone
from pathlib import Path
import re
import statistics
import sys

# First Party
from breakdown4_report import STATS_T_RE
from breakdown5_report import EXITS, PKTS_PER_GIB, bpftrace_maps

STREAM_RE = re.compile(r"^stream: .*?, ([\d.]+) GiB/s, failed rows (\d+)", re.M)
FIELD_RES = {
    "in flight": re.compile(r"in flight ([\d.]+) writes"),
    "placer-wait": re.compile(r"placer-wait (\d+)"),
    "wire": re.compile(r"wire (\d+)"),
    "busy qps": re.compile(r"busy qps ([\d.]+ of [\d.]+)"),
    "placer queue": re.compile(r"placer queue ([\d.]+)"),
}


def _times(run: Path) -> tuple[float, float]:
    """Window start and end (epoch seconds) from ``times.txt``."""
    t: dict[str, float] = {}
    for line in (run / "times.txt").read_text().splitlines():
        name, value = line.split()
        t[name] = float(value)
    return t["start"], t["end"]


def window_lines(cfg: Path, run: Path) -> list[tuple[float, str]]:
    """Stats lines inside a run's window.

    Args:
        cfg: The config directory (for ``stats_lines.txt``).
        run: The run directory (for ``times.txt``).

    Returns:
        ``(GiB/s, line)`` per stats line stamped inside the window.
    """
    a, b = _times(run)
    out: list[tuple[float, str]] = []
    path = cfg / "stats_lines.txt"
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = STATS_T_RE.match(line)
        if not m:
            continue
        stamp = datetime.strptime(m.group(1), "%b %d %Y %H:%M:%S")
        ts = stamp.replace(tzinfo=timezone.utc).timestamp()
        if a + 0.5 < ts <= b + 0.5:
            out.append((float(m.group(2)), line))
    return out


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


def _field(line: str, name: str) -> str:
    m = FIELD_RES[name].search(line)
    return m.group(1) if m else "-"


def main() -> None:
    """Print the tables for the breakdown6 directory in ``sys.argv[1]``."""
    root = Path(sys.argv[1])
    cfgs = sorted(
        (p for p in root.iterdir() if (p / "clean" / "times.txt").exists()),
        key=lambda p: p.stat().st_mtime,
    )
    print("## 1. Rate and steady stats (clean run)\n")
    print(
        "| config | kvlayers GiB/s (25 s) | stats GiB/s (10 s) | in flight | "
        "placer-wait us | wire us | busy qps | placer queue | CPUs busy |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    lines_out: list[str] = []
    for cfg in cfgs:
        m = STREAM_RE.search((cfg / "clean" / "kvl.txt").read_text(errors="replace"))
        rate = f"{float(m.group(1)):.2f}" if m else "-"
        rows = window_lines(cfg, cfg / "clean")
        steady = sorted(rows, key=lambda r: -r[0])[:3]
        mean = statistics.mean(r[0] for r in rows) if rows else 0.0
        mid = steady[0][1] if steady else ""
        cells = [_field(mid, k) for k in FIELD_RES]
        busy = _mpstat_busy(cfg / "clean" / "mpstat.txt")
        print(
            f"| {cfg.name} | {rate} | {mean:.2f} | "
            + " | ".join(cells)
            + f" | {busy:.1f} |"
        )
        lines_out.append(f"{cfg.name}:")
        lines_out.extend("  " + r[1].split("kv-sink: stats: ")[-1] for r in steady)
    print("\nSteady stats lines (the 3 highest-rate lines in the window):\n")
    print("```")
    print("\n".join(lines_out))
    print("```")
    print("\n## 2. rxe_requester exits (bt run, 10 s)\n")
    bt = [c for c in cfgs if (c / "bt" / "bpftrace.json").exists()]
    maps = {c.name: bpftrace_maps(c / "bt" / "bpftrace.json") for c in bt}
    gib = {
        k: v.get("exit", {}).get("sent_packet", 0) / PKTS_PER_GIB
        for k, v in maps.items()
    }
    print("| exit | " + " | ".join(f"{c.name} per GiB | %" for c in bt) + " |")
    print("|---|" + "---|---|" * len(bt))
    for e in EXITS:
        cells = []
        for c in bt:
            exits = maps[c.name].get("exit", {})
            total = sum(exits.values()) or 1
            n = exits.get(e, 0)
            per = n / gib[c.name] if gib[c.name] else 0.0
            cells.append(f"{per:.0f} | {100 * n / total:.2f}")
        print(f"| {e} | " + " | ".join(cells) + " |")
    print(
        "| GiB/s (bt window) | "
        + " | ".join(f"{gib[c.name] / 10:.2f} | " for c in bt)
        + " |"
    )


if __name__ == "__main__":
    main()
