# SPDX-License-Identifier: Apache-2.0
"""A/B of lwaon3.sh's runs: aon and lw under several LMCache builds.

Reads ``<root>/<label>/<step>/<session>/<session>_<point>.json`` for each label
(default ``1b,1c``; the first label is the baseline), with
lwaon_report.point_row's validity rules (no failed request, full outputs,
external hits at least 95% of the stored tokens). One row per
(cached + new, c): TTFT p50 of aon and lw under each build, then for every
other label X, lw X / lw <baseline> and lw X / aon X. Steps are read in name
order, and a later step's point replaces an earlier one with the same shape.

Usage: lwaon3_report.py <lwaon3-dir> [--labels 1b,1c,1b2] [--csv <out.csv>]
"""

# Standard
from pathlib import Path
import csv
import sys

# First Party
from lwaon_report import point_row  # type: ignore[import-not-found]

DEFAULT_LABELS = ["1b", "1c"]
Row = dict[str, str | float | int]


def collect(root: Path, labels: list[str]) -> list[Row]:
    """Every aon and lw point of the given builds.

    Args:
        root: The lwaon3 results directory.
        labels: Build labels (subdirectories of root) to read.

    Returns:
        One row per point, with label, step, mode and session added.
    """
    rows: list[Row] = []
    for label in labels:
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


def main(root: str, labels: list[str], csv_out: str) -> None:
    """Print the A/B table and optionally write the rows as CSV.

    Args:
        root: The lwaon3 results directory.
        labels: Build labels; the first is the baseline for the lw ratios.
        csv_out: CSV path, or "" for none.
    """
    rows = collect(Path(root), labels)
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
    base, others = labels[0], labels[1:]
    header = ["cached + new", "c"]
    header += [f"aon {lb}" for lb in labels] + [f"lw {lb}" for lb in labels]
    for lb in others:
        header += [f"lw {lb} / lw {base}", f"lw {lb} / aon {lb}"]
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for pre, length, c in keys:
        cell = {
            (label, mode): index.get((label, mode, pre, length, c))
            for label in labels
            for mode in ("aon", "lw")
        }
        shape = f"{pre} + {length}" if pre else f"{length} (full)"
        cols = [shape, str(c)]
        cols += [_fmt(cell[lb, "aon"]) for lb in labels]
        cols += [_fmt(cell[lb, "lw"]) for lb in labels]
        for lb in others:
            cols += [
                _ratio(cell[lb, "lw"], cell[base, "lw"]),
                _ratio(cell[lb, "lw"], cell[lb, "aon"]),
            ]
        print("| " + " | ".join(cols) + " |")


def _fmt(row: Row | None) -> str:
    if row is None:
        return "-"
    return f"{row['ttft_p50']:.3f}" + ("" if row["valid"] else " INVALID")


def _ratio(top: Row | None, bottom: Row | None) -> str:
    if not (top and bottom and top["valid"] and bottom["valid"]):
        return "-"
    return f"{float(top['ttft_p50']) / float(bottom['ttft_p50']):.2f}"


if __name__ == "__main__":
    args = sys.argv[2:]
    opts = dict(zip(args[::2], args[1::2], strict=False))
    label_arg = opts.get("--labels", "")
    main(
        sys.argv[1],
        label_arg.split(",") if label_arg else DEFAULT_LABELS,
        opts.get("--csv", ""),
    )
