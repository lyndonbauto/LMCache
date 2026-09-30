# SPDX-License-Identifier: Apache-2.0
"""Build the functional-test prompt corpus for one model from corpus_spec.json.

Prompts are assembled in token space so each one has an exact token count for
the model's tokenizer (for example 256 * N for P-exact). Every prompt is a
registry of records and ends with a question about a record in the first
chunk, so KV loaded into the wrong place gives a wrong answer.

Usage::

    python build_corpus.py --model meta-llama/Llama-3.1-8B-Instruct \
        --spec functional/corpus/corpus_spec.json --out corpus_llama.json

The output records every prompt's token IDs and the expected answer, plus a
SHA-256 over all token IDs so a run can be tied to the exact corpus.
"""

# Standard
from typing import Any
import argparse
import hashlib
import json
import random

# Third Party
from transformers import AutoTokenizer, PreTrainedTokenizerBase

CITIES = [
    "Oslo", "Lima", "Accra", "Hanoi", "Perth", "Quito", "Riga", "Dakar",
    "Porto", "Cusco", "Leeds", "Turin", "Busan", "Cairo", "Nairobi", "Split",
]  # fmt: skip
THINGS = [
    "warehouse", "bridge", "library", "harbour", "station", "tower",
    "market", "garden", "museum", "stadium", "factory", "school",
]  # fmt: skip
FILLER_WORDS = [
    " the", " and", " of", " with", " north", " south", " east", " west",
    " old", " new", " red", " blue", " green", " small", " large", " quiet",
]  # fmt: skip
MAX_TARGET_INDEX = 8  # keeps the asked-about record inside the first chunk


class _Encoder:
    """Tokenizes text segments without special tokens and pads exactly."""

    def __init__(self, tokenizer: PreTrainedTokenizerBase) -> None:
        self._tok = tokenizer
        self.bos: list[int] = (
            [tokenizer.bos_token_id] if tokenizer.bos_token_id is not None else []
        )
        self._filler = [
            ids[0]
            for word in FILLER_WORDS
            if len(ids := tokenizer.encode(word, add_special_tokens=False)) == 1
        ]
        if not self._filler:
            raise RuntimeError("no single-token filler words for this tokenizer")

    def encode(self, text: str) -> list[int]:
        """Return the token IDs of ``text`` without special tokens."""
        return self._tok.encode(text, add_special_tokens=False)

    def filler(self, count: int, rng: random.Random) -> list[int]:
        """Return ``count`` single-token filler words."""
        return [rng.choice(self._filler) for _ in range(count)]


def _record(tag: str, index: int, rng: random.Random) -> tuple[str, str]:
    """Return one record sentence and its access code."""
    code = f"{rng.randint(1000, 9999)}"
    sentence = (
        f"Record {tag}-{index}: the {rng.choice(THINGS)} in "
        f"{rng.choice(CITIES)} has access code {code}.\n"
    )
    return sentence, code


def _question(tag: str, index: int, compact: bool) -> str:
    if compact:
        return f"\nQ: access code of Record {tag}-{index}?\nA:"
    return (
        f"\nQuestion: what is the access code of Record {tag}-{index}? "
        "Answer with the code, then repeat the full record sentence.\nAnswer:"
    )


def _registry_prompt(
    enc: _Encoder, tag: str, n_tokens: int, compact: bool = False
) -> dict[str, Any]:
    """Build a registry prompt of exactly ``n_tokens`` tokens.

    Records are added while they fit; the gap before the question is padded
    with single-token filler words, so the length is exact.
    """
    rng = random.Random(f"corpus-v1-{tag}")
    header = enc.encode("Registry:\n" if compact else
                        "Below is a registry of records. Read it carefully.\n\n")
    records: list[list[int]] = []
    codes: list[str] = []
    probe_question = enc.encode(_question(tag, MAX_TARGET_INDEX, compact))
    budget = n_tokens - len(enc.bos) - len(header) - len(probe_question) - 2
    used = 0
    while True:
        sentence, code = _record(tag, len(records), rng)
        ids = enc.encode(sentence)
        if records and used + len(ids) > budget:
            break
        records.append(ids)
        codes.append(code)
        used += len(ids)
        if used > budget:
            break
    target = min(max(1, len(records) // 20), MAX_TARGET_INDEX, len(records) - 1)
    question = enc.encode(_question(tag, target, compact))
    body = enc.bos + header + [t for ids in records for t in ids]
    gap = n_tokens - len(body) - len(question)
    if gap < 0:
        raise ValueError(f"{tag}: {n_tokens} tokens is too short for one record")
    token_ids = body + enc.filler(gap, rng) + question
    if len(token_ids) != n_tokens:
        raise RuntimeError(f"{tag}: built {len(token_ids)} tokens, wanted {n_tokens}")
    return {
        "n_tokens": n_tokens,
        "token_ids": token_ids,
        "expected": codes[target],
        "target_record": f"{tag}-{target}",
        "records": len(records),
    }


def _shared_prompts(enc: _Encoder, spec: dict[str, Any], chunk: int) -> list[dict]:
    """P-shared: one exact-length shared registry, then per-prompt tails."""
    prefix_tokens = spec["shared_prefix_chunks"] * chunk
    rng = random.Random("corpus-v1-shared-prefix")
    body = enc.bos + enc.encode("Below is a registry of records. Read it carefully.\n\n")
    codes: list[str] = []
    while True:
        sentence, code = _record("S", len(codes), rng)
        ids = enc.encode(sentence)
        if len(body) + len(ids) > prefix_tokens:
            break
        body += ids
        codes.append(code)
    body += enc.filler(prefix_tokens - len(body), rng)
    prompts = []
    for i, tail_tokens in enumerate(spec["tail_tokens"]):
        target = i % (MAX_TARGET_INDEX + 1)
        tail_rng = random.Random(f"corpus-v1-shared-tail-{i}")
        head = enc.encode(f"\nAddendum {i}: nothing below changes the registry.")
        question = enc.encode(_question("S", target, compact=False))
        gap = tail_tokens - len(head) - len(question)
        if gap < 0:
            raise ValueError(f"P-shared tail {i}: {tail_tokens} tokens is too short")
        token_ids = body + head + enc.filler(gap, tail_rng) + question
        prompts.append({
            "n_tokens": len(token_ids),
            "token_ids": token_ids,
            "expected": codes[target],
            "target_record": f"S-{target}",
            "shared_prefix_tokens": prefix_tokens,
        })
    return prompts


def _multi_turn_prompts(enc: _Encoder, spec: dict[str, Any]) -> list[dict]:
    """P-multi: scripted conversations; each turn asks about a turn-1 record.

    Assistant turns are scripted (the correct code), so every turn's prompt is
    fixed and does not depend on what the model generated.
    """
    prompts = []
    for c in range(spec["conversations"]):
        rng = random.Random(f"corpus-v1-multi-{c}")
        history = enc.bos + enc.encode("Below is a registry of records. Read it carefully.\n\n")
        first_turn_codes: list[str] = []
        for turn in range(1, spec["turns"] + 1):
            user = enc.encode(f"\nUser (turn {turn}): here are more records.\n")
            added = 0
            while added < spec["turn_tokens"]:
                sentence, code = _record(f"C{c}-T{turn}", added, rng)
                ids = enc.encode(sentence)
                user += ids
                added += len(ids)
                if turn == 1:
                    first_turn_codes.append(code)
            target = (turn - 1) % min(len(first_turn_codes), MAX_TARGET_INDEX + 1)
            question = enc.encode(_question(f"C{c}-T1", target, compact=False))
            token_ids = history + user + question
            expected = first_turn_codes[target]
            prompts.append({
                "n_tokens": len(token_ids),
                "token_ids": token_ids,
                "expected": expected,
                "target_record": f"C{c}-T1-{target}",
                "conversation": c,
                "turn": turn,
            })
            history = token_ids + enc.encode(f" {expected}.\n")
    return prompts


def build(model: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Build every set of the corpus for ``model``.

    Args:
        model: Hugging Face model id whose tokenizer defines token counts.
        spec: The parsed corpus_spec.json.

    Returns:
        The corpus: ``{"version", "model", "sha256", "sets": {name: [prompt]}}``.
        P-salt is recorded as a reference to its base set and salts.
    """
    enc = _Encoder(AutoTokenizer.from_pretrained(model, local_files_only=True))
    chunk = spec["chunk_size"]
    sets = spec["sets"]
    out: dict[str, list[dict]] = {}
    out["P-short"] = [
        _registry_prompt(enc, f"H{i}", n, compact=True)
        for i, n in enumerate(sets["P-short"]["lengths"])
    ]
    out["P-exact"] = [
        _registry_prompt(enc, f"E{i}", c * chunk)
        for i, c in enumerate(sets["P-exact"]["chunks"])
    ]
    out["P-ragged"] = [
        _registry_prompt(enc, f"R{i}", c * chunk + extra)
        for i, (c, extra) in enumerate(sets["P-ragged"]["chunks_plus_tokens"])
    ]
    out["P-long"] = [
        _registry_prompt(enc, f"L{i}", c * chunk)
        for i, c in enumerate(sets["P-long"]["chunks"])
    ]
    out["P-shared"] = _shared_prompts(enc, sets["P-shared"], chunk)
    out["P-multi"] = _multi_turn_prompts(enc, sets["P-multi"])
    for name, prompts in out.items():
        for i, prompt in enumerate(prompts):
            prompt["id"] = f"{name}-{i:02d}"
    digest = hashlib.sha256()
    for name in sorted(out):
        for prompt in out[name]:
            digest.update(json.dumps(prompt["token_ids"]).encode())
    return {
        "version": spec["version"],
        "model": model,
        "chunk_size": chunk,
        "max_tokens": spec["max_tokens"],
        "sha256": digest.hexdigest(),
        "sets": out,
        "salt": sets["P-salt"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.spec) as f:
        spec = json.load(f)
    corpus = build(args.model, spec)
    with open(args.out, "w") as f:
        json.dump(corpus, f)
    for name, prompts in corpus["sets"].items():
        lengths = [p["n_tokens"] for p in prompts]
        print(f"{name}: {len(prompts)} prompts, {min(lengths)}-{max(lengths)} tokens")
    print(f"sha256 {corpus['sha256']}")


if __name__ == "__main__":
    main()
