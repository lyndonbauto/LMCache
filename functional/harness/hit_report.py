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

The cache model is scoped by each run's model and ``cache_salt`` (from its
``meta``), so a different model or salt starts from an empty cache.
``--expect TAG:PROMPT_ID=TOKENS`` overrides the modelled hit for one request
(for example after a test deleted chunks from L2). ``--oracle TAG=PATH``
names a reference run (for example vLLM's own prefix cache) that is the
oracle for that send's prefix hits: requests whose expected hit is a proper
prefix (more than 0 and less than ``n_tokens - 1``) are compared with it,
and the rest with the baseline; the Exact cell then says which one.

By default every retrieve must be ``not_deferred`` and the deferred counter
must not move (the plain path). For the pipelined path,
``--outcomes TAG=pipelined,not_deferred`` sets the outcomes a send's
retrieves may have (the deferred counter is then reported, not checked),
and ``--require TAG=pipelined`` also requires every request of that send
with a modelled hit to have at least one retrieve with that outcome. ``*``
as TAG applies to every send. ``--allow-error TAG`` accepts requests that
``client.py --allow-errors`` recorded as failed (fault tests); otherwise
they fail the row. ``--no-hit-check TAG`` reports the hit columns of that
send without checking them (fault tests where vLLM counts a lookup hit
whose load then fails). A baseline applies only
to runs of the model it was recorded with; prompts with no baseline for the
run's model show "n/a" for exactness and are not failed on it. For a
concurrent run (``meta.concurrency`` > 1) the hit and deferred checks use the
batch totals in ``meta.batch_metrics_delta``; exactness and outcomes are
still per request.

Usage::

    python hit_report.py --corpus corpus.json --baseline bi_run1.json \
        [--baseline more.json] [--expect warm:P-prefix-07=512] \
        [--oracle warm=vllm_pc_warm.json] \
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


def remember(stored: set[tuple[int, ...]], tokens: list[int], chunk: int) -> None:
    """Add every full-chunk prefix of ``tokens`` to the cache model.

    Args:
        stored: Token prefixes in cache, updated in place.
        tokens: Prompt token IDs of a request that was just served.
        chunk: Chunk size in tokens.
    """
    for end in range(chunk, len(tokens) + 1, chunk):
        stored.add(tuple(tokens[:end]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--corpus", required=True)
    parser.add_argument(
        "--baseline",
        action="append",
        default=[],
        help="client.py output with the oracle; repeat to merge several",
    )
    parser.add_argument("--lmcache-log", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        help="TAG:PROMPT_ID=TOKENS overrides the modelled hit of one request",
    )
    parser.add_argument(
        "--oracle",
        action="append",
        default=[],
        help="TAG=PATH: oracle for the prefix hits of send TAG",
    )
    parser.add_argument(
        "--outcomes",
        action="append",
        default=[],
        help="TAG=OUTCOME[,OUTCOME...]: outcomes a send's retrieves may have",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        help="TAG=OUTCOME: every request with a hit has a retrieve with it",
    )
    parser.add_argument(
        "--allow-error",
        action="append",
        default=[],
        help="TAG: accept requests recorded as failed in that send",
    )
    parser.add_argument(
        "--no-hit-check",
        action="append",
        default=[],
        help="TAG: report but do not check the hit and deferred columns",
    )
    parser.add_argument("runs", nargs="+", help="client.py outputs in send order")
    args = parser.parse_args()
    allowed_outcomes = {
        tag: set(names.split(","))
        for tag, _, names in (item.partition("=") for item in args.outcomes)
    }
    required_outcome = {
        tag: name for tag, _, name in (item.partition("=") for item in args.require)
    }
    allow_error = set(args.allow_error)
    no_hit_check = set(args.no_hit_check)
    with open(args.corpus) as f:
        corpus = json.load(f)
    # A baseline only applies to runs of the model it was recorded with.
    baseline: dict[tuple[str, str], Any] = {}
    for path in args.baseline:
        with open(path) as f:
            recorded = json.load(f)
        for pid, result in recorded["results"].items():
            baseline[(recorded["meta"]["model"], pid)] = result
    overrides: dict[tuple[str, str], int] = {}
    for item in args.expect:
        key, _, value = item.rpartition("=")
        tag, _, pid = key.partition(":")
        overrides[(tag, pid)] = int(value)
    prefix_oracles: dict[str, tuple[str, dict[str, Any]]] = {}
    for item in args.oracle:
        tag, _, path = item.partition("=")
        with open(path) as f:
            recorded = json.load(f)
        prefix_oracles[tag] = (recorded["meta"]["tag"], recorded["results"])
    chunk = corpus["chunk_size"]
    prompts = {p["id"]: p for s in corpus["sets"].values() for p in s}
    outcomes = retrieve_outcomes(args.lmcache_log)
    # Cache entries are scoped by model and cache_salt.
    stored: dict[tuple[str, str], set[tuple[int, ...]]] = collections.defaultdict(set)
    report: dict[str, Any] = {}
    batches: dict[str, Any] = {}
    totals: collections.Counter[str] = collections.Counter()
    for run_path in args.runs:
        with open(run_path) as f:
            run = json.load(f)
        meta = run["meta"]
        tag = meta["tag"]
        scope = (meta["model"], meta.get("salt", ""))
        concurrent_run = meta.get("concurrency", 1) > 1
        allowed = allowed_outcomes.get(tag, allowed_outcomes.get("*", {"not_deferred"}))
        plain_path = allowed == {"not_deferred"}
        required = required_outcome.get(tag, required_outcome.get("*", ""))
        errors_ok = tag in allow_error or "*" in allow_error
        print(f"\n### {tag} (model {scope[0]}, salt {scope[1] or '-'})\n")
        print(
            "| Prompt | Tokens | Exact | Expected hit | vLLM ext hit | "
            "LMCache hit (L1/L2) | Deferred | Retrieves (chunks: outcome) | OK |"
        )
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        rows = []
        batch_want = 0
        for pid, result in run["results"].items():
            tokens = prompts[pid]["token_ids"]
            want = overrides.get((tag, pid), expected_hit(tokens, stored[scope], chunk))
            batch_want += want
            delta = result.get("metrics_delta", {})
            ext_hit = int(delta.get("vllm:external_prefix_cache_hits_total", -1))
            lmc_hit = int(delta.get("lmcache_mp_lookup_hit_tokens_total", -1))
            l1 = int(delta.get("lmcache_mp_lookup_hit_l1_tokens_total", -1))
            l2 = int(delta.get("lmcache_mp_lookup_hit_l2_tokens_total", -1))
            deferred = int(delta.get("lmcache_mp_num_deferred_retrieves_total", -1))
            retrieves = outcomes.get(result.get("request_id", ""), [])
            error = result.get("error", "")
            exact: bool | None = None
            oracle = "baseline"
            if (
                not error
                and 0 < want < len(tokens) - 1
                and pid in prefix_oracles.get(tag, ("", {}))[1]
            ):
                oracle, results = prefix_oracles[tag]
                exact = result["token_ids"] == results[pid]["token_ids"]
            elif not error and (scope[0], pid) in baseline:
                exact = result["token_ids"] == baseline[(scope[0], pid)]["token_ids"]
            outcome_ok = all(o in allowed for _, o in retrieves)
            if required and want > 0:
                outcome_ok = outcome_ok and any(o == required for _, o in retrieves)
            # Concurrent runs have no per-request counters; their hit and
            # deferred checks are on the batch totals below. A failed request
            # has no counters either, nor has one whose counters a fault took
            # down in a send that allows errors.
            unscraped = (errors_ok and "metrics_delta" not in result) or (
                tag in no_hit_check or "*" in no_hit_check
            )
            hit_ok = concurrent_run or bool(error) or unscraped or ext_hit == want
            deferred_ok = concurrent_run or not plain_path or unscraped or deferred == 0
            ok = exact is not False and hit_ok and deferred_ok and outcome_ok
            if error:
                ok = errors_ok and outcome_ok
            row = {
                "prompt": pid,
                "n_tokens": len(tokens),
                "exact": exact,
                "oracle": oracle,
                "expected_hit": want,
                "vllm_external_hit": ext_hit,
                "lmcache_hit": lmc_hit,
                "lmcache_hit_l1": l1,
                "lmcache_hit_l2": l2,
                "deferred_delta": deferred,
                "retrieves": retrieves,
                "error": error,
                "ok": ok,
            }
            rows.append(row)
            totals[f"{tag}:n"] += 1
            totals[f"{tag}:exact"] += exact is True
            totals[f"{tag}:no_baseline"] += exact is None and not error
            if error:
                totals[f"{tag}:errors"] += 1
            if not concurrent_run:
                totals[f"{tag}:hit_as_expected"] += ext_hit == want
            totals[f"{tag}:ok"] += ok
            for _, outcome in retrieves:
                totals[f"{tag}:outcome={outcome}"] += 1
            retrieve_text = ", ".join(f"{c}: {o}" for c, o in retrieves) or "-"
            exact_text = {True: "yes", False: "NO", None: "n/a"}[exact]
            if oracle != "baseline":
                exact_text += f" ({oracle})"
            if error:
                exact_text = f"ERROR ({error[:60]})"
            print(
                f"| {pid} | {len(tokens)} | {exact_text} | {want} | "
                f"{ext_hit} | {lmc_hit} ({l1}/{l2}) | {deferred} | {retrieve_text} "
                f"| {'yes' if ok else 'NO'} |"
            )
            if not concurrent_run:
                remember(stored[scope], tokens, chunk)
        if concurrent_run:
            # Requests in one concurrent batch cannot hit each other's stores.
            for pid in run["results"]:
                remember(stored[scope], prompts[pid]["token_ids"], chunk)
        batch: dict[str, Any] = {}
        if concurrent_run:
            delta = meta.get("batch_metrics_delta", {})
            ext = int(delta.get("vllm:external_prefix_cache_hits_total", -1))
            deferred = int(delta.get("lmcache_mp_num_deferred_retrieves_total", -1))
            batch = {
                "expected_hit": batch_want,
                "vllm_external_hit": ext,
                "lmcache_hit_l1": int(
                    delta.get("lmcache_mp_lookup_hit_l1_tokens_total", -1)
                ),
                "lmcache_hit_l2": int(
                    delta.get("lmcache_mp_lookup_hit_l2_tokens_total", -1)
                ),
                "deferred_delta": deferred,
                "ok": ext == batch_want and (not plain_path or deferred == 0),
            }
            totals[f"{tag}:batch_ok"] += batch["ok"]
            print(
                f"\nBatch of {len(rows)} at concurrency {meta['concurrency']}: "
                f"expected hit {batch_want}, vLLM ext hit {ext}, LMCache L1/L2 "
                f"{batch['lmcache_hit_l1']}/{batch['lmcache_hit_l2']}, deferred "
                f"{deferred}, {'OK' if batch['ok'] else 'NOT OK'}"
            )
        report[tag] = rows
        batches[tag] = batch
    print("\nTotals: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(
                {"rows": report, "batches": batches, "totals": dict(totals)},
                f,
                indent=1,
            )


if __name__ == "__main__":
    main()
