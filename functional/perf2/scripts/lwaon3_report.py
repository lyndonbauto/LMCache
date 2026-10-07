# SPDX-License-Identifier: Apache-2.0
"""D-27 A/B for lwaon3.sh's runs: aon and lw, prototype-stage-1b against 1c.

Reads ``<root>/<label>/<step>/<session>/<session>_<point>.json`` for the labels
``1b`` and ``1c``, with lwaon_report.point_row's validity rules (no failed
request, full outputs, external hits at least 95% of the stored tokens). One
row per (cached + new, c): TTFT p50 of aon and lw under each build, then
lw 1c / lw 1b and lw 1c / aon 1c. Steps are read in name order, and a later
step's point replaces an earlier one with the same shape.

Usage: lwaon3_report.py <lwaon3-dir> [--csv <out.csv>]
"""

# Standard
from pathlib import Path
import csv
import sys

# First Party
from lwaon_report import point_row  # type: ignore[import-not-found]

LABELS = ("1b", "1c")
Row = dict[str, str | float | int]


def collect(root: Path) -> list[Row]:
    """Every aon and lw point of both builds.

    Args:
        root: The lwaon3 results directory.

    Returns:
        One row per point, with label, step, mode and session added.
    """
    rows: list[Row] = []
    for label in LABELS:
        base = root / label
        if not base.is_dir():
            continue
        for stepdir in sorted(p for p in base.iterdir() if p.is_dir()):
            for f in sorted(stepdir.glob("*/*.json")):
                session = f.parent.name
                if "_store_" in f.name or not f.name.startswith(session + "_"):
                    continue
                if "_aon" in session:
                    mode = "aon"
                elif "_lw" in session:
                    mode = "lw"
                else:
                    continue
                row = point_row(f)
                row.update(label=label, step=stepdir.name, mode=mode, session=session)
                rows.append(row)
    return rows


def main(root: str, csv_out: str) -> None:
    """Print the A/B table and optionally write the rows as CSV.

    Args:
        root: The lwaon3 results directory.
        csv_out: CSV path, or "" for none.
    """
    rows = collect(Path(root))
    if csv_out:
        fields = [
            "label",
            "step",
            "mode",
            "session",
            "prefix",
            "length",
            "c",
            "n",
            "ttft_p50",
            "ttft_p90",
            "valid",
            "note",
        ]
        with open(csv_out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    # A rerun step (after a host reboot) replaces the earlier point.
    index = {(r["label"], r["mode"], r["prefix"], r["length"], r["c"]): r for r in rows}
    keys = sorted({(r["prefix"], r["length"], r["c"]) for r in rows})
    print(
        "| cached + new | c | aon 1b | aon 1c | lw 1b | lw 1c "
        "| lw 1c / lw 1b | lw 1c / aon 1c |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for pre, length, c in keys:
        cell = {
            (label, mode): index.get((label, mode, pre, length, c))
            for label in LABELS
            for mode in ("aon", "lw")
        }
        shape = f"{pre} + {length}" if pre else f"{length} (full)"
        print(
            f"| {shape} | {c} | {_fmt(cell['1b', 'aon'])} "
            f"| {_fmt(cell['1c', 'aon'])} | {_fmt(cell['1b', 'lw'])} "
            f"| {_fmt(cell['1c', 'lw'])} "
            f"| {_ratio(cell['1c', 'lw'], cell['1b', 'lw'])} "
            f"| {_ratio(cell['1c', 'lw'], cell['1c', 'aon'])} |"
        )


def _fmt(row: Row | None) -> str:
    if row is None:
        return "-"
    return f"{row['ttft_p50']:.3f}" + ("" if row["valid"] else " INVALID")


def _ratio(top: Row | None, bottom: Row | None) -> str:
    if not (top and bottom and top["valid"] and bottom["valid"]):
        return "-"
    return f"{float(top['ttft_p50']) / float(bottom['ttft_p50']):.2f}"


if __name__ == "__main__":
    out = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--csv" else ""
    main(sys.argv[1], out)
