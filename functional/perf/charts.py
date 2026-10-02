# SPDX-License-Identifier: Apache-2.0
"""Draw the perf sweep's charts from results.csv (aggregate.py output).

Writes, under ``--out``:

- ``ttft_L<len>.png``: TTFT p50 vs concurrency, one line per mode;
- ``total_L<len>.png``: total latency p50 vs concurrency, one line per mode;
- ``ttft_vs_length_c1.png`` and ``ttft_vs_length_c32.png``: TTFT p50 vs
  prompt length at concurrency 1 and 32;
- ``speedup_c1.png``: nocache TTFT p50 / mode TTFT p50 at concurrency 1, by
  prompt length, for each cached mode.

INVALID points are drawn as hollow red-edged markers and left out of the line;
rows that were not run (empty metrics) are skipped.
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
from matplotlib.ticker import FuncFormatter  # noqa: E402

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
        "label": "Aerospike disk, layer-by-layer (Soft-RoCE, default 5 s wait)",
    },
}
WAIT_COLORS = ("#ff7f0e", "#9467bd", "#8c564b", "#e377c2")
LABEL = {8192: "8k", 16384: "16k", 32768: "32k", 65536: "64k", 130816: "128k"}


def add_wait_styles(modes: set[str]) -> None:
    """Add a STYLE entry for every lw_wait<secs> mode present."""
    waits = sorted(int(m[len("lw_wait") :]) for m in modes if m.startswith("lw_wait"))
    for i, w in enumerate(waits):
        STYLE[f"lw_wait{w}"] = {
            "color": WAIT_COLORS[i % len(WAIT_COLORS)],
            "marker": "v",
            "label": f"Aerospike disk, layer-by-layer (Soft-RoCE, {w} s wait)",
        }


def load(path: str) -> list[dict]:
    """Return results.csv rows that have metrics, numeric fields converted."""
    with open(path) as f:
        rows = [r for r in csv.DictReader(f) if r["ttft_p50"] != ""]
    for r in rows:
        for k in ("length", "concurrency", "n", "valid"):
            r[k] = int(r[k])
        for k in ("ttft_p50", "total_p50", "ttft_p90", "total_p90"):
            r[k] = float(r[k])
    return rows


def speedup_rows(rows: list[dict], conc: int) -> list[dict]:
    """Return one row per (cached mode, length) with ``speedup`` at ``conc``.

    The speedup is nocache TTFT p50 / mode TTFT p50; a row is valid only if
    both points are.
    """
    base = {
        r["length"]: r
        for r in rows
        if r["mode"] == "nocache" and r["concurrency"] == conc
    }
    out: list[dict] = []
    for r in rows:
        b = base.get(r["length"])
        if r["mode"] == "nocache" or r["concurrency"] != conc or b is None:
            continue
        s = dict(r)
        s["speedup"] = b["ttft_p50"] / r["ttft_p50"]
        s["valid"] = int(r["valid"] and b["valid"])
        out.append(s)
    return out


def line_chart(
    rows: list[dict],
    xkey: str,
    ykey: str,
    title: str,
    xlabel: str,
    path: str,
    xticks: dict[int, str],
    ylabel: str = "",
) -> None:
    """Plot ``ykey`` against ``xkey`` per mode and save a small PNG."""
    fig, ax = plt.subplots(figsize=(6.4, 4.8), dpi=100)
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
    plain = FuncFormatter(lambda v, _: f"{v:g}")
    ax.yaxis.set_major_formatter(plain)
    ax.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
    ax.set_xlabel(xlabel)
    what = {"ttft_p50": "TTFT", "total_p50": "total latency"}.get(ykey, ykey)
    ax.set_ylabel(ylabel or f"{what} p50 (s, log scale)")
    if ykey == "speedup":
        ax.axhline(1.0, color="black", linewidth=0.8, linestyle="--")
    ax.set_title(title, fontsize=10)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=2)
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
    add_wait_styles({r["mode"] for r in rows})
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
    for conc in (1, 32):
        cr = [r for r in rows if r["concurrency"] == conc]
        if not cr:
            continue
        lengths = {n: LABEL.get(n, str(n)) for n in sorted({r["length"] for r in cr})}
        line_chart(
            cr,
            "length",
            "ttft_p50",
            f"TTFT vs prompt length at concurrency {conc}\n{sub}",
            "prompt length (tokens)",
            os.path.join(args.out, f"ttft_vs_length_c{conc}.png"),
            lengths,
        )
    sp = speedup_rows(rows, 1)
    if sp:
        lengths = {n: LABEL.get(n, str(n)) for n in sorted({r["length"] for r in sp})}
        line_chart(
            sp,
            "length",
            "speedup",
            f"TTFT speedup vs no cache at concurrency 1 (above 1 = faster)\n{sub}",
            "prompt length (tokens)",
            os.path.join(args.out, "speedup_c1.png"),
            lengths,
            ylabel="nocache TTFT p50 / mode TTFT p50 (log scale)",
        )
    print(f"charts in {args.out}: {sorted(os.listdir(args.out))}")


if __name__ == "__main__":
    main()
