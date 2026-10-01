# SPDX-License-Identifier: Apache-2.0
"""Check per-request cache hits and outputs of one LMCache session.

Replays the session's sends in order against a model of the cache: every
full chunk of every prompt already sent is stored (by its token prefix), so
a prompt's expected hit is its longest run of stored full chunks. vLLM must
compute at least one token, so a hit covering the whole prompt shows up as
``n_tokens - 1``. For each request the report shows token equality with the
baseline, the expected hit, vLLM's external prefix cache hit tokens and
LMCache's lookup hit tokens (both from ``client.py --metrics-urls``), the
deferred-retrieve counter delta and the ``pipelined_outcome`` of the
request's ``MP retrieve end`` log lines.

Usage::

    python hit_report.py --corpus corpus.json --baseline bi_run1.json \
        --lmcache-log lmcache_tag.log --out report.json cold.json warm.json

Prints a markdown table per send and a totals line; exits 0 always (the
verdict is in the ``ok`` fields and the totals).
"""

# Standard
from typing import Any
import argparse
import collections
import json
import re

OUTCOME_RE = re.compile(
    r"MP retrieve end: session=(\S+) .*?retrieved_count=(\d+) "
    r"pipelined_outcome=([a-z_]+)"
)


def retrieve_outcomes(log_path: str) -> dict[str, list[tuple[int, str]]]:
    """Map each vLLM request id to its retrieves' (chunk count, outcome).

    Args:
        log_path: LMCache server log at DEBUG level.

    Returns:
        ``{request_id: [(retrieved_count, pipelined_outcome), ...]}``. The
        session id in the log is ``<request_id>-<n>-<suffix>``.
    """
    outcomes: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    with open(log_path, errors="replace") as f:
        for line in f:
            match = OUTCOME_RE.search(line)
            if match:
                request_id = match.group(1).rsplit("-", 2)[0]
                outcomes[request_id].append((int(match.group(2)), match.group(3)))
    return outcomes


def expected_hit(tokens: list[int], stored: set[tuple[int, ...]], chunk: int) -> int:
    """Return the hit in tokens that vLLM should report for one prompt.

    Args:
        tokens: Prompt token IDs.
        stored: Token prefixes (each a multiple of ``chunk`` long) in cache.
        chunk: Chunk size in tokens.

    Returns:
        Tokens covered by the longest run of stored full chunks, capped at
        ``len(tokens) - 1``.
    """
    hit = 0
    while hit + chunk <= len(tokens) and tuple(tokens[: hit + chunk]) in stored:
        hit += chunk
    return min(hit, len(tokens) - 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--lmcache-log", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("runs", nargs="+", help="client.py outputs in send order")
    args = parser.parse_args()
    with open(args.corpus) as f:
        corpus = json.load(f)
    with open(args.baseline) as f:
        baseline = json.load(f)["results"]
    chunk = corpus["chunk_size"]
    prompts = {p["id"]: p for s in corpus["sets"].values() for p in s}
    outcomes = retrieve_outcomes(args.lmcache_log)
    stored: set[tuple[int, ...]] = set()
    report: dict[str, Any] = {}
    totals = collections.Counter()
    for run_path in args.runs:
        with open(run_path) as f:
            run = json.load(f)
        tag = run["meta"]["tag"]
        print(f"\n### {tag}\n")
        print(
            "| Prompt | Tokens | Exact | Expected hit | vLLM ext hit | "
            "LMCache hit (L1/L2) | Deferred | Retrieves (chunks: outcome) | OK |"
        )
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        rows = []
        for pid, result in run["results"].items():
            tokens = prompts[pid]["token_ids"]
            want = expected_hit(tokens, stored, chunk)
            delta = result.get("metrics_delta", {})
            ext_hit = int(delta.get("vllm:external_prefix_cache_hits_total", -1))
            lmc_hit = int(delta.get("lmcache_mp_lookup_hit_tokens_total", -1))
            l1 = int(delta.get("lmcache_mp_lookup_hit_l1_tokens_total", -1))
            l2 = int(delta.get("lmcache_mp_lookup_hit_l2_tokens_total", -1))
            deferred = int(delta.get("lmcache_mp_num_deferred_retrieves_total", -1))
            retrieves = outcomes.get(result.get("request_id", ""), [])
            exact = result["token_ids"] == baseline[pid]["token_ids"]
            outcome_ok = all(o == "not_deferred" for _, o in retrieves)
            ok = exact and ext_hit == want and deferred == 0 and outcome_ok
            row = {
                "prompt": pid,
                "n_tokens": len(tokens),
                "exact": exact,
                "expected_hit": want,
                "vllm_external_hit": ext_hit,
                "lmcache_hit": lmc_hit,
                "lmcache_hit_l1": l1,
                "lmcache_hit_l2": l2,
                "deferred_delta": deferred,
                "retrieves": retrieves,
                "ok": ok,
            }
            rows.append(row)
            totals[f"{tag}:n"] += 1
            totals[f"{tag}:exact"] += exact
            totals[f"{tag}:hit_as_expected"] += ext_hit == want
            totals[f"{tag}:ok"] += ok
            for _, outcome in retrieves:
                totals[f"{tag}:outcome={outcome}"] += 1
            retrieve_text = ", ".join(f"{c}: {o}" for c, o in retrieves) or "-"
            print(
                f"| {pid} | {len(tokens)} | {'yes' if exact else 'NO'} | {want} | "
                f"{ext_hit} | {lmc_hit} ({l1}/{l2}) | {deferred} | {retrieve_text} "
                f"| {'yes' if ok else 'NO'} |"
            )
            for end in range(chunk, len(tokens) + 1, chunk):
                stored.add(tuple(tokens[:end]))
        report[tag] = rows
    print("\nTotals: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"rows": report, "totals": dict(totals)}, f, indent=1)


if __name__ == "__main__":
    main()
