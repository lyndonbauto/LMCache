# SPDX-License-Identifier: Apache-2.0
"""Closed-loop streaming load client for the LMCache + Aerospike perf check.

Sends ``n`` prompts of exactly ``--length`` tokens to vLLM's OpenAI
``/v1/completions`` endpoint with ``stream=True``, keeping exactly
``--concurrency`` requests in flight, and records per request:

- ``ttft_s``: first streamed token time minus send time;
- ``total_s``: last streamed token time minus send time;
- ``out_tokens``: completion tokens from the final usage chunk.

Prompts are token-ID lists (so their length is exact): prompt ``i`` of
length ``L`` is ``L`` tokens drawn from ``[1000, 128000)`` by a NumPy
generator seeded with ``L * 1009 + i``, with the first token replaced by a
value unique to ``(L, i)``, so no two prompts share a prefix. The same
``(L, i)`` always gives the same prompt, in every mode.

Prometheus counters from ``--metrics-urls`` are scraped before and after the
point and their deltas stored (vLLM's external prefix cache hit tokens,
LMCache's deferred retrieves by outcome).

Usage::

    python perf_client.py --length 8192 --ids 0-3 --concurrency 1 \\
        --out point.json --metrics-urls http://127.0.0.1:8000/metrics
    python perf_client.py --warmup "Warm-up 1" --out warm.json

Output JSON schema::

    {"tag": str, "length": int, "concurrency": int, "n": int, "wall_s": float,
     "requests": [{"id": int, "ttft_s": float, "total_s": float,
                   "out_tokens": int, "error": str}],
     "metrics_delta": {"<metric>{<labels>}": float}}
"""

# Standard
import argparse
import asyncio
import json
import re
import sys
import time

# Third Party
import aiohttp
import numpy as np

MODEL = "meta-llama/Llama-3.1-8B-Instruct"
LENGTH_INDEX = {8192: 0, 16384: 1, 32768: 2, 65536: 3, 130816: 4}
METRIC_PREFIXES = (
    "vllm:external_prefix_cache_hits_total",
    "vllm:external_prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "lmcache_mp_num_deferred_retrieves_total",
)
SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN)")
LABEL_DROP_RE = re.compile(r'(engine|model_name|job|instance)="[^"]*",?')


def prompt_tokens(length: int, idx: int) -> list[int]:
    """Return the token IDs of prompt ``idx`` of a given length.

    Args:
        length: Prompt length in tokens; must be a key of ``LENGTH_INDEX``.
        idx: Prompt index, 0 to 63.

    Returns:
        ``length`` token IDs whose first token is unique to ``(length, idx)``.

    Raises:
        ValueError: If the length is not one of the sweep's or idx is out of range.
    """
    if length not in LENGTH_INDEX:
        raise ValueError(f"length {length} is not one of {sorted(LENGTH_INDEX)}")
    if not 0 <= idx < 64:
        raise ValueError(f"prompt index {idx} out of range 0..63")
    rng = np.random.default_rng(length * 1009 + idx)
    toks = rng.integers(1000, 128000, size=length)
    toks[0] = 500 + LENGTH_INDEX[length] * 64 + idx
    return [int(t) for t in toks]


def parse_ids(spec: str) -> list[int]:
    """Parse ``"0-3"`` or ``"0,2,5"`` (or a mix) into a list of indices.

    Args:
        spec: Comma-separated indices or inclusive ranges.

    Returns:
        The indices in the order given.
    """
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


async def scrape(session: aiohttp.ClientSession, urls: list[str]) -> dict[str, float]:
    """Return the selected Prometheus samples from every URL, summed by name+labels.

    Engine/model labels are dropped so samples from one source sum together.
    An unreachable URL contributes nothing.
    """
    samples: dict[str, float] = {}
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                text = await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
        for line in text.splitlines():
            if not line.startswith(METRIC_PREFIXES):
                continue
            m = SAMPLE_RE.match(line)
            if not m or m.group(3) == "NaN":
                continue
            labels = LABEL_DROP_RE.sub("", m.group(2) or "").replace(",}", "}")
            key = m.group(1) + ("" if labels in ("", "{}") else labels)
            samples[key] = samples.get(key, 0.0) + float(m.group(3))
    return samples


async def one_request(
    session: aiohttp.ClientSession,
    url: str,
    prompt: list[int] | str,
    max_tokens: int,
    timeout: float,
) -> dict[str, float | int | str]:
    """Send one streaming completion and time it.

    Returns:
        ``ttft_s``, ``total_s``, ``out_tokens`` and ``error`` ("" on success).
    """
    body = {
        "model": MODEL,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    t_first = t_last = 0.0
    out_tokens = 0
    error = ""
    try:
        async with session.post(
            f"{url}/v1/completions",
            json=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status != 200:
                error = f"HTTP {r.status}: {(await r.text())[:300]}"
            else:
                async for raw in r.content:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("usage"):
                        out_tokens = int(chunk["usage"].get("completion_tokens", 0))
                    choices = chunk.get("choices") or []
                    if choices and (
                        choices[0].get("text")
                        or choices[0].get("finish_reason") is None
                    ):
                        now = time.perf_counter()
                        if t_first == 0.0:
                            t_first = now
                        t_last = now
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        error = f"{type(e).__name__}: {e}"[:300]
    if not error and t_first == 0.0:
        error = "no token streamed"
    return {
        "ttft_s": t_first - t0 if t_first else -1.0,
        "total_s": t_last - t0 if t_last else -1.0,
        "out_tokens": out_tokens,
        "error": error,
    }


async def run(args: argparse.Namespace) -> dict:
    """Run one point (or one warm-up request) and return the result dict."""
    metrics_urls = [u for u in args.metrics_urls.split(",") if u]
    conn = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=conn) as session:
        if args.warmup:
            prompt: list[int] | str = args.warmup
            if args.warmup_tokens:
                # Fixed and unrelated to every sweep prompt (first token 900).
                rng = np.random.default_rng(7)
                toks = rng.integers(1000, 128000, size=args.warmup_tokens)
                toks[0] = 900
                prompt = [int(t) for t in toks]
            res = await one_request(session, args.url, prompt, 16, args.timeout)
            return {"tag": args.tag, "warmup": args.warmup, "requests": [res]}
        ids = parse_ids(args.ids)
        prompts = {i: prompt_tokens(args.length, i) for i in ids}
        queue: asyncio.Queue[int] = asyncio.Queue()
        for i in ids:
            queue.put_nowait(i)
        results: list[dict] = []

        async def worker() -> None:
            while True:
                try:
                    i = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                res = await one_request(
                    session, args.url, prompts[i], args.max_tokens, args.timeout
                )
                res["id"] = i
                results.append(res)

        before = await scrape(session, metrics_urls)
        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        wall = time.perf_counter() - t0
        # Counters are updated when a request finishes; give them a moment.
        await asyncio.sleep(1.0)
        after = await scrape(session, metrics_urls)
        delta = {k: after[k] - before.get(k, 0.0) for k in after}
        return {
            "tag": args.tag,
            "length": args.length,
            "concurrency": args.concurrency,
            "n": len(ids),
            "ids": ids,
            "max_tokens": args.max_tokens,
            "wall_s": wall,
            "requests": sorted(results, key=lambda r: r["id"]),
            "metrics_delta": delta,
        }


def main() -> None:
    """Parse arguments, run, write the JSON and print a one-line summary."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--length", type=int, default=8192)
    p.add_argument("--ids", default="0-3")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--timeout", type=float, default=3600.0)
    p.add_argument("--metrics-urls", default="")
    p.add_argument("--tag", default="")
    p.add_argument(
        "--warmup", default="", help="send this short text prompt once instead"
    )
    p.add_argument(
        "--warmup-tokens",
        type=int,
        default=0,
        help="with --warmup: send a fixed prompt of this many token IDs instead",
    )
    p.add_argument("--out", required=True)
    args = p.parse_args()
    result = asyncio.run(run(args))
    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)
    reqs = result["requests"]
    errs = [r for r in reqs if r["error"]]
    ok = [r for r in reqs if not r["error"]]
    if args.warmup:
        print(f"warmup {args.tag}: {'ok' if not errs else errs[0]['error']}")
    else:
        ttft = sorted(r["ttft_s"] for r in ok)
        p50 = ttft[len(ttft) // 2] if ttft else -1
        hits = result["metrics_delta"].get("vllm:external_prefix_cache_hits_total", -1)
        print(
            f"point {args.tag}: n={len(reqs)} errors={len(errs)} "
            f"wall={result['wall_s']:.2f}s "
            f"ttft_p50~{p50:.3f}s ext_hit_tokens={hits:.0f} "
            f"out_tokens={sorted({r['out_tokens'] for r in ok})}"
        )
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
