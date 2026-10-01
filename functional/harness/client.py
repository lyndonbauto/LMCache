# SPDX-License-Identifier: Apache-2.0
"""Send the corpus to a vLLM server one request at a time and record the output.

Each prompt is sent as token IDs, so its length is exactly what the corpus
says. Requests are sequential (batch size 1). For every prompt the result
holds the completion text, its token IDs, the top-5 logprobs at every
position and the prompt token count vLLM reports.

With ``--metrics-urls``, each result also holds ``metrics_delta``: how much
each counter in ``TRACKED_COUNTERS`` (summed over its labels) grew while that
one request ran, so per-request cache hits can be read without parsing logs.

``--ids`` keeps only the named prompts of the selected sets. With
``--concurrency N`` (N > 1) every selected prompt is sent at once, N in
flight; per-request deltas are then not recorded, and ``meta`` holds the
whole batch's ``batch_metrics_delta`` instead.

Usage::

    python client.py --corpus corpus_llama.json --out baseline_run1.json \
        [--sets P-exact,P-shared] [--ids P-exact-00,P-exact-05] [--salt tenant-a] \
        [--concurrency 16] [--timeout 600] [--url http://localhost:8000] \
        [--metrics-urls http://localhost:8000/metrics,http://localhost:8080/metrics]
"""

# Standard
from typing import Any
import argparse
import concurrent.futures
import json
import time
import urllib.request

SEED = 0

TRACKED_COUNTERS = (
    "vllm:external_prefix_cache_queries_total",
    "vllm:external_prefix_cache_hits_total",
    "vllm:prompt_tokens_total",
    "lmcache_mp_lookup_requested_tokens_total",
    "lmcache_mp_lookup_hit_tokens_total",
    "lmcache_mp_lookup_hit_l1_tokens_total",
    "lmcache_mp_lookup_hit_l2_tokens_total",
    "lmcache_mp_num_submitted_retrieves_total",
    "lmcache_mp_num_finished_retrieves_total",
    "lmcache_mp_num_deferred_retrieves_total",
    "lmcache_mp_l2_prefetch_lookup_requests_total",
    "lmcache_mp_l2_prefetch_hit_chunks_total",
)


def _token_id(token: str) -> int:
    """Parse vLLM's ``token_id:<n>`` token representation."""
    return int(token.split(":", 1)[1])


def scrape_counters(urls: list[str]) -> dict[str, float]:
    """Return every tracked counter, summed over its labels, from all URLs.

    Args:
        urls: Prometheus text endpoints to read.

    Returns:
        ``{counter_name: value}`` for each name in ``TRACKED_COUNTERS``; a
        counter no endpoint exposes yet reads 0.
    """
    totals = dict.fromkeys(TRACKED_COUNTERS, 0.0)
    for url in urls:
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read().decode()
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            name = line.split("{", 1)[0].split(" ", 1)[0]
            if name in totals:
                totals[name] += float(line.rsplit(" ", 1)[1])
    return totals


def complete(
    url: str,
    model: str,
    token_ids: list[int],
    max_tokens: int,
    salt: str,
    timeout_s: float = 900.0,
) -> dict[str, Any]:
    """Run one greedy completion and return its text, tokens and logprobs.

    Args:
        url: Server base URL.
        model: Served model name.
        token_ids: Prompt token IDs.
        max_tokens: Completion length.
        salt: ``cache_salt`` for the request, or "" for none.
        timeout_s: Socket timeout for the request.

    Returns:
        ``text``, ``token_ids``, ``top_logprobs`` (one ``{token_id: logprob}``
        dict per position), ``prompt_tokens``, ``finish_reason``, ``latency_s``
        and ``request_id`` (vLLM's ``cmpl-...`` id, the prefix of the
        session id in LMCache's logs).
    """
    body: dict[str, Any] = {
        "model": model,
        "prompt": token_ids,
        "max_tokens": max_tokens,
        "temperature": 0,
        "seed": SEED,
        "logprobs": 5,
        "return_tokens_as_token_ids": True,
    }
    if salt:
        body["cache_salt"] = salt
    request = urllib.request.Request(
        f"{url}/v1/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        reply = json.load(response)
    latency = time.perf_counter() - start
    choice = reply["choices"][0]
    logprobs = choice["logprobs"]
    return {
        "text": choice["text"],
        "token_ids": [_token_id(t) for t in logprobs["tokens"]],
        "top_logprobs": [
            {str(_token_id(t)): v for t, v in position.items()}
            for position in logprobs["top_logprobs"]
        ],
        "prompt_tokens": reply["usage"]["prompt_tokens"],
        "finish_reason": choice["finish_reason"],
        "latency_s": round(latency, 4),
        "request_id": reply["id"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--sets", default="", help="comma-separated; default all")
    parser.add_argument("--salt", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--ids", default="", help="comma-separated prompt ids")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="send all selected prompts at once with this many in flight",
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument(
        "--metrics-urls",
        default="",
        help="comma-separated Prometheus endpoints; record per-request deltas",
    )
    args = parser.parse_args()
    metrics_urls = [u for u in args.metrics_urls.split(",") if u]
    with open(args.corpus) as f:
        corpus = json.load(f)
    with urllib.request.urlopen(f"{args.url}/v1/models", timeout=30) as response:
        model = json.load(response)["data"][0]["id"]
    wanted = [s for s in args.sets.split(",") if s] or list(corpus["sets"])
    ids = [i for i in args.ids.split(",") if i]
    selected = {
        name: [p for p in corpus["sets"][name] if not ids or p["id"] in ids]
        for name in wanted
    }
    results: dict[str, Any] = {}
    batch_delta: dict[str, float] = {}

    def run_one(prompt: dict[str, Any]) -> dict[str, Any]:
        result = complete(
            args.url,
            model,
            prompt["token_ids"],
            corpus["max_tokens"],
            args.salt,
            args.timeout,
        )
        if result["prompt_tokens"] != prompt["n_tokens"]:
            raise RuntimeError(
                f"{prompt['id']}: server saw {result['prompt_tokens']} prompt "
                f"tokens, corpus has {prompt['n_tokens']}"
            )
        result["correct"] = prompt["expected"] in result["text"]
        return result

    if args.concurrency > 1:
        # Per-request counter deltas are meaningless with requests in flight
        # together, so only the whole batch's delta is recorded (in meta).
        batch = [p for prompts in selected.values() for p in prompts]
        before = scrape_counters(metrics_urls) if metrics_urls else {}
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
            futures = {p["id"]: pool.submit(run_one, p) for p in batch}
            for pid, future in futures.items():
                results[pid] = future.result()
        wall = round(time.perf_counter() - start, 3)
        if metrics_urls:
            after = scrape_counters(metrics_urls)
            batch_delta = {k: after[k] - before[k] for k in after}
        print(f"{len(batch)} requests at concurrency {args.concurrency}: {wall} s")
    for name, prompts in selected.items():
        for prompt in prompts:
            if args.concurrency > 1:
                continue
            before = scrape_counters(metrics_urls) if metrics_urls else {}
            result = run_one(prompt)
            if metrics_urls:
                after = scrape_counters(metrics_urls)
                result["metrics_delta"] = {k: after[k] - before[k] for k in after}
            results[prompt["id"]] = result
        correct = sum(results[p["id"]]["correct"] for p in prompts)
        print(
            f"{name}: {len(prompts)} prompts, answers correct {correct}/{len(prompts)}",
            flush=True,
        )
    meta = {
        "tag": args.tag,
        "model": model,
        "corpus_sha256": corpus["sha256"],
        "salt": args.salt,
        "sets": wanted,
        "ids": ids,
        "concurrency": args.concurrency,
        "batch_metrics_delta": batch_delta,
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(args.out, "w") as f:
        json.dump({"meta": meta, "results": results}, f)


if __name__ == "__main__":
    main()
