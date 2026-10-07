# SPDX-License-Identifier: Apache-2.0
"""Plain-text tables for breakdown10.sh (Sriram, 2026-10-06 20:19 PT).

Reads ``<breakdown10-dir>/`` (``facts/``, ``raw/``, ``run_a/``, ``run_b/``)
and prints:

1. the container flags and the machine facts files as collected;
2. the ``ib_write_bw`` reference rate;
3. per run: ``kvlayers`` GiB/s, the 2 highest-rate stats lines in the 10 s
   window, mpstat per-CPU averages, the top 25 threads (top's second frame),
   and the 10 interrupt sources with the largest ``/proc/interrupts`` delta
   over the stream, with the CPUs that took them.

Usage: breakdown10_report.py <breakdown10-dir>
"""

# Standard
from pathlib import Path
import re
import sys

# First Party
from breakdown6_report import STREAM_RE, window_lines

GBITS_RE = re.compile(r"^\s*524288\s+\d+\s+[\d.]+\s+([\d.]+)", re.M)
MPSTAT_COLS = ("%usr", "%sys", "%irq", "%soft", "%steal", "%idle")
RUN_NAMES = {
    "run_a": "(a) vLLM + LMCache MP server up and idle",
    "run_b": "(b) vLLM and LMCache MP server stopped",
}


def _mpstat_rows(path: Path) -> list[tuple[str, dict[str, float]]]:
    """mpstat's Average block as (cpu, {column: value}) rows; [] if missing."""
    rows: list[tuple[str, dict[str, float]]] = []
    if not path.exists():
        return rows
    header: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        f = line.split()
        if len(f) < 3 or f[0] != "Average:":
            continue
        if f[1] == "CPU":
            header = f[1:]
            continue
        if header and len(f) - 1 == len(header):
            rows.append(
                (f[1], {h: float(v) for h, v in zip(header[1:], f[2:], strict=True)})
            )
    return rows


def _irq_counts(path: Path) -> tuple[int, dict[str, tuple[list[int], str]]]:
    """Parse ``/proc/interrupts``.

    Args:
        path: A saved copy of ``/proc/interrupts``.

    Returns:
        The CPU count, and per source label its per-CPU counts and description.
    """
    lines = path.read_text(errors="replace").splitlines()
    ncpu = len(lines[0].split())
    out: dict[str, tuple[list[int], str]] = {}
    for line in lines[1:]:
        f = line.split()
        if not f or not f[0].endswith(":"):
            continue
        counts = []
        for tok in f[1 : 1 + ncpu]:
            if not tok.isdigit():
                break
            counts.append(int(tok))
        desc = " ".join(f[1 + len(counts) :])
        out[f[0][:-1]] = (counts, desc)
    return ncpu, out


def _irq_table(run: Path, top: int = 10) -> list[str]:
    """The ``top`` interrupt sources by total delta, with the CPUs they hit."""
    before, after = run / "irq_before.txt", run / "irq_after.txt"
    if not (before.exists() and after.exists()):
        return ["(no /proc/interrupts snapshots)"]
    _, b = _irq_counts(before)
    _, a = _irq_counts(after)
    deltas: list[tuple[int, str, list[int], str]] = []
    for label, (counts, desc) in a.items():
        old = b.get(label, ([0] * len(counts), ""))[0]
        old = (old + [0] * len(counts))[: len(counts)]
        per = [x - y for x, y in zip(counts, old, strict=True)]
        deltas.append((sum(per), label, per, desc))
    deltas.sort(reverse=True)
    out = [
        f"{'source':<8} {'delta':>10}  {'description':<40} CPUs (share of this source)"
    ]
    for total, label, per, desc in deltas[:top]:
        if total <= 0:
            break
        hit = sorted(((v, i) for i, v in enumerate(per) if v > 0), reverse=True)
        shares = ", ".join(f"cpu{i} {100 * v / total:.0f}%" for v, i in hit[:6])
        if len(hit) > 6:
            shares += f", +{len(hit) - 6} more CPUs"
        out.append(f"{label:<8} {total:>10}  {desc[:40]:<40} {shares}")
    return out


def _top_threads(path: Path, n: int = 25) -> list[str]:
    """The first ``n`` thread lines of top's last frame."""
    if not path.exists():
        return ["(no top output)"]
    text = path.read_text(errors="replace")
    frame = text.split("\ntop - ")[-1].splitlines()
    for i, line in enumerate(frame):
        if line.lstrip().startswith("PID"):
            return [line.rstrip()] + [
                x.rstrip()[:140] for x in frame[i + 1 : i + 1 + n]
            ]
    return ["(no thread table in top output)"]


def _section(title: str, lines: list[str]) -> None:
    """Print a titled plain-text block."""
    print(f"\n=== {title} ===")
    print("\n".join(lines))


def _run(root: Path, name: str) -> None:
    """Print one run's tables."""
    d = root / name
    clean = d / "clean"
    kvl = clean / "kvl.txt"
    m = STREAM_RE.search(kvl.read_text(errors="replace")) if kvl.exists() else None
    rate = f"{float(m.group(1)):.2f} GiB/s, failed rows {m.group(2)}" if m else "-"
    procs = d / "procs.txt"
    nprocs = len(procs.read_text().splitlines()) if procs.exists() else 0
    rows = window_lines(d, clean) if (clean / "times.txt").exists() else []
    steady = sorted(rows, key=lambda r: -r[0])[:2]
    out = [f"kvlayers --qps 16 --duration 20: {rate}"]
    out.append(f"vLLM/LMCache processes in lmc-c during the stream: {nprocs}")
    out.append("asd nodes: 1 (single-node mesh config); stats lines from that node:")
    out.extend("  " + r[1].split("kv-sink: stats: ")[-1] for r in steady)
    mp = _mpstat_rows(clean / "mpstat.txt")
    out.append("")
    out.append("mpstat -P ALL 1 10, averages:")
    out.append(f"{'CPU':>4} " + " ".join(f"{c:>7}" for c in MPSTAT_COLS))
    busy = 0.0
    for cpu, vals in mp:
        out.append(
            f"{cpu:>4} " + " ".join(f"{vals.get(c, 0.0):7.2f}" for c in MPSTAT_COLS)
        )
        if cpu != "all":
            busy += (100 - vals.get("%idle", 100.0)) / 100
    out.append(f"CPUs busy (sum of 1 - %idle): {busy:.1f}")
    out.append("")
    out.append(
        "top -b -H, 25 threads (second frame, 2 s interval, 6 s into the stream):"
    )
    out.extend(_top_threads(clean / "top_raw.txt"))
    out.append("")
    out.append("/proc/interrupts delta over the 20 s stream, top 10 sources:")
    out.extend(_irq_table(clean))
    _section(RUN_NAMES.get(name, name), out)


def main() -> None:
    """Print the tables for the breakdown10 directory in ``sys.argv[1]``."""
    root = Path(sys.argv[1])
    facts = root / "facts"
    for f in ("docker.txt", "machine.txt"):
        p = facts / f
        _section(
            f"facts/{f}",
            p.read_text(errors="replace").splitlines() if p.exists() else ["(missing)"],
        )
    raw = root / "raw" / "q16_t2_client.txt"
    m = GBITS_RE.search(raw.read_text(errors="replace")) if raw.exists() else None
    ref = "-"
    if m:
        gbits = float(m.group(1))
        ref = f"{gbits:.2f} Gb/s = {gbits * 1e9 / 8 / 2**30:.2f} GiB/s"
    _section("ib_write_bw -s 524288 -q 16 -t 2, 10 s (reference)", [ref])
    rows = _mpstat_rows(root / "raw" / "q16_t2_mpstat.txt")
    busy = sum((100 - v.get("%idle", 100.0)) / 100 for c, v in rows if c != "all")
    _section("ib_write_bw mpstat (5 s)", [f"CPUs busy: {busy:.1f}"])
    for name in ("run_a", "run_b"):
        if (root / name).exists():
            _run(root, name)


if __name__ == "__main__":
    main()
