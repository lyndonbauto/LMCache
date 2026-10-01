# SPDX-License-Identifier: Apache-2.0
"""Top-1 agreement of client runs with a baseline (T-E2E-09's second oracle).

For every request of every run that has a baseline entry (same prompt id),
position ``i`` agrees when the run's top-1 token at ``i`` equals the
baseline's top-1 token at ``i``. Greedy decoding makes the top-1 token the
generated token, so ``token_ids`` is compared position by position over the
longer of the two sequences; a position only one side has counts as a
disagreement. Positions after a first divergence are compared too (their
contexts differ, so this is the strict reading of the plan's rule). The pass
rule is ``agreeing / compared >= --threshold`` (default 0.999, plan 5.6) over
all runs together. For the positions before the first divergence the report
also gives the largest absolute difference of the top-1 logprob, which shows
how close the two runs' numerics were.

Failed requests (``client.py --allow-errors``) and requests with no baseline
are listed and left out of the ratio.

Usage::

    python logprob_agree.py --baseline bi_run1.json [--baseline more.json] \
        [--threshold 0.999] [--out agree.json] run1.json [run2.json ...]

Prints one markdown row per run and a final verdict line; exits 0 always
(the verdict is in the last line and in ``--out``).
"""

# Standard
from typing import Any
import argparse
import json


def agreement(run: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Compare one request's result with its baseline.

    Args:
        run: A ``client.py`` result with ``token_ids`` and ``top_logprobs``.
        base: The baseline result for the same prompt.

    Returns:
        ``compared`` (positions), ``agreeing``, ``first_divergence`` (index,
        or ``None`` if the sequences are equal) and ``max_abs_dlogprob``
        (largest top-1 logprob difference before the first divergence, or
        ``None`` if there is no such position).
    """
    ta, tb = run["token_ids"], base["token_ids"]
    compared = max(len(ta), len(tb))
    agreeing = sum(1 for x, y in zip(ta, tb, strict=False) if x == y)
    divergence = next(
        (i for i, (x, y) in enumerate(zip(ta, tb, strict=False)) if x != y), None
    )
    if divergence is None and len(ta) != len(tb):
        divergence = min(len(ta), len(tb))
    upto = min(len(ta), len(tb)) if divergence is None else divergence
    diffs = [
        abs(run["top_logprobs"][i][str(ta[i])] - base["top_logprobs"][i][str(tb[i])])
        for i in range(upto)
        if str(ta[i]) in run["top_logprobs"][i]
        and str(tb[i]) in base["top_logprobs"][i]
    ]
    return {
        "compared": compared,
        "agreeing": agreeing,
        "first_divergence": divergence,
        "max_abs_dlogprob": round(max(diffs), 5) if diffs else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--baseline", action="append", required=True)
    parser.add_argument("--threshold", type=float, default=0.999)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    baseline: dict[str, Any] = {}
    for path in args.baseline:
        with open(path) as f:
            baseline.update(json.load(f)["results"])

    rows: dict[str, Any] = {}
    total_compared = total_agreeing = 0
    print(
        "| run | requests | exact | positions | agreeing | ratio "
        "| max top-1 dlogprob | no baseline | errors |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for path in args.runs:
        with open(path) as f:
            results = json.load(f)["results"]
        per: dict[str, Any] = {}
        missing, errors = [], []
        for pid, res in results.items():
            if "error" in res:
                errors.append(pid)
                continue
            if pid not in baseline:
                missing.append(pid)
                continue
            per[pid] = agreement(res, baseline[pid])
        compared = sum(r["compared"] for r in per.values())
        agreeing = sum(r["agreeing"] for r in per.values())
        exact = sum(r["first_divergence"] is None for r in per.values())
        dl = [
            r["max_abs_dlogprob"]
            for r in per.values()
            if r["max_abs_dlogprob"] is not None
        ]
        total_compared += compared
        total_agreeing += agreeing
        rows[path] = {"requests": per, "no_baseline": missing, "errors": errors}
        ratio = agreeing / compared if compared else 0.0
        name = path.rsplit("/", 1)[-1]
        print(
            f"| {name} | {len(per)} | {exact}/{len(per)} | {compared} | {agreeing} "
            f"| {ratio:.5f} | {max(dl) if dl else '-'} | {len(missing)} "
            f"| {len(errors)} |"
        )
    ratio = total_agreeing / total_compared if total_compared else 0.0
    verdict = "PASS" if total_compared and ratio >= args.threshold else "FAIL"
    if args.out:
        with open(args.out, "w") as f:
            json.dump(
                {
                    "threshold": args.threshold,
                    "ratio": ratio,
                    "verdict": verdict,
                    "runs": rows,
                },
                f,
            )
    print(
        f"top-1 agreement {total_agreeing}/{total_compared} = {ratio:.5f} "
        f"(threshold {args.threshold}): {verdict}"
    )


if __name__ == "__main__":
    main()
