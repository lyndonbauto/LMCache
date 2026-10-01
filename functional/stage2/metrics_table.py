# SPDX-License-Identifier: Apache-2.0
"""Build the T-E2E-10 table for Stage 2a from the hit reports and metrics.

For every session (test x layerwise) and send (cold, warm): requests,
retrieves by ``pipelined_outcome``, the growth of
``lmcache_mp_num_deferred_retrieves_total``, vLLM's external prefix cache
hit rate (hit tokens / queried tokens), LMCache's L1 and L2 hit tokens, and
how many requests matched the expected hit. The session's final
``/metrics`` scrape is checked for any deferred-retrieve sample.

Usage::

    python metrics_table.py /root/lmc-work/functional/stage2
"""

# Standard
import collections
import glob
import json
import os
import sys


def main() -> None:
    root = sys.argv[1]
    print(
        "| Session | Send | Requests | Hit as expected | Exact | Retrieves by "
        "outcome | Deferred counter delta | vLLM external hit rate | "
        "LMCache hit tokens L1 / L2 |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for report_path in sorted(glob.glob(os.path.join(root, "e2e*", "report_*.json"))):
        session = os.path.basename(report_path)[len("report_") : -len(".json")]
        with open(report_path) as f:
            report = json.load(f)
        for tag, rows in report["rows"].items():
            run_path = os.path.join(os.path.dirname(report_path), f"{tag}.json")
            with open(run_path) as f:
                results = json.load(f)["results"]
            sums: collections.Counter = collections.Counter()
            for result in results.values():
                sums.update(result.get("metrics_delta", {}))
            outcomes = collections.Counter(o for r in rows for _, o in r["retrieves"])
            queries = sums["vllm:external_prefix_cache_queries_total"]
            hits = sums["vllm:external_prefix_cache_hits_total"]
            rate = f"{100 * hits / queries:.1f}% ({int(hits)}/{int(queries)})"
            outcome_text = (
                ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())) or "none"
            )
            print(
                f"| {session} | {tag.rsplit('_', 1)[1]} | {len(rows)} | "
                f"{sum(r['vllm_external_hit'] == r['expected_hit'] for r in rows)} | "
                f"{sum(r['exact'] for r in rows)} | {outcome_text} | "
                f"{int(sums['lmcache_mp_num_deferred_retrieves_total'])} | {rate} | "
                f"{int(sums['lmcache_mp_lookup_hit_l1_tokens_total'])} / "
                f"{int(sums['lmcache_mp_lookup_hit_l2_tokens_total'])} |"
            )
    print()
    for metrics_path in sorted(glob.glob(os.path.join(root, "e2e*", "metrics_*.txt"))):
        with open(metrics_path) as f:
            deferred = [
                line.strip()
                for line in f
                if line.startswith("lmcache_mp_num_deferred_retrieves_total")
            ]
        print(
            f"- `{os.path.relpath(metrics_path, root)}`: deferred-retrieve samples "
            f"at session end: {deferred or 'none (counter never incremented)'}"
        )


if __name__ == "__main__":
    main()
