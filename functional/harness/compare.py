# SPDX-License-Identifier: Apache-2.0
"""Compare two client runs prompt by prompt (determinism check and the oracle).

For each prompt: exact token equality, the first position where the token
IDs differ, whether the first generated token's top-1 agrees, the largest
first-position logprob difference over tokens both runs ranked in their top
5, and whether each run answered correctly.

Usage::

    python compare.py run_a.json run_b.json [--out comparison.json]

Prints a markdown table per set and writes per-prompt rows to --out.
"""

# Standard
from typing import Any
import argparse
import json


def compare_prompt(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Return the comparison of one prompt's two results."""
    ta, tb = a["token_ids"], b["token_ids"]
    divergence = next(
        (i for i, (x, y) in enumerate(zip(ta, tb, strict=False)) if x != y), None
    )
    if divergence is None and len(ta) != len(tb):
        divergence = min(len(ta), len(tb))
    first_a, first_b = a["top_logprobs"][0], b["top_logprobs"][0]
    shared = set(first_a) & set(first_b)
    return {
        "exact": ta == tb,
        "first_divergence": divergence,
        "top1_first_token_agrees": ta[:1] == tb[:1],
        "first_token_max_abs_dlogprob": (
            round(max(abs(first_a[t] - first_b[t]) for t in shared), 4)
            if shared
            else None
        ),
        "a_correct": a["correct"],
        "b_correct": b["correct"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("run_a")
    parser.add_argument("run_b")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    with open(args.run_a) as f:
        a = json.load(f)
    with open(args.run_b) as f:
        b = json.load(f)
    if a["meta"]["corpus_sha256"] != b["meta"]["corpus_sha256"]:
        raise SystemExit("the two runs used different corpora")
    rows = {
        pid: compare_prompt(a["results"][pid], b["results"][pid])
        for pid in a["results"]
        if pid in b["results"]
    }
    print(f"A = {a['meta']['tag']}, B = {b['meta']['tag']}\n")
    print(
        "| Set | Prompts | Exact match | Top-1 first token | Divergence position "
        "(min / median) | Max first-token dlogprob | Correct A / B |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- |")
    by_set: dict[str, list[dict[str, Any]]] = {}
    for pid, row in rows.items():
        by_set.setdefault(pid.rsplit("-", 1)[0], []).append(row)
    total = {"n": 0, "exact": 0, "top1": 0}
    for name, set_rows in by_set.items():
        n = len(set_rows)
        exact = sum(r["exact"] for r in set_rows)
        top1 = sum(r["top1_first_token_agrees"] for r in set_rows)
        divs = sorted(r["first_divergence"] for r in set_rows if not r["exact"])
        div_text = f"{divs[0]} / {divs[len(divs) // 2]}" if divs else "-"
        dlp = [
            r["first_token_max_abs_dlogprob"]
            for r in set_rows
            if r["first_token_max_abs_dlogprob"] is not None
        ]
        print(
            f"| {name} | {n} | {exact}/{n} | {top1}/{n} | {div_text} | "
            f"{max(dlp) if dlp else '-'} | {sum(r['a_correct'] for r in set_rows)}/"
            f"{sum(r['b_correct'] for r in set_rows)} |"
        )
        total["n"] += n
        total["exact"] += exact
        total["top1"] += top1
    print(
        f"\nTotal: exact {total['exact']}/{total['n']}, "
        f"top-1 first token {total['top1']}/{total['n']}"
    )
    if args.out:
        with open(args.out, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
