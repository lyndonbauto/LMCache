# SPDX-License-Identifier: Apache-2.0
"""Send the corpus to a vLLM server one request at a time and record the output.

Each prompt is sent as token IDs, so its length is exactly what the corpus
says. Requests are sequential (batch size 1). For every prompt the result
holds the completion text, its token IDs, the top-5 logprobs at every
position and the prompt token count vLLM reports.

Usage::

    python client.py --corpus corpus_llama.json --out baseline_run1.json \
        [--sets P-exact,P-shared] [--salt tenant-a] [--url http://localhost:8000]
"""

# Standard
from typing import Any
import argparse
import json
import time
import urllib.request

SEED = 0


def _token_id(token: str) -> int:
    """Parse vLLM's ``token_id:<n>`` token representation."""
    return int(token.split(":", 1)[1])


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
        dict per position), ``prompt_tokens``, ``finish_reason``, ``latency_s``.
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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--sets", default="", help="comma-separated; default all")
    parser.add_argument("--salt", default="")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    with open(args.corpus) as f:
        corpus = json.load(f)
    with urllib.request.urlopen(f"{args.url}/v1/models", timeout=30) as response:
        model = json.load(response)["data"][0]["id"]
    wanted = [s for s in args.sets.split(",") if s] or list(corpus["sets"])
    results: dict[str, Any] = {}
    for name in wanted:
        for prompt in corpus["sets"][name]:
            result = complete(
                args.url, model, prompt["token_ids"], corpus["max_tokens"], args.salt
            )
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
