# SPDX-License-Identifier: Apache-2.0
"""Tables for breakdown5.sh (Sriram, 2026-10-06 13:19 PT).

Reads ``<breakdown5-dir>/<cfg>-<A|B>/`` (``times.txt``, ``counters_before.txt``,
``counters_after.txt``, ``client.txt`` for perftest, ``bpftrace.json`` for B
runs) and ``S_server/stats_lines.txt``, and prints:

- A: Soft-RoCE counter deltas over the 10 s window, total and per GiB moved.
- B: ``rxe_requester`` exit reasons and timer counts, per GiB and as % of exits.

GiB moved in a counter window = rate x window length: perftest's reported rate,
or for the sink the mean of the server's stats lines inside the window. GiB in
the bpftrace window = ``sent_packet`` exits / 262,144 (each ``rxe_requester``
call that returns 0 sends one 4 KiB data packet; a 512 KiB write is 128).

Usage: breakdown5_report.py <breakdown5-dir>
"""

# Standard
from datetime import datetime, timezone
from pathlib import Path
import json
import statistics
import sys

# First Party
from breakdown4_report import PT_RE, STATS_T_RE

COUNTERS = [
    "sent_pkts",
    "rcvd_pkts",
    "completer_retry_err",
    "retry_exceeded_err",
    "out_of_seq_request",
    "rcvd_seq_err",
    "duplicate_request",
    "ack_deferred",
    "rcvd_rnr_err",
    "send_rnr_err",
]
EXITS = [
    "sent_packet",
    "window_full",
    "rx_backed_up",
    "wait_fence",
    "need_rd_atomic",
    "nothing_or_other",
]
RUNS = ["P8-A", "P8-B", "S-A", "S-B"]
PKTS_PER_GIB = 2**30 // 4096


def _counters(path: Path) -> dict[str, int]:
    """Counters from one ``rdma statistic show link`` line."""
    words = path.read_text().split()
    out: dict[str, int] = {}
    for key, value in zip(words[2::2], words[3::2], strict=False):
        if value.isdigit():
            out[key] = int(value)
    return out


def _times(run: Path) -> tuple[float, float]:
    """Window start and end (epoch seconds) from ``times.txt``."""
    t: dict[str, float] = {}
    for line in (run / "times.txt").read_text().splitlines():
        name, value = line.split()
        t[name] = float(value)
    return t["start"], t["end"]


def window_gib(root: Path, run: Path) -> float:
    """GiB moved in a run's window.

    Args:
        root: The breakdown5 directory (for ``S_server/stats_lines.txt``).
        run: The run directory.

    Returns:
        Rate x window length in GiB; 0.0 if no rate is found.
    """
    a, b = _times(run)
    client = run / "client.txt"
    if client.exists():
        m = PT_RE.search(client.read_text(errors="replace"))
        return float(m.group(1)) * 1e9 / 8 / 2**30 * (b - a) if m else 0.0
    rates: list[float] = []
    for line in (root / "S_server" / "stats_lines.txt").read_text().splitlines():
        m = STATS_T_RE.match(line)
        if not m:
            continue
        stamp = datetime.strptime(m.group(1), "%b %d %Y %H:%M:%S")
        ts = stamp.replace(tzinfo=timezone.utc).timestamp()
        if a + 0.5 < ts <= b + 0.5:
            rates.append(float(m.group(2)))
    return statistics.mean(rates) * (b - a) if rates else 0.0


def bpftrace_maps(path: Path) -> dict[str, dict[str, int]]:
    """Maps printed by ``bpftrace -f json`` at exit.

    Args:
        path: The ``bpftrace.json`` file.

    Returns:
        Map name without ``@`` -> key -> count. Scalar maps (``@calls``) use
        the key ``""``.
    """
    out: dict[str, dict[str, int]] = {}
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("map", "value"):
            continue
        for name, value in rec.get("data", {}).items():
            key = name.lstrip("@")
            if isinstance(value, dict):
                out[key] = {k: int(v) for k, v in value.items()}
            else:
                out[key] = {"": int(value)}
    return out


def main() -> None:
    """Print tables A and B for the breakdown5 directory in ``sys.argv[1]``."""
    root = Path(sys.argv[1])
    runs = [r for r in RUNS if (root / r / "counters_after.txt").exists()]
    gib = {r: window_gib(root, root / r) for r in runs}
    secs = {r: _times(root / r)[1] - _times(root / r)[0] for r in runs}
    print("## A. Soft-RoCE counters (rdma statistic show link rxe0/1), window deltas\n")
    print(
        "Window: " + ", ".join(f"{r} {secs[r]:.1f} s, {gib[r]:.1f} GiB" for r in runs)
    )
    print(
        "\n| counter | " + " | ".join(f"{r} delta | {r} per GiB" for r in runs) + " |"
    )
    print("|---|" + "---|---|" * len(runs))
    deltas: dict[str, dict[str, int]] = {}
    for r in runs:
        before = _counters(root / r / "counters_before.txt")
        after = _counters(root / r / "counters_after.txt")
        deltas[r] = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    for c in COUNTERS:
        cells = []
        for r in runs:
            d = deltas[r].get(c, 0)
            per = d / gib[r] if gib[r] else 0.0
            cells.append(f"{d} | {per:.1f}")
        print(f"| {c} | " + " | ".join(cells) + " |")
    b_runs = [r for r in runs if (root / r / "bpftrace.json").exists()]
    print("\n## B. rxe_requester exits (bpftrace, 10 s)\n")
    maps = {r: bpftrace_maps(root / r / "bpftrace.json") for r in b_runs}
    bt_gib = {
        r: maps[r].get("exit", {}).get("sent_packet", 0) / PKTS_PER_GIB for r in b_runs
    }
    print(
        "bpftrace window: "
        + ", ".join(
            f"{r} {bt_gib[r]:.1f} GiB ({bt_gib[r] / 10:.2f} GiB/s)" for r in b_runs
        )
        + "\n"
    )
    print("| exit | " + " | ".join(f"{r} count | per GiB | %" for r in b_runs) + " |")
    print("|---|" + "---|---|---|" * len(b_runs))
    for e in EXITS:
        cells = []
        for r in b_runs:
            exits = maps[r].get("exit", {})
            n = exits.get(e, 0)
            total = sum(exits.values()) or 1
            per = n / bt_gib[r] if bt_gib[r] else 0.0
            cells.append(f"{n} | {per:.0f} | {100 * n / total:.2f}")
        print(f"| {e} | " + " | ".join(cells) + " |")
    for name, key in (("calls", ""), ("timer", "retransmit"), ("timer", "rnr")):
        cells = []
        for r in b_runs:
            n = maps[r].get(name, {}).get(key, 0)
            per = n / bt_gib[r] if bt_gib[r] else 0.0
            cells.append(f"{n} | {per:.1f} | ")
        print(f"| {key or name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
