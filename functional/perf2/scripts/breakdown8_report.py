# SPDX-License-Identifier: Apache-2.0
"""Tables for breakdown8.sh (Sriram, 2026-10-06 16:08 PT, server 55d6ae8d8).

Subcommands:

- ``faster <breakdown8-dir>``: prints the config whose clean run has the
  higher ``kvlayers`` GiB/s (empty if none ran).
- ``report <breakdown8-dir>``: markdown, per config:

  1. GiB/s (``kvlayers`` over 25 s, and the mean of the stats lines inside the
     clean run's 10 s window), fields from the highest-rate stats line, mpstat
     CPUs busy, and 3 steady stats lines.
  2. ``rxe_requester`` exits per GiB from the bt run (GiB = ``sent_packet`` /
     262,144).
  3. The placer off-CPU summary from ``offcpu/tables.md`` (written by
     ``breakdown7_report.py sched``): % off CPU, sleeps per write, sleep per
     write, and the top 3 blocking stacks.
  4. lw 8k c=1 at 16 QPs: E3 layer 0 and layer 31 arrival times, the whole
     fetch rate, breakdown3's layer 0 -> 31 rate, and the point line.

Also ``layers <step-dir>`` prints the layer 0 / layer 31 medians for any step
with an E3 timeline (used to compare with ``breakdown3/mem``).
"""

# Standard
from pathlib import Path
import re
import statistics
import sys

# First Party
from breakdown5_report import EXITS, PKTS_PER_GIB, bpftrace_maps
from breakdown6_report import FIELD_RES, STREAM_RE, window_lines
from breakdown_report import E3_RE, LAYER_MIB, timeline_gib

WINDOW_RE = re.compile(r"^sched window ([\d.]+) s, (\d+) writes/s", re.M)
POINT_RE = re.compile(r"^point .*$", re.M)


def _rate(run: Path) -> float:
    """``kvlayers`` GiB/s of a run; 0.0 if missing."""
    kvl = run / "kvl.txt"
    m = STREAM_RE.search(kvl.read_text(errors="replace")) if kvl.exists() else None
    return float(m.group(1)) if m else 0.0


def _configs(root: Path) -> list[Path]:
    """Config directories (those with a clean run), oldest first."""
    return sorted(
        (p for p in root.iterdir() if (p / "clean" / "times.txt").exists()),
        key=lambda p: p.stat().st_mtime,
    )


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


def _cells(line: str) -> list[str]:
    """The cells of a markdown table row."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def placer_summary(tables: Path) -> tuple[list[str], list[list[str]], float]:
    """Placer rows from a ``breakdown7_report.py sched`` table file.

    Args:
        tables: ``offcpu/tables.md``.

    Returns:
        ``(role-table row cells, top 3 blocking-stack row cells, writes/s)``;
        empty lists and 0.0 when the file or rows are missing.
    """
    role: list[str] = []
    stacks: list[list[str]] = []
    wps = 0.0
    if not tables.exists():
        return role, stacks, wps
    text = tables.read_text(errors="replace")
    m = WINDOW_RE.search(text)
    wps = float(m.group(2)) if m else 0.0
    for line in text.splitlines():
        if not line.startswith("| placer |"):
            continue
        cells = _cells(line)
        if len(cells) == 10 and not role:
            role = cells
        elif len(cells) >= 6 and "`" in line and len(stacks) < 3:
            stacks.append(cells[:5] + [" | ".join(cells[5:])])
    return role, stacks, wps


def layer_arrivals(step: Path) -> tuple[float, float, int]:
    """Median layer 0 and layer 31 residency times from the E3 timeline.

    Args:
        step: A step directory with ``E_timeline_qp16/lmcache_*.log``.

    Returns:
        ``(layer 0 ms, layer 31 ms, retrieves)``, times after ``begin_fetch``;
        zeros when no retrieve has both layers.
    """
    gens: dict[str, dict[int, float]] = {}
    for log in (step / "E_timeline_qp16").glob("lmcache_*.log"):
        for m in E3_RE.finditer(log.read_text(errors="replace")):
            gens.setdefault(m.group(3), {})[int(m.group(1))] = float(m.group(2))
    full = [g for g in gens.values() if 0 in g and 31 in g]
    if not full:
        return 0.0, 0.0, 0
    first = statistics.median(g[0] for g in full)
    last = statistics.median(g[31] for g in full)
    return first, last, len(full)


def cmd_faster(root: Path) -> None:
    """Print the config name with the higher clean-run GiB/s."""
    cfgs = _configs(root)
    if cfgs:
        print(max(cfgs, key=lambda c: _rate(c / "clean")).name)


def cmd_report(root: Path) -> None:
    """Print the markdown report for the breakdown8 directory ``root``."""
    cfgs = _configs(root)
    print("## 1. Rate and steady stats (clean run)\n")
    print(
        "| config | kvlayers GiB/s (25 s) | stats GiB/s (10 s) | "
        + " | ".join(FIELD_RES)
        + " | CPUs busy |"
    )
    print("|---|---|---|" + "---|" * len(FIELD_RES) + "---|")
    steady_out: list[str] = []
    for cfg in cfgs:
        rows = window_lines(cfg, cfg / "clean")
        steady = sorted(rows, key=lambda r: -r[0])[:3]
        mean = statistics.mean(r[0] for r in rows) if rows else 0.0
        top = steady[0][1] if steady else ""
        fields = []
        for rx in FIELD_RES.values():
            m = rx.search(top)
            fields.append(m.group(1) if m else "-")
        busy = _mpstat_busy(cfg / "clean" / "mpstat.txt")
        print(
            f"| {cfg.name} | {_rate(cfg / 'clean'):.2f} | {mean:.2f} | "
            + " | ".join(fields)
            + f" | {busy:.1f} |"
        )
        steady_out.append(f"{cfg.name}:")
        steady_out.extend("  " + r[1].split("kv-sink: stats: ")[-1] for r in steady)
    print("\nSteady stats lines (the 3 highest-rate lines in the window):\n")
    print("```")
    print("\n".join(steady_out))
    print("```")

    print("\n## 2. rxe_requester exits per GiB (bt run, 10 s)\n")
    bt = [c for c in cfgs if (c / "bt" / "bpftrace.json").exists()]
    maps = {c.name: bpftrace_maps(c / "bt" / "bpftrace.json") for c in bt}
    print("| exit | " + " | ".join(c.name for c in bt) + " |")
    print("|---|" + "---|" * len(bt))
    gib = {
        k: v.get("exit", {}).get("sent_packet", 0) / PKTS_PER_GIB
        for k, v in maps.items()
    }
    for e in EXITS:
        row = []
        for c in bt:
            n = maps[c.name].get("exit", {}).get(e, 0)
            row.append(f"{n / gib[c.name]:.0f}" if gib[c.name] else "-")
        print(f"| {e} | " + " | ".join(row) + " |")
    print(
        "| GiB/s (bt window) | "
        + " | ".join(f"{gib[c.name] / 10:.2f}" for c in bt)
        + " |"
    )
    print(
        "| kvlayers GiB/s (bt run) | "
        + " | ".join(f"{_rate(c / 'bt'):.2f}" for c in bt)
        + " |"
    )

    print("\n## 3. Placers off CPU (offcpu run, 5 s sched window)\n")
    print(
        "| config | writes/s | off CPU % | sleeps per write | mean sleep ms | "
        "wake->run p50 / p99 ms | placer sleep ms per write |"
    )
    print("|---|---|---|---|---|---|---|")
    stack_out: list[str] = []
    for cfg in cfgs:
        role, stacks, wps = placer_summary(cfg / "offcpu" / "tables.md")
        if not role:
            print(f"| {cfg.name} | - | - | - | - | - | - |")
            continue
        per_thread = float(role[3])
        sleeps = float(role[4])
        mean_ms = float(role[6])
        per_write = sleeps * mean_ms / wps if wps else 0.0
        print(
            f"| {cfg.name} | {wps:.0f} | {100 * per_thread:.1f} | {role[5]} | "
            f"{role[6]} | {role[7]} | {per_write:.3f} |"
        )
        for s in stacks:
            ms_write = 1000 * float(s[1]) / wps if wps else 0.0
            stack_out.append(
                f"| {cfg.name} | {s[1]} | {ms_write:.3f} | {s[2]} | {s[4]} | {s[5]} |"
            )
    print("\nTop 3 placer blocking stacks by sleep time:\n")
    print("| config | sleep s/s | ms per write | % of placer sleep | mean ms | stack |")
    print("|---|---|---|---|---|---|")
    print("\n".join(stack_out))

    lw = root / "lw"
    print("\n## 4. lw 8k c=1 at 16 QPs\n")
    if (lw / "config.txt").exists():
        sessions = list((lw / "E_timeline_qp16").glob("session_*.txt"))
        points = [
            m.group(0)
            for s in sessions
            for m in POINT_RE.finditer(s.read_text(errors="replace"))
        ]
        first, last, n = layer_arrivals(lw)
        whole = 32 * LAYER_MIB / 1024 / (last / 1000) if last else 0.0
        print(f"- config: {(lw / 'config.txt').read_text().strip()}")
        print(
            f"- layer 0 resident +{first:.1f} ms, layer 31 +{last:.1f} ms after "
            f"begin_fetch (medians of {n} retrieves); whole fetch "
            f"{whole:.2f} GiB/s (1 GiB / layer 31 time)"
        )
        print(
            f"- layer 0 -> 31 rate (breakdown3's measure): "
            f"{timeline_gib(lw, '16'):.2f} GiB/s; overstated when layer 0 lands late"
        )
        print("- " + ("; ".join(f"`{p}`" for p in points) or "no point line"))
    else:
        print("- not run")

    ab = root / "ab"
    if ab.exists():
        print("\n## 5. Same-session A/B (clean runs, in run order)\n")
        print(
            "| run | kvlayers GiB/s (25 s) | stats GiB/s (10 s) | "
            + " | ".join(FIELD_RES)
            + " | CPUs busy |"
        )
        print("|---|---|---|" + "---|" * len(FIELD_RES) + "---|")
        for d in sorted(p for p in ab.iterdir() if (p / "clean").exists()):
            rows = window_lines(d, d / "clean")
            mean = statistics.mean(r[0] for r in rows) if rows else 0.0
            top = max(rows, key=lambda r: r[0])[1] if rows else ""
            fields = []
            for rx in FIELD_RES.values():
                m = rx.search(top)
                fields.append(m.group(1) if m else "-")
            busy = _mpstat_busy(d / "clean" / "mpstat.txt")
            print(
                f"| {d.name} | {_rate(d / 'clean'):.2f} | {mean:.2f} | "
                + " | ".join(fields)
                + f" | {busy:.1f} |"
            )


def main() -> None:
    """Dispatch on ``sys.argv[1]``; see the module docstring."""
    cmd, root = sys.argv[1], Path(sys.argv[2])
    if cmd == "faster":
        cmd_faster(root)
    elif cmd == "report":
        cmd_report(root)
    elif cmd == "layers":
        first, last, n = layer_arrivals(root)
        print(f"layer 0 +{first:.1f} ms, layer 31 +{last:.1f} ms, {n} retrieves")
    else:
        raise ValueError(f"unknown subcommand {cmd}")


if __name__ == "__main__":
    main()
