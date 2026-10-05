# SPDX-License-Identifier: Apache-2.0
"""Chart lw TTFT p50 against queue_pairs from the queue-pair scan.

Reads the scan's ``results.csv`` (``perf/aggregate.py`` output, modes
``lw_qp<n>``, ``aon`` and ``nocache``) and writes ``qp_scan.png``: one panel
per prompt length, lw TTFT p50 per concurrency against queue_pairs, with aon
and nocache at the same concurrency as dashed and dotted lines.

Usage::

    python qp_chart.py --csv qpscan_results.csv --out charts
"""

# Standard
import argparse
import csv
import os

# Third Party
import matplotlib

matplotlib.use("Agg")
# Third Party
import matplotlib.pyplot as plt  # noqa: E402


def main() -> None:
    """Write qp_scan.png under --out."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    with open(args.csv) as f:
        rows = [r for r in csv.DictReader(f) if r["valid"] == "1"]
    lengths = sorted({int(r["length"]) for r in rows})
    fig, axes = plt.subplots(1, len(lengths), figsize=(6 * len(lengths), 4.5))
    for ax, length in zip(axes, lengths, strict=True):
        mine = [r for r in rows if int(r["length"]) == length]
        concs = sorted({int(r["concurrency"]) for r in mine if r["mode"] != "nocache"})
        for i, c in enumerate(concs):
            color = f"C{i}"
            pts = sorted(
                (int(r["mode"][len("lw_qp") :]), float(r["ttft_p50"]))
                for r in mine
                if r["mode"].startswith("lw_qp") and int(r["concurrency"]) == c
            )
            ax.plot(
                [p[0] for p in pts],
                [p[1] for p in pts],
                marker="o",
                color=color,
                label=f"lw c={c}",
            )
            for mode, style in (("aon", "--"), ("nocache", ":")):
                ref = [
                    float(r["ttft_p50"])
                    for r in mine
                    if r["mode"] == mode and int(r["concurrency"]) == c
                ]
                if ref:
                    ax.axhline(ref[0], color=color, linestyle=style, linewidth=1)
        ax.set_xscale("log", base=2)
        ax.set_xticks([1, 4, 8, 16], ["1", "4", "8", "16"])
        ax.set_xlabel("rdma.queue_pairs")
        ax.set_ylabel("TTFT p50 (s)")
        ax.set_title(f"{length} tokens, full hits (dashed aon, dotted nocache)")
        ax.grid(True, alpha=0.3)
        ax.legend()
    fig.tight_layout()
    os.makedirs(args.out, exist_ok=True)
    fig.savefig(os.path.join(args.out, "qp_scan.png"), dpi=110)


if __name__ == "__main__":
    main()
