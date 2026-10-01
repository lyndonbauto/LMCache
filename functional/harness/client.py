# SPDX-License-Identifier: Apache-2.0
"""Send the corpus to a vLLM server one request at a time and record the output.

Each prompt is sent as token IDs, so its length is exactly what the corpus
says. Requests are sequential (batch size 1). For every prompt the result
holds the completion text, its token IDs, the top-5 logprobs at every
position and the prompt token count vLLM reports.

With ``--metrics-urls``, each result also holds ``metrics_delta``: how much
each counter in ``TRACKED_COUNTERS`` (summed over its labels) grew while that
one request ran, so per-request cache hits can be read without parsing logs.

Usage::

    python client.py --corpus corpus_llama.json --out baseline_run1.json \
        [--sets P-exact,P-shared] [--salt tenant-a] [--url http://localhost:8000] \
        [--metrics-urls http://localhost:8000/metrics,http://localhost:8080/metrics]
"""

# Standard
from typing import Any
import argparse
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
    url: str, model: str, token_ids: list[int], max_tokens: int, salt: str
) -> dict[str, Any]:
    """Run one greedy completion and return its text, tokens and logprobs.

    Args:
        url: Server base URL.
        model: Served model name.
        token_ids: Prompt token IDs.
        max_tokens: Completion length.
        salt: ``cache_salt`` for the request, or "" for none.

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
    with urllib.request.urlopen(request, timeout=900) as response:
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
    results: dict[str, Any] = {}
    for name in wanted:
        for prompt in corpus["sets"][name]:
            before = scrape_counters(metrics_urls) if metrics_urls else {}
            result = complete(
                args.url, model, prompt["token_ids"], corpus["max_tokens"], args.salt
            )
            if metrics_urls:
                after = scrape_counters(metrics_urls)
                result["metrics_delta"] = {k: after[k] - before[k] for k in after}
            if result["prompt_tokens"] != prompt["n_tokens"]:
                raise RuntimeError(
                    f"{prompt['id']}: server saw {result['prompt_tokens']} prompt "
                    f"tokens, corpus has {prompt['n_tokens']}"
                )
            result["correct"] = prompt["expected"] in result["text"]
            results[prompt["id"]] = result
        correct = sum(results[p["id"]]["correct"] for p in corpus["sets"][name])
        print(
            f"{name}: {len(corpus['sets'][name])} prompts, answers correct "
            f"{correct}/{len(corpus['sets'][name])}",
            flush=True,
        )
    meta = {
        "tag": args.tag,
        "model": model,
        "corpus_sha256": corpus["sha256"],
        "salt": args.salt,
        "sets": wanted,
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(args.out, "w") as f:
        json.dump({"meta": meta, "results": results}, f)


if __name__ == "__main__":
    main()
