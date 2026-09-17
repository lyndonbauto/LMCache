#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Generate synthetic request traces for the AIE-101 hit-rate regime measurement.

Emits token-ID traces rather than text. That is deliberate: it needs no
tokenizer, no model download and no network, so the whole measurement is
reproducible from this file alone and a reviewer can rerun it in seconds.

Each workload targets a different point on the axis that decides whether layer
pipelining can pay -- the fraction of a prompt that still needs prefill:

``chat``
    Multi-turn conversation. Each turn replays the entire conversation so far
    and appends a new message, so the cached fraction rises with turn count.
    This is the canonical LMCache workload and it trends toward the
    *complete-hit* regime, where pipelining wins least.

``rag``
    A shared system prompt and document prefix, then a per-request question.
    The cached fraction is set by how much of the prompt is shared prefix.

``blend``
    Scattered reuse: a shared prefix plus chunks drawn from a pool in varying
    order. Approximates the CacheBlend shape, where 10-25% of tokens are
    recomputed on purpose -- which is close to where pipelining peaks.

``cold``
    Unique traffic with no reuse. The negative control.

Usage::

    ./gen_traces.py chat --requests 500 -o traces/chat.jsonl
    ./gen_traces.py rag  --requests 500 -o traces/rag.jsonl
    ./gen_traces.py blend --requests 500 -o traces/blend.jsonl
    ./gen_traces.py cold --requests 500 -o traces/cold.jsonl
"""

from __future__ import annotations

# Standard
from pathlib import Path
from typing import Iterator
import argparse
import json
import random

# Token IDs are arbitrary here; only their identity matters, since LMCache
# hashes them. A wide space keeps accidental collisions between "unrelated"
# content from registering as reuse.
_TOKEN_SPACE = 1 << 30


def _fresh(rng: random.Random, count: int) -> list[int]:
    """Return ``count`` token IDs unlikely to collide with any other content."""
    return [rng.randrange(_TOKEN_SPACE) for _ in range(count)]


def gen_chat(
    rng: random.Random,
    requests: int,
    turns: int,
    system_tokens: int,
    message_tokens: int,
) -> Iterator[list[int]]:
    """Yield multi-turn conversations, each turn replaying its own history.

    Args:
        rng: Seeded source of randomness.
        requests: Total requests to emit, across as many conversations as needed.
        turns: Turns per conversation before starting a new one.
        system_tokens: Length of the system prompt shared by every conversation.
        message_tokens: Tokens added per turn.

    Yields:
        Token IDs per request, in arrival order.
    """
    system = _fresh(rng, system_tokens)
    emitted = 0
    while emitted < requests:
        history = list(system)
        for _ in range(turns):
            if emitted >= requests:
                return
            history.extend(_fresh(rng, message_tokens))
            yield list(history)
            emitted += 1


def gen_rag(
    rng: random.Random,
    requests: int,
    documents: int,
    system_tokens: int,
    document_tokens: int,
    question_tokens: int,
) -> Iterator[list[int]]:
    """Yield a shared system prompt plus one of N documents plus a question.

    Args:
        rng: Seeded source of randomness.
        requests: Requests to emit.
        documents: Size of the document pool. A smaller pool means more reuse.
        system_tokens: Shared system prompt length.
        document_tokens: Length of each document.
        question_tokens: Length of the per-request question.

    Yields:
        Token IDs per request, in arrival order.
    """
    system = _fresh(rng, system_tokens)
    pool = [_fresh(rng, document_tokens) for _ in range(documents)]
    for _ in range(requests):
        document = pool[rng.randrange(documents)]
        yield system + document + _fresh(rng, question_tokens)


def gen_blend(
    rng: random.Random,
    requests: int,
    pool_size: int,
    system_tokens: int,
    fragment_tokens: int,
    fragments: int,
) -> Iterator[list[int]]:
    """Yield a shared prefix plus fragments drawn in varying order.

    Because LMCache's prefix hashing is rolling, reordering fragments destroys
    the prefix match beyond the first differing fragment. That is the point:
    this workload is what a scattered-reuse pattern looks like to a
    prefix-matching cache, and it is why CacheBlend needs its own
    non-contiguous matcher rather than the prefix path.

    Args:
        rng: Seeded source of randomness.
        requests: Requests to emit.
        pool_size: Number of reusable fragments.
        system_tokens: Shared prefix length.
        fragment_tokens: Length of each fragment.
        fragments: Fragments per request.

    Yields:
        Token IDs per request, in arrival order.
    """
    system = _fresh(rng, system_tokens)
    pool = [_fresh(rng, fragment_tokens) for _ in range(pool_size)]
    for _ in range(requests):
        chosen = rng.sample(range(pool_size), k=min(fragments, pool_size))
        tokens = list(system)
        for index in chosen:
            tokens.extend(pool[index])
        yield tokens


def gen_cold(rng: random.Random, requests: int, prompt_tokens: int) -> Iterator[list[int]]:
    """Yield entirely unique prompts, the no-reuse control.

    Args:
        rng: Seeded source of randomness.
        requests: Requests to emit.
        prompt_tokens: Length of each prompt.

    Yields:
        Token IDs per request, in arrival order.
    """
    for _ in range(requests):
        yield _fresh(rng, prompt_tokens)


def write_trace(path: Path, traces: Iterator[list[int]]) -> int:
    """Write requests to ``path`` as JSONL and return how many were written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8") as handle:
        for index, tokens in enumerate(traces):
            handle.write(
                json.dumps(
                    {
                        "request_id": f"req-{index}",
                        "timestamp": float(index),
                        "token_ids": tokens,
                    }
                )
                + "\n"
            )
            count += 1
    return count


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "workload", choices=["chat", "rag", "blend", "cold"], help="Workload shape."
    )
    parser.add_argument("-o", "--output", required=True, help="Destination JSONL.")
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--seed", type=int, default=0, help="For reproducibility.")
    parser.add_argument("--system-tokens", type=int, default=512)
    # chat
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--message-tokens", type=int, default=256)
    # rag
    parser.add_argument("--documents", type=int, default=20)
    parser.add_argument("--document-tokens", type=int, default=4096)
    parser.add_argument("--question-tokens", type=int, default=64)
    # blend
    parser.add_argument("--pool-size", type=int, default=40)
    parser.add_argument("--fragment-tokens", type=int, default=512)
    parser.add_argument("--fragments", type=int, default=8)
    # cold
    parser.add_argument("--prompt-tokens", type=int, default=4096)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    if args.workload == "chat":
        traces = gen_chat(
            rng, args.requests, args.turns, args.system_tokens, args.message_tokens
        )
    elif args.workload == "rag":
        traces = gen_rag(
            rng,
            args.requests,
            args.documents,
            args.system_tokens,
            args.document_tokens,
            args.question_tokens,
        )
    elif args.workload == "blend":
        traces = gen_blend(
            rng,
            args.requests,
            args.pool_size,
            args.system_tokens,
            args.fragment_tokens,
            args.fragments,
        )
    else:
        traces = gen_cold(rng, args.requests, args.prompt_tokens)

    written = write_trace(Path(args.output), traces)
    print(f"Wrote {written} {args.workload} requests to {args.output} (seed {args.seed})")


if __name__ == "__main__":
    main()
