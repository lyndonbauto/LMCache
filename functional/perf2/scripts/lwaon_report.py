# SPDX-License-Identifier: Apache-2.0
"""lw against aon for lwaon.sh's runs: one row per (step, prefix, length, c).

Reads ``<root>/<step>_<rep>/<session>/<session>_<point>.json`` (store files
skipped). A session is aon if its name has ``_aon``, lw if it has ``_lw``. A point
is valid when no request failed, every request generated ``max_tokens``, and vLLM's
external prefix-cache hits are at least 95% of the stored tokens (``n * (length -
1)`` for full hits, ``n * prefix`` for partial hits). TTFT p50 and p90 are NumPy's
linear interpolation over the point's requests. "lw wins" when lw's p50 is lower
than aon's in every repeat where both are valid.

Usage: lwaon_report.py <lwaon-dir> [--csv <out.csv>]
"""

# Standard
from pathlib import Path
import csv
import json
import re
import sys

# Third Party
import numpy as np

STEP_RE = re.compile(r"^(?P<step>[a-z]+)_(?P<rep>[a-z0-9]+)$")


def point_row(path: Path) -> dict[str, str | float | int]:
    """Metrics and validity of one point file.

    Args:
        path: A point JSON written by perf_client.py.

    Returns:
        Row with length, prefix, c, n, ttft_p50, ttft_p90, valid and note.
    """
    d = json.loads(path.read_text())
    reqs = d["requests"]
    ttft = [r["ttft_s"] for r in reqs if not r["error"]]
    pre, length, n = d["prefix_length"], d["length"], d["n"]
    want = n * pre if pre else n * (length - 1)
    hits = d["metrics_delta"].get("vllm:external_prefix_cache_hits_total", 0.0)
    notes = []
    if len(ttft) < len(reqs):
        notes.append(f"{len(reqs) - len(ttft)} failed")
    if any(r["out_tokens"] != d["max_tokens"] for r in reqs if not r["error"]):
        notes.append("short output")
    if hits < 0.95 * want:
        notes.append(f"hits {hits:.0f} < 95% of {want}")
    return {
        "length": length,
        "prefix": pre,
        "c": d["concurrency"],
        "n": n,
        "ttft_p50": float(np.percentile(ttft, 50)) if ttft else float("nan"),
        "ttft_p90": float(np.percentile(ttft, 90)) if ttft else float("nan"),
        "valid": 0 if notes else 1,
        "note": "; ".join(notes),
    }


def collect(root: Path) -> list[dict[str, str | float | int]]:
    """Every point under the lwaon directory.

    Args:
        root: The lwaon results directory.

    Returns:
        One row per point, with step, rep and mode added.
    """
    rows = []
    for rundir in sorted(p for p in root.iterdir() if p.is_dir()):
        m = STEP_RE.match(rundir.name)
        if not m:
            continue
        for f in sorted(rundir.glob("*/*.json")):
            session = f.parent.name
            if "_store_" in f.name or not f.name.startswith(session + "_"):
                continue
            mode = "aon" if "_aon" in session else "lw" if "_lw" in session else ""
            if not mode:
                continue
            row = point_row(f)
            row.update(step=m["step"], rep=m["rep"], mode=mode, session=session)
            rows.append(row)
    return rows


def main(root: str, csv_out: str) -> None:
    """Print the lw-vs-aon table and optionally write the rows as CSV.

    Args:
        root: The lwaon results directory.
        csv_out: CSV path, or "" for none.
    """
    rows = collect(Path(root))
    if csv_out:
        fields = [
            "step",
            "rep",
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
    keys = sorted({(r["step"], r["prefix"], r["length"], r["c"]) for r in rows})
    reps = sorted({str(r["rep"]) for r in rows})
    print("| step | cached + new | c | aon p50 (s) | lw p50 (s) | lw / aon | lw wins |")
    print("|---|---|---|---|---|---|---|")
    for step, pre, length, c in keys:
        aon, lw, ratios = [], [], []
        for rep in reps:
            pick = {
                r["mode"]: r
                for r in rows
                if (r["step"], r["prefix"], r["length"], r["c"], r["rep"])
                == (step, pre, length, c, rep)
            }
            a, b = pick.get("aon"), pick.get("lw")
            aon.append(_fmt(a))
            lw.append(_fmt(b))
            if a and b and a["valid"] and b["valid"]:
                ratios.append(float(b["ttft_p50"]) / float(a["ttft_p50"]))
        shape = f"{pre} + {length}" if pre else f"{length} (full)"
        wins = "yes" if ratios and all(x < 1 for x in ratios) else "no"
        if len(ratios) < 2:
            wins += f" ({len(ratios)} valid pair)"
        ratio = " / ".join(f"{x:.2f}" for x in ratios) or "-"
        print(
            f"| {step} | {shape} | {c} | {' / '.join(aon)} | {' / '.join(lw)} | "
            f"{ratio} | {wins} |"
        )


def _fmt(row: dict[str, str | float | int] | None) -> str:
    if row is None:
        return "-"
    return f"{row['ttft_p50']:.3f}" + ("" if row["valid"] else " INVALID")


if __name__ == "__main__":
    out = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--csv" else ""
    main(sys.argv[1], out)
