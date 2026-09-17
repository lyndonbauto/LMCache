# SPDX-License-Identifier: Apache-2.0
"""
Convert a request trace into LMCache lookup-hash JSONL, offline.

The rest of this package starts from ``lookup_hashes_*.jsonl`` emitted by a
*running* LMCache deployment
(:class:`~lmcache.v1.mp_observability.subscribers.logging.lookup_hash.LookupHashLoggingSubscriber`).
That is the right input when the deployment already exists, but it cannot
answer the question "what hit rate would this workload get?" before anything
has been deployed.

This module closes that gap: given a trace of requests, it computes the same
chunk hashes the server would have computed and writes the same JSONL schema,
so :mod:`lmcache.tools.cache_simulator.simulator` can then replay it.

It deliberately reuses :class:`~lmcache.v1.multiprocess.token_hasher.TokenHasher`
rather than reimplementing the hashing. That matters more than it looks:

* The hashes are **rolling** -- chunk *N*'s hash depends on chunks 0..*N*-1 --
  so a cache hit requires an exact *prefix* match. Changing one early token
  invalidates every chunk after it. A content-only reimplementation would
  report far higher hit rates than reality.
* Partial trailing chunks are **discarded**. Only complete chunks are hashed,
  stored, or looked up, so the tail of every request is permanently a miss.
  For a short prompt that tail alone can dominate the uncached fraction.

Both behaviours are properties of LMCache, not of this tool, and getting them
wrong would quietly invalidate any conclusion drawn from the output.

Input format
------------

JSONL, one request per line, ordered oldest first. Each line needs *either*
token IDs or text:

.. code-block:: json

    {"token_ids": [1, 2, 3], "request_id": "r0", "timestamp": 0.0}
    {"prompt": "hello world", "request_id": "r1"}

``token_ids`` is preferred and needs no tokenizer, which keeps the whole
pipeline pure-CPU and reproducible. ``prompt`` requires ``--tokenizer``.

Usage (module mode)::

    python3 -m lmcache.tools.cache_simulator.trace_hasher \\
        -i trace.jsonl \\
        --chunk-size 256 \\
        --layers 32 --kv-heads 8 --head-dim 128 --dtype bfloat16 \\
        -o lookup_hashes_trace.jsonl

Then feed the result to the simulator::

    python3 -m lmcache.tools.cache_simulator.simulator \\
        -i lookup_hashes_trace.jsonl --cache-capacity-gib 64 -o stats.png
"""

# Standard
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO
import argparse
import json
import sys

# First Party
from lmcache.v1.multiprocess.token_hasher import TokenHasher

# Byte width of each supported KV dtype, mirroring the mapping the simulator
# applies to the ``dtypes`` field.
_DTYPE_BYTES: dict[str, int] = {
    "float32": 4,
    "float16": 2,
    "bfloat16": 2,
    "float8_e4m3fn": 1,
    "float8_e5m2": 1,
    "int8": 1,
    "int32": 4,
    "int64": 8,
}


@dataclass
class TraceRequest:
    """One request read from a trace file.

    Attributes:
        request_id: Identifier carried through to the lookup event.
        timestamp: Arrival time. The simulator sorts on it, so it determines
            replay order and therefore the eviction sequence.
        token_ids: Token IDs, empty when the request supplied text instead.
        prompt: Request text, empty when ``token_ids`` was supplied.
    """

    request_id: str
    timestamp: float
    token_ids: list[int] = field(default_factory=list)
    prompt: str = ""


def kv_shape(chunk_size: int, layers: int, kv_heads: int, head_dim: int) -> list[int]:
    """Build the per-chunk KV tensor shape the simulator expects.

    The shape describes one chunk's worth of KV cache as
    ``[2 * layers, chunk_size, kv_heads * head_dim]``: a key and a value plane
    per layer, each holding ``chunk_size`` tokens of ``kv_heads * head_dim``
    elements. The simulator multiplies this out by the dtype width to get
    bytes per chunk, which is what its capacity model consumes.

    For multi-head latent attention (MLA) there is one latent per token per
    layer rather than separate key and value planes, so this helper does not
    describe it. Pass an explicit shape instead.

    Args:
        chunk_size: Tokens per chunk.
        layers: Number of layers storing KV.
        kv_heads: Key/value heads per layer, after any grouped-query sharing.
        head_dim: Elements per head.

    Returns:
        The three-element shape.

    Raises:
        ValueError: If any argument is not positive.
    """
    if chunk_size <= 0 or layers <= 0 or kv_heads <= 0 or head_dim <= 0:
        raise ValueError(
            "chunk_size, layers, kv_heads and head_dim must all be positive, "
            f"got {chunk_size}, {layers}, {kv_heads}, {head_dim}"
        )
    return [2 * layers, chunk_size, kv_heads * head_dim]


def read_trace(path: Path) -> list[TraceRequest]:
    """Read a request trace from JSONL.

    Args:
        path: Trace file. One JSON object per line; blank lines are skipped.
            Each object needs ``token_ids`` or ``prompt``, and may carry
            ``request_id`` and ``timestamp``.

    Returns:
        The requests, in file order. Order is significant: the simulator
        replays them as a cache-eviction sequence, so reordering changes the
        answer. Requests without a timestamp are given their line position, so
        an untimestamped trace still replays in file order.

    Raises:
        ValueError: If a line is not valid JSON, is not a JSON object, or
            carries neither ``token_ids`` nor ``prompt``.
        OSError: If the file cannot be read.
    """
    requests: list[TraceRequest] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: malformed JSON -- {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{lineno}: expected a JSON object")
            if "token_ids" not in record and "prompt" not in record:
                raise ValueError(
                    f"{path}:{lineno}: request needs either 'token_ids' or 'prompt'"
                )
            index = len(requests)
            requests.append(
                TraceRequest(
                    request_id=str(record.get("request_id", f"req-{index}")),
                    timestamp=float(record.get("timestamp", index)),
                    token_ids=[int(t) for t in record.get("token_ids", [])],
                    prompt=str(record.get("prompt", "")),
                )
            )
    return requests


def tokenize_requests(
    requests: list[TraceRequest], tokenizer_name: str
) -> list[list[int]]:
    """Resolve every request to token IDs, tokenizing text where needed.

    Requests that already carry token IDs are passed through untouched, so a
    trace may mix the two forms.

    Args:
        requests: Requests from :func:`read_trace`.
        tokenizer_name: Hugging Face tokenizer name or path. Only loaded if at
            least one request has no token IDs, so a purely numeric trace
            needs no tokenizer and no network access.

    Returns:
        One token ID list per request, in the same order.

    Raises:
        ValueError: If a tokenizer is required but ``tokenizer_name`` is empty.
        ImportError: If a tokenizer is required but transformers is missing.
    """
    if not any(not r.token_ids for r in requests):
        return [list(r.token_ids) for r in requests]

    if not tokenizer_name:
        raise ValueError("trace contains 'prompt' requests, so --tokenizer is required")

    # Third Party
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    return [
        list(r.token_ids) if r.token_ids else list(tokenizer(r.prompt)["input_ids"])
        for r in requests
    ]


def write_lookup_events(
    requests: list[TraceRequest],
    token_ids_per_request: list[list[int]],
    hasher: TokenHasher,
    model_name: str,
    shape: list[int],
    dtype: str,
    handle: TextIO,
) -> int:
    """Hash each request and write one lookup event per line.

    The output schema matches what the running server logs, including the
    ``"0x" + hex`` hash encoding, so the simulator cannot tell the difference
    between this and a production capture.

    Args:
        requests: Requests from :func:`read_trace`, used for their
            ``request_id`` and ``timestamp`` only.
        token_ids_per_request: Token IDs, index-aligned to ``requests``.
        hasher: Configured hasher. Its ``chunk_size`` is written into every
            event and must match the one the simulator is run with.
        model_name: Value for the ``model_name`` field.
        shape: Per-chunk KV shape, e.g. from :func:`kv_shape`.
        dtype: KV dtype name; must be one the simulator recognises.
        handle: Destination, opened for text writing.

    Returns:
        Number of events written.

    Raises:
        ValueError: If ``dtype`` is not a recognised KV dtype, since an
            unknown dtype makes the simulator silently compute zero bytes per
            chunk and therefore model an infinite cache.
    """
    if dtype not in _DTYPE_BYTES:
        raise ValueError(
            f"unrecognised dtype {dtype!r}; expected one of "
            f"{sorted(_DTYPE_BYTES)}. An unknown dtype would make the "
            "simulator treat chunks as zero bytes and never evict."
        )

    written = 0
    for request, token_ids in zip(requests, token_ids_per_request, strict=True):
        hashes = hasher.compute_chunk_hashes(token_ids)
        event = {
            "timestamp": request.timestamp,
            "request_id": request.request_id,
            "model_name": model_name,
            "chunk_size": hasher.chunk_size,
            # Full prompt length, including the trailing partial chunk that is
            # never cached. Keeping the tail in the denominator is what makes
            # the resulting hit rate a fraction of the *prompt* rather than a
            # fraction of the cacheable part.
            "seq_len": len(token_ids),
            "dtypes": [dtype],
            "shapes": [shape],
            "chunk_hashes": ["0x" + h.hex() for h in hashes],
        }
        handle.write(json.dumps(event) + "\n")
        written += 1
    return written


def add_trace_hasher_arguments(parser: argparse.ArgumentParser) -> None:
    """Register command-line arguments on ``parser``.

    Args:
        parser: Parser to extend.
    """
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help="Request trace JSONL. Each line needs 'token_ids' or 'prompt'.",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Destination lookup-hash JSONL.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=256,
        help="Tokens per chunk. Must match the deployment (default: 256).",
    )
    parser.add_argument(
        "--hash-algorithm",
        default="blake3",
        help=(
            "Hash algorithm, matching the deployment. The multiprocess server "
            "defaults to blake3 (default: blake3)."
        ),
    )
    parser.add_argument(
        "--model-name",
        default="",
        help="Value for the model_name field in each event.",
    )
    parser.add_argument(
        "--tokenizer",
        default="",
        help="Tokenizer name or path. Required only for 'prompt' requests.",
    )
    parser.add_argument(
        "--layers",
        type=int,
        default=32,
        help="Layers storing KV, for the bytes-per-chunk model (default: 32).",
    )
    parser.add_argument(
        "--kv-heads",
        type=int,
        default=8,
        help="Key/value heads per layer (default: 8).",
    )
    parser.add_argument(
        "--head-dim",
        type=int,
        default=128,
        help="Elements per head (default: 128).",
    )
    parser.add_argument(
        "--dtype",
        default="bfloat16",
        help="KV dtype (default: bfloat16).",
    )


def run_trace_hasher(args: argparse.Namespace) -> None:
    """Execute the conversion described by ``args``.

    Args:
        args: Parsed arguments from :func:`add_trace_hasher_arguments`.

    Raises:
        ValueError: Propagated from the helpers on malformed input.
    """
    requests = read_trace(Path(args.input))
    if not requests:
        raise ValueError(f"{args.input} contains no requests")

    token_ids_per_request = tokenize_requests(requests, args.tokenizer)
    hasher = TokenHasher(chunk_size=args.chunk_size, hash_algorithm=args.hash_algorithm)
    shape = kv_shape(args.chunk_size, args.layers, args.kv_heads, args.head_dim)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        written = write_lookup_events(
            requests=requests,
            token_ids_per_request=token_ids_per_request,
            hasher=hasher,
            model_name=args.model_name,
            shape=shape,
            dtype=args.dtype,
            handle=handle,
        )

    bytes_per_chunk = shape[0] * shape[1] * shape[2] * _DTYPE_BYTES[args.dtype]
    total_tokens = sum(len(t) for t in token_ids_per_request)
    print(f"Wrote {written} lookup events to {output_path}")
    print(f"  chunk size        : {args.chunk_size} tokens")
    print(f"  bytes per chunk   : {bytes_per_chunk / 2**20:.2f} MiB")
    print(f"  KV per token      : {bytes_per_chunk / args.chunk_size / 2**10:.2f} KiB")
    print(f"  total tokens      : {total_tokens:,}")


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Convert a request trace into LMCache lookup-hash JSONL.",
    )
    add_trace_hasher_arguments(parser)
    args = parser.parse_args()
    try:
        run_trace_hasher(args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
