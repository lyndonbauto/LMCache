# SPDX-License-Identifier: Apache-2.0
"""Add the T-LKP-03 prefix-semantics prompts (set P-prefix) to a corpus.

Every P-prefix prompt is derived from one stored prompt B of 6 full chunks
plus a partial seventh (by default P-ragged-11: 6 x 256 + 99 tokens), so the
hit each one should get is known exactly:

- ``P-prefix-00``: B itself (stored first; sent again it hits all 6 chunks).
- ``P-prefix-01..04``: B with one token changed in chunk k = 0, 1, 3, 5, so
  only the first k chunks match B (hits of 0, 1, 3 and 5 chunks).
- ``P-prefix-05``: B2, B with a different token changed in chunk 0, so it
  shares nothing with B or the variants above.
- ``P-prefix-06``, ``P-prefix-07``: B2 cut to 2 and 3 full chunks plus
  ``--tail`` tokens. Sending 06, then 07, then 05 stores B2's chunks
  0-1, then 2, then 3-5 in separate sends, so the L2 records of chunk 2 alone
  can be found (``l2_keys.py``) and deleted to make a gap.

With a tail below ``chunk_size - max_tokens``, no prompt here completes a
chunk during decode.

Usage::

    python prefix_corpus.py --corpus corpus_v2.json --out corpus_stage2b.json
"""

# Standard
from typing import Any
import argparse
import hashlib
import json


def _changed(tokens: list[int], position: int, skip: int) -> list[int]:
    """Return ``tokens`` with the token at ``position`` replaced.

    The replacement is the ``skip``-th distinct later token of the prompt
    that differs from the original, so it is a real token of the same text.
    """
    original = tokens[position]
    candidates: list[int] = []
    for token in tokens[position + 1 :]:
        if token != original and token not in candidates:
            candidates.append(token)
        if len(candidates) > skip:
            break
    out = list(tokens)
    out[position] = candidates[skip]
    return out


def prefix_prompts(base: dict[str, Any], chunk: int, tail: int) -> list[dict]:
    """Build the P-prefix set from base prompt B.

    Args:
        base: A corpus prompt with at least 6 full chunks.
        chunk: Chunk size in tokens.
        tail: Tokens after the last full chunk in the cut-down B2 prompts.

    Returns:
        The P-prefix prompts, ids ``P-prefix-00`` to ``P-prefix-07``.

    Raises:
        ValueError: The base prompt has fewer than 6 full chunks.
    """
    tokens = base["token_ids"]
    if len(tokens) < 6 * chunk:
        raise ValueError(f"{base['id']} has fewer than 6 full chunks")
    b2 = _changed(tokens, 1, skip=1)
    variants: list[tuple[str, list[int], int]] = [
        ("B (6 chunks stored)", tokens, 6),
        ("B changed in chunk 0", _changed(tokens, 1, skip=0), 0),
        ("B changed in chunk 1", _changed(tokens, chunk, skip=0), 1),
        ("B changed in chunk 3", _changed(tokens, 3 * chunk, skip=0), 3),
        ("B changed in chunk 5", _changed(tokens, 5 * chunk, skip=0), 5),
        ("B2: B changed in chunk 0 another way", b2, 0),
        ("B2 cut to 2 chunks + tail", b2[: 2 * chunk + tail], 0),
        ("B2 cut to 3 chunks + tail", b2[: 3 * chunk + tail], 0),
    ]
    prompts = []
    for i, (what, ids, shared) in enumerate(variants):
        prompts.append(
            {
                "id": f"P-prefix-{i:02d}",
                "n_tokens": len(ids),
                "token_ids": ids,
                "expected": base["expected"],
                "target_record": base["target_record"],
                "derived_from": base["id"],
                "what": what,
                "chunks_shared_with_base": shared,
            }
        )
    return prompts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--base", default="P-ragged-11")
    parser.add_argument("--tail", type=int, default=20)
    args = parser.parse_args()
    with open(args.corpus) as f:
        corpus = json.load(f)
    chunk = corpus["chunk_size"]
    if args.tail + corpus["max_tokens"] >= chunk:
        raise ValueError("tail + max_tokens would complete a chunk during decode")
    by_id = {p["id"]: p for s in corpus["sets"].values() for p in s}
    corpus["sets"]["P-prefix"] = prefix_prompts(by_id[args.base], chunk, args.tail)
    digest = hashlib.sha256()
    for name in sorted(corpus["sets"]):
        for prompt in corpus["sets"][name]:
            digest.update(json.dumps(prompt["token_ids"]).encode())
    corpus["sha256"] = digest.hexdigest()
    with open(args.out, "w") as f:
        json.dump(corpus, f)
    for prompt in corpus["sets"]["P-prefix"]:
        print(f"{prompt['id']}: {prompt['n_tokens']} tokens, {prompt['what']}")
    print(f"sha256 {corpus['sha256']}")


if __name__ == "__main__":
    main()
