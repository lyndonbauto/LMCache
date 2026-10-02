# SPDX-License-Identifier: Apache-2.0
"""Draw the perf sweep's charts from results.csv (aggregate.py output).

Writes, under ``--out``:

- ``ttft_L<len>.png``: TTFT p50 vs concurrency, one line per mode;
- ``total_L<len>.png``: total latency p50 vs concurrency, one line per mode;
- ``ttft_vs_length_c1.png``: TTFT p50 vs prompt length at concurrency 1.

INVALID points are drawn as hollow red-edged markers and left out of the line.
p10-p90 bands are not drawn (n is 4 at low concurrency); the p90 is in the CSV.

Usage::

    python charts.py --csv functional/perf/results.csv --out functional/perf/charts
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

STYLE = {
    "nocache": {"color": "#555555", "marker": "o", "label": "no cache (recompute)"},
    "aon": {
        "color": "#1f77b4",
        "marker": "s",
        "label": "Aerospike disk, all-or-nothing",
    },
    "lw": {
        "color": "#d62728",
        "marker": "^",
        "label": "Aerospike disk, layer-by-layer (Soft-RoCE)",
    },
}
LABEL = {8192: "8k", 16384: "16k", 32768: "32k", 65536: "64k", 130816: "128k"}


def load(path: str) -> list[dict]:
    """Return results.csv rows with numeric fields converted."""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("length", "concurrency", "n", "valid"):
            r[k] = int(r[k])
        for k in ("ttft_p50", "total_p50", "ttft_p90", "total_p90"):
            r[k] = float(r[k])
    return rows


def line_chart(
    rows: list[dict],
    xkey: str,
    ykey: str,
    title: str,
    xlabel: str,
    path: str,
    xticks: dict[int, str],
) -> None:
    """Plot ``ykey`` against ``xkey`` per mode and save a small PNG."""
    fig, ax = plt.subplots(figsize=(6.4, 4.0), dpi=100)
    for mode, st in STYLE.items():
        pts = sorted((r for r in rows if r["mode"] == mode), key=lambda r: r[xkey])
        good = [r for r in pts if r["valid"]]
        bad = [r for r in pts if not r["valid"]]
        if good:
            ax.plot(
                [r[xkey] for r in good],
                [r[ykey] for r in good],
                color=st["color"],
                marker=st["marker"],
                label=st["label"],
            )
        if bad:
            ax.scatter(
                [r[xkey] for r in bad],
                [r[ykey] for r in bad],
                facecolors="none",
                edgecolors="red",
                marker=st["marker"],
                s=60,
                label=f"{mode} INVALID",
            )
    ax.set_xscale("log", base=2)
    ax.set_xticks(list(xticks))
    ax.set_xticklabels(list(xticks.values()))
    ax.set_yscale("log")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(f"{ykey.replace('_p50', '')} p50 (s, log scale)")
    ax.set_title(title, fontsize=10)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> None:
    """Draw every chart the CSV has data for."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    rows = load(args.csv)
    sub = "Llama-3.1-8B, MI300X TP=1, 128 output tokens"
    for length in sorted({r["length"] for r in rows}):
        lr = [r for r in rows if r["length"] == length]
        concs = {c: str(c) for c in sorted({r["concurrency"] for r in lr})}
        name = LABEL.get(length, str(length))
        line_chart(
            lr,
            "concurrency",
            "ttft_p50",
            f"TTFT vs concurrency, {name} prompt ({length} tokens)\n{sub}",
            "concurrency (requests in flight)",
            os.path.join(args.out, f"ttft_L{length}.png"),
            concs,
        )
        line_chart(
            lr,
            "concurrency",
            "total_p50",
            f"Total latency vs concurrency, {name} prompt ({length} tokens)\n{sub}",
            "concurrency (requests in flight)",
            os.path.join(args.out, f"total_L{length}.png"),
            concs,
        )
    c1 = [r for r in rows if r["concurrency"] == 1]
    if c1:
        lengths = {n: LABEL.get(n, str(n)) for n in sorted({r["length"] for r in c1})}
        line_chart(
            c1,
            "length",
            "ttft_p50",
            f"TTFT vs prompt length at concurrency 1\n{sub}",
            "prompt length (tokens)",
            os.path.join(args.out, "ttft_vs_length_c1.png"),
            lengths,
        )
    print(f"charts in {args.out}: {sorted(os.listdir(args.out))}")


if __name__ == "__main__":
    main()
