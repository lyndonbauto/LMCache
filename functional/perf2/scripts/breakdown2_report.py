# SPDX-License-Identifier: Apache-2.0
"""Tables for the day-2 perf2 breakdown (breakdown2.sh): Q1 and Q2.

Q1 A: CPU per GiB per function of the Soft-RoCE workers (``kworker`` threads;
perf truncates their names, so every kworker counts) during ib_write_bw and
during kvlayers. Samples come from ``perf record -F 999``, so one sample is
about 1 ms of CPU. GiB moved in the 5 s window: ib_write_bw's average rate x
5 s; kvlayers' stats lines inside the window.
Q1 B: wire us per write of the stats lines with at least 28 writes in flight.
Q1 C: ib_write_bw alone and alongside kvlayers.
Q2 D: CPU per GiB by thread kind and dso during aon, GiB from the namespace's
read counters (512 KiB records).

Q3 (G) uses breakdown_report.py on the q3 directory.

Usage: breakdown2_report.py <breakdown2-dir>
"""

# Standard
from pathlib import Path
import re
import statistics
import sys

# Local
from breakdown_report import STATS_RE, read_marks

SAMPLE_MS = 1000 / 999
RECORD_GIB = 0.5 / 1024
PT_RE = re.compile(r"^ *524288 +\d+ +[\d.]+ +([\d.]+)", re.M)
STREAM_RE = re.compile(r"^stream: .* ([\d.]+) GiB/s", re.M)


def kind(comm: str) -> str:
    """Thread kind: the comm without its numbering.

    Args:
        comm: A perf comm, for example ``kworker/u46:2-r``.

    Returns:
        The comm up to its first ``/`` or ``:``, trailing digits removed.
    """
    return re.sub(r"[/:].*$|\d+$", "", comm)


def perf_rows(path: Path) -> list[tuple[int, str, str, str]]:
    """Rows of a ``perf report -n`` file.

    Args:
        path: perf_full.txt (comm, dso, symbol) or perf_comm_dso.txt.

    Returns:
        (samples, comm, dso, symbol) per row; symbol is "" for comm,dso.
    """
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        f = line.split()
        if len(f) < 4 or not f[0].endswith("%") or not f[1].isdigit():
            continue
        sym = ""
        if len(f) > 5 and f[4] in ("[.]", "[k]"):
            sym = " ".join(t for t in f[5:] if t != "-")
        rows.append((int(f[1]), f[2], f[3], sym))
    return rows


def perftest_gib_s(path: Path) -> float:
    """ib_write_bw's average rate.

    Args:
        path: The client's output.

    Returns:
        GiB/s (0.0 if the run printed no result line).
    """
    m = PT_RE.search(path.read_text(errors="replace")) if path.exists() else None
    return float(m.group(1)) * 1e9 / 8 / 2**30 if m else 0.0


def stats_slice(step: Path, start: str, end: str) -> list[dict[str, float]]:
    """The stats lines between two marks.

    Args:
        step: Directory with marks.txt and the server log.
        start: Mark name at the slice start.
        end: Mark name at the slice end.

    Returns:
        One dict of stats fields per line.
    """
    marks = read_marks(step)
    lines = (step / "asd-kvsink-bp-perf.log").read_text(errors="replace")
    out = []
    for line in lines.splitlines()[marks[start] : marks[end]]:
        m = STATS_RE.search(line)
        if m:
            out.append({k: float(v) for k, v in m.groupdict().items()})
    return out


def q1_a(q1: Path) -> None:
    """Print Q1 A: rxe workers' top 30 symbols, samples and ms per GiB.

    Args:
        q1: The q1 step directory.
    """
    gib = {
        "perftest": perftest_gib_s(q1 / "A_perftest_client.txt") * 5,
        "kvlayers": sum(
            r["gib"] for r in stats_slice(q1, "A_kvl_prof_start", "A_kvl_prof_end")
        ),
    }
    dirs = {"perftest": q1 / "A_perftest", "kvlayers": q1 / "A_kvlayers"}
    syms: dict[str, dict[str, int]] = {"perftest": {}, "kvlayers": {}}
    kinds: dict[str, dict[str, int]] = {"perftest": {}, "kvlayers": {}}
    for run, d in dirs.items():
        for n, comm, _dso, sym in perf_rows(d / "perf_full.txt"):
            kinds[run][kind(comm)] = kinds[run].get(kind(comm), 0) + n
            if kind(comm) == "kworker":
                syms[run][sym] = syms[run].get(sym, 0) + n
    print("## Q1 A: GiB moved in the 5 s window")
    for run, g in gib.items():
        print(f"- {run}: {g:.1f} GiB")
    print("\n## Q1 A: CPU by thread kind (samples ~ ms; ms per GiB)\n")
    print("| thread kind | perftest | ms/GiB | kvlayers | ms/GiB |")
    print("|---|---|---|---|---|")
    names = sorted(
        set(kinds["perftest"]) | set(kinds["kvlayers"]),
        key=lambda k: -(kinds["perftest"].get(k, 0) + kinds["kvlayers"].get(k, 0)),
    )
    for k in names[:8]:
        p, v = kinds["perftest"].get(k, 0), kinds["kvlayers"].get(k, 0)
        print(
            f"| {k} | {p} | {_per_gib(p, gib['perftest'])} | {v} | "
            f"{_per_gib(v, gib['kvlayers'])} |"
        )
    print("\n## Q1 A: rxe workers' top 30 symbols (by kvlayers samples)\n")
    print("| symbol | perftest | ms/GiB | kvlayers | ms/GiB | ratio |")
    print("|---|---|---|---|---|---|")
    top = sorted(syms["kvlayers"].items(), key=lambda x: -x[1])[:30]
    for sym, v in top:
        p = syms["perftest"].get(sym, 0)
        pg = p * SAMPLE_MS / gib["perftest"] if gib["perftest"] else 0.0
        vg = v * SAMPLE_MS / gib["kvlayers"] if gib["kvlayers"] else 0.0
        ratio = f"{vg / pg:.1f}x" if pg else "-"
        print(f"| {sym} | {p} | {pg:.1f} | {v} | {vg:.1f} | {ratio} |")


def q1_b(q1: Path) -> None:
    """Print Q1 B: wire us per write at about 32 in flight, hot and cold.

    Args:
        q1: The q1 step directory.
    """
    print("\n## Q1 B: hot (32 MiB) vs cold (1 GiB), lines with >= 28 in flight\n")
    print(
        "| run | GiB/s (stream) | lines | wire us median | wire us range | "
        "copy us | in flight |"
    )
    print("|---|---|---|---|---|---|---|")
    for name in ("hot1", "cold1", "hot2", "cold2"):
        rows = [
            r
            for r in stats_slice(q1, f"B_{name}_start", f"B_{name}_end")
            if r["ifw"] >= 28
        ]
        txt = (q1 / f"kvl_B_{name}.txt").read_text(errors="replace")
        m = STREAM_RE.search(txt)
        if not rows:
            print(f"| {name} | {m.group(1) if m else '-'} | 0 | - | - | - | - |")
            continue
        wire = [r["wire"] for r in rows]
        print(
            f"| {name} | {m.group(1) if m else '-'} | {len(rows)} | "
            f"{statistics.median(wire):.0f} | {min(wire):.0f}-{max(wire):.0f} | "
            f"{statistics.median(r['copy'] for r in rows):.0f} | "
            f"{statistics.median(r['ifw'] for r in rows):.1f} |"
        )


def q1_c(q1: Path) -> None:
    """Print Q1 C: ib_write_bw alone and alongside kvlayers --qps 1.

    Args:
        q1: The q1 step directory.
    """
    print("\n## Q1 C: ib_write_bw -q 16 -t 2, alone vs alongside kvlayers\n")
    print("| run | ib_write_bw GiB/s |")
    print("|---|---|")
    for name in ("C_alone1", "C_with_kvl", "C_alone2"):
        print(f"| {name} | {perftest_gib_s(q1 / f'{name}_client.txt'):.2f} |")
    m = STREAM_RE.search((q1 / "kvl_C.txt").read_text(errors="replace"))
    print(f"\nkvlayers --qps 1 alongside: {m.group(1) if m else '-'} GiB/s")


def q2_d(q2: Path, sink_ms_per_gib: float) -> None:
    """Print Q2 D: CPU per GiB by thread kind and dso during aon.

    Args:
        q2: The q2 step directory.
        sink_ms_per_gib: kvlayers' total CPU ms per GiB from Q1 A.
    """
    reads = []
    for line in (q2 / "D_reads.txt").read_text().splitlines():
        reads.append(sum(int(v) for v in re.findall(r"=(\d+)", line)))
    gib = (reads[1] - reads[0]) * RECORD_GIB if len(reads) == 2 else 0.0
    rows: dict[tuple[str, str], int] = {}
    for n, comm, dso, _sym in perf_rows(q2 / "D_aon" / "perf_comm_dso.txt"):
        rows[(kind(comm), dso)] = rows.get((kind(comm), dso), 0) + n
    total = sum(rows.values())
    print(f"\n## Q2 D: aon 8k c=16, {gib:.1f} GiB fetched in the window\n")
    print("| thread kind | dso | samples | ms/GiB |")
    print("|---|---|---|---|")
    for (k, dso), n in sorted(rows.items(), key=lambda x: -x[1])[:15]:
        print(f"| {k} | {dso} | {n} | {_per_gib(n, gib)} |")
    print(
        f"\nTotal: {total} samples, {_per_gib(total, gib)} ms/GiB; "
        f"kvlayers over the sink (Q1 A): {sink_ms_per_gib:.0f} ms/GiB"
    )


def _per_gib(samples: int, gib: float) -> str:
    return f"{samples * SAMPLE_MS / gib:.0f}" if gib else "-"


def main(root: str) -> None:
    """Print the Q1 and Q2 tables.

    Args:
        root: The breakdown2 directory.
    """
    q1, q2 = Path(root) / "q1", Path(root) / "q2"
    sink = 0.0
    if (q1 / "A_kvlayers" / "perf_full.txt").exists():
        q1_a(q1)
        q1_b(q1)
        q1_c(q1)
        kvl = sum(n for n, *_ in perf_rows(q1 / "A_kvlayers" / "perf_full.txt"))
        g = sum(r["gib"] for r in stats_slice(q1, "A_kvl_prof_start", "A_kvl_prof_end"))
        sink = kvl * SAMPLE_MS / g if g else 0.0
    if (q2 / "D_reads.txt").exists():
        q2_d(q2, sink)


if __name__ == "__main__":
    main(sys.argv[1])
