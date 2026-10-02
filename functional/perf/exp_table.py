# SPDX-License-Identifier: Apache-2.0
"""Summarize the lw investigation experiment points (sessions ``E_<mode>``).

Reads ``<dir>/E_*/E_<mode>_[P<pre>_]L<len>_c<c>.json`` (perf_client.py output)
and the matching ``outcomes_*.txt``, and prints one markdown table row per
point: mode, cached prefix, new tokens, c, n, TTFT p50 and mean, total p50,
hit tokens against expected, outcome mix and errors.

Expected hit tokens are ``n * (len - 1)`` for a full hit (vLLM computes the
last token itself) and ``n * pre`` for a partial hit.

Usage::

    python exp_table.py --dir /root/lmc-work/functional/perf
"""

# Standard
import argparse
import json
import os
import re

# Third Party
import numpy as np

NAME_RE = re.compile(r"^E_(\w+?)_(?:P(\d+)_)?L(\d+)_c(\d+)\.json$")


def rows(root: str) -> list[dict]:
    """Return one dict per experiment point found under ``root``."""
    out: list[dict] = []
    for sess in sorted(os.listdir(root)):
        d = os.path.join(root, sess)
        if not sess.startswith("E_") or not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            m = NAME_RE.match(f)
            if not m or m.group(1) != sess[2:]:
                continue
            with open(os.path.join(d, f)) as fh:
                p = json.load(fh)
            pre, length, c = int(m.group(2) or 0), int(m.group(3)), int(m.group(4))
            ok = [r for r in p["requests"] if not r["error"]]
            ttft = np.array([r["ttft_s"] for r in ok]) if ok else np.array([np.nan])
            total = np.array([r["total_s"] for r in ok]) if ok else np.array([np.nan])
            delta = p["metrics_delta"]
            hit = int(delta.get("vllm:external_prefix_cache_hits_total", 0))
            n = p["n"]
            mode = sess[2:]
            expected = n * pre if pre else n * (length - 1)
            if mode == "nocache":
                expected = 0
            of = os.path.join(d, "outcomes_" + f[: -len(".json")] + ".txt")
            mix = ""
            if os.path.exists(of):
                with open(of) as fh:
                    mix = fh.read().strip()
            out.append(
                {
                    "mode": mode,
                    "pre": pre,
                    "new": length if pre else 0,
                    "len": length,
                    "c": c,
                    "n": n,
                    "ttft_p50": float(np.percentile(ttft, 50)),
                    "ttft_mean": float(np.mean(ttft)),
                    "total_p50": float(np.percentile(total, 50)),
                    "hit": hit,
                    "expected": expected,
                    "mix": mix,
                    "errors": len(p["requests"]) - len(ok),
                }
            )
    return out


def main() -> None:
    """Print the markdown table."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dir", default="/root/lmc-work/functional/perf")
    args = ap.parse_args()
    print(
        "| mode | cached | new | c | n | TTFT p50 | TTFT mean | total p50 "
        "| hit / expected | outcomes | errors |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows(args.dir), key=lambda r: (r["pre"], r["c"], r["mode"])):
        cached = r["pre"] if r["pre"] else f"{r['len']} (full)"
        print(
            f"| {r['mode']} | {cached} | {r['new']} | {r['c']} | {r['n']} "
            f"| {r['ttft_p50']:.3f} | {r['ttft_mean']:.3f} | {r['total_p50']:.3f} "
            f"| {r['hit']} / {r['expected']} | {r['mix']} | {r['errors']} |"
        )


if __name__ == "__main__":
    main()
