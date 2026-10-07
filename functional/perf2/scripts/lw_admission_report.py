# SPDX-License-Identifier: Apache-2.0
"""Layerwise admission grid: TTFT and inter-token latency per byte budget.

Reads lwaon3.sh's runs under ``<root>``: the admission build's lw points
(label ``1e``; budget from the step or session suffix ``_b<G>`` in GiB or
``_b<N>m`` in MiB, 0 = cap off) and the controls, aon and lw of a reference
build (label ``1c``).
Validity follows lwaon_report.point_row. Prints two tables, one row per
(cached + new, c):

1. TTFT p50 / p90 (s) of aon 1c, lw 1c and lw 1e at every budget, and
   lw 1e at each budget / aon 1c (p50).
2. Inter-token latency of lw 1e at every budget: the median over requests of
   the mean gap between output tokens, ``(total_s - ttft_s) / (out_tokens -
   1)``, in ms, and the p50 / p90 over requests of the largest gap between
   streamed chunks after the first (``max_gap_s``), in s.

Usage: lw_admission_report.py <lwaon3-dir> [--label 1e] [--control 1c]
"""

# Standard
from pathlib import Path
import json
import re
import sys

# Third Party
import numpy as np

# First Party
from lwaon_report import point_row  # type: ignore[import-not-found]

BUDGET_RE = re.compile(r"_b(\d+)(m?)$")
Row = dict[str, str | float | int]
Key = tuple[int, int, int]


def gap_stats(path: Path) -> dict[str, float]:
    """Inter-token latency of one point file's successful requests.

    Args:
        path: A point JSON written by perf_client.py.

    Returns:
        itl_ms (median over requests of the mean inter-token gap, ms),
        gap_p50 and gap_p90 (percentiles of max_gap_s, s); NaN when the
        client did not record them.
    """
    reqs = [r for r in json.loads(path.read_text())["requests"] if not r["error"]]
    itl = [
        (r["total_s"] - r["ttft_s"]) / (r["out_tokens"] - 1)
        for r in reqs
        if "total_s" in r and r["out_tokens"] > 1
    ]
    gaps = [r["max_gap_s"] for r in reqs if "max_gap_s" in r]
    nan = float("nan")
    return {
        "itl_ms": float(np.median(itl)) * 1000 if itl else nan,
        "gap_p50": float(np.percentile(gaps, 50)) if gaps else nan,
        "gap_p90": float(np.percentile(gaps, 90)) if gaps else nan,
    }


def collect(base: Path) -> list[Row]:
    """Every aon and lw point of one build.

    Args:
        base: The build's results directory (``<root>/<label>``).

    Returns:
        One row per point with mode, session, budget (MiB, -1 when the step
        and session carry no budget suffix) and the gap stats added.
    """
    rows: list[Row] = []
    if not base.is_dir():
        return rows
    for stepdir in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in sorted(stepdir.glob("*/*.json")):
            session = f.parent.name
            if "_store_" in f.name or not f.name.startswith(session + "_"):
                continue
            mode = "aon" if "_aon" in session else "lw" if "_lw" in session else ""
            if not mode:
                continue
            m = BUDGET_RE.search(stepdir.name) or BUDGET_RE.search(session)
            row = point_row(f)
            row.update(gap_stats(f))
            row.update(mode=mode, session=session, budget=_budget_mib(m))
            rows.append(row)
    return rows


def main(root: str, label: str, control: str) -> None:
    """Print the TTFT and inter-token latency tables.

    Args:
        root: The lwaon3 results directory.
        label: The admission build's label.
        control: The reference build's label (aon and lw controls).
    """
    test = [r for r in collect(Path(root) / label) if r["mode"] == "lw"]
    ctrl = collect(Path(root) / control)
    budgets = sorted({int(r["budget"]) for r in test if int(r["budget"]) >= 0})
    lw: dict[tuple[int, Key], Row] = {
        (int(r["budget"]), _key(r)): r for r in test if int(r["budget"]) >= 0
    }
    aon = {_key(r): r for r in ctrl if r["mode"] == "aon"}
    lwc = {_key(r): r for r in ctrl if r["mode"] == "lw"}
    keys = sorted({k for _, k in lw})

    header = ["cached + new", "c", f"aon {control}", f"lw {control}"]
    header += [_bname(b) for b in budgets]
    header += [f"{_bname(b)} / aon" for b in budgets]
    _table_header(header)
    for k in keys:
        cols = [_shape(k), str(k[2]), _ttft(aon.get(k)), _ttft(lwc.get(k))]
        cols += [_ttft(lw.get((b, k))) for b in budgets]
        cols += [_ratio(lw.get((b, k)), aon.get(k)) for b in budgets]
        print("| " + " | ".join(cols) + " |")

    print()
    header = ["cached + new", "c"]
    header += [f"ITL ms {_bname(b)}" for b in budgets]
    header += [f"max gap {_bname(b)}" for b in budgets]
    _table_header(header)
    for k in keys:
        cols = [_shape(k), str(k[2])]
        cols += [_itl(lw.get((b, k))) for b in budgets]
        cols += [_gap(lw.get((b, k))) for b in budgets]
        print("| " + " | ".join(cols) + " |")


def _budget_mib(m: re.Match[str] | None) -> int:
    if m is None:
        return -1
    return int(m[1]) if m[2] else int(m[1]) << 10


def _key(row: Row) -> Key:
    return int(row["prefix"]), int(row["length"]), int(row["c"])


def _bname(budget: int) -> str:
    if budget == 0:
        return "off"
    return f"{budget >> 10}G" if budget % 1024 == 0 else f"{budget}M"


def _shape(k: Key) -> str:
    return f"{k[0]} + {k[1]}" if k[0] else f"{k[1]} (full)"


def _table_header(header: list[str]) -> None:
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))


def _ttft(row: Row | None) -> str:
    if row is None:
        return "-"
    cell = f"{row['ttft_p50']:.3f} / {row['ttft_p90']:.3f}"
    return cell if row["valid"] else f"{cell} INVALID ({row['note']})"


def _ratio(top: Row | None, bottom: Row | None) -> str:
    if not (top and bottom and top["valid"] and bottom["valid"]):
        return "-"
    return f"{float(top['ttft_p50']) / float(bottom['ttft_p50']):.2f}"


def _itl(row: Row | None) -> str:
    return "-" if row is None else f"{row['itl_ms']:.1f}"


def _gap(row: Row | None) -> str:
    if row is None:
        return "-"
    return f"{row['gap_p50']:.3f} / {row['gap_p90']:.3f}"


if __name__ == "__main__":
    args = sys.argv[2:]
    opts = dict(zip(args[::2], args[1::2], strict=False))
    main(sys.argv[1], opts.get("--label", "1e"), opts.get("--control", "1c"))
