#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Turn a hit-rate distribution into the expected pipelining saving (AIE-101).

The go/no-go input for the pipelining half of AIE-85 is not a hit rate. It is
**milliseconds per request, weighted by the traffic that actually occurs.**
This script computes that from a lookup-hash trace.

Why milliseconds and not a percentage
-------------------------------------

Pipelining recovers the transfer minus the one layer it can never hide::

    saving <= T * (1 - 1/L)

``T`` depends only on link speed. Active parameter count and GPU throughput do
not appear in it -- they move the *prefill*, which is the denominator when the
saving is quoted as a fraction. Two configurations differing only in GPU speed
save an identical number of milliseconds while reporting percentages that
differ by 2x, so a percentage is not a measurement unless the regime is pinned.

The model
---------

Per request, with ``cached`` and ``uncached`` token counts from the simulated
hit rate::

    T      = cached   * kv_bytes_per_token / link_rate
    C_rem  = uncached * 2 * active_params  / gpu_flops

    all_or_nothing = T + C_rem
    pipelined      = T/L + max(T * (L-1)/L, C_rem)
    saving         = all_or_nothing - pipelined

``C_rem`` is the prefill that is *still required*, not the prefill the cache
avoided. That distinction is the whole point: the avoided prefill is the
caching win and is not available for pipelining to hide transfer behind. On a
complete hit ``C_rem`` is zero, there is nothing to overlap, and the saving is
zero however fast the fabric is.

Caveats, stated because they bound the answer
---------------------------------------------

* ``C_rem`` uses the standard two-FLOPs-per-parameter-per-token approximation
  and ignores attention's quadratic term. At long context that understates
  prefill, which understates the overlap available and so **understates** the
  saving. Conservative in the right direction.
* Transfer is modelled at a single sustained rate. The M0 sweep saw a single
  object reach 1.88 GB/s against a 12.2 GB/s NIC ceiling, so pass
  ``--link-gbps`` deliberately rather than assuming line rate.
* Set ``--active-params-b`` to the model's **active** parameters, not its
  total. Production serving is mostly MoE at 3-6% sparsity, so the two differ
  by 20x or more.

Usage::

    ./payoff.py -i lookup_chat.jsonl --capacity-gib 64 \\
        --link-gbps 12.2 --gpu-tflops 400 --active-params-b 8 --layers 32
"""

from __future__ import annotations

# Standard
from pathlib import Path
from typing import Any
import argparse
import json
import sys

# First Party
from lmcache.tools.cache_simulator.simulator import (
    compute_kv_bytes_per_chunk,
    load_lookup_events,
    simulate,
)

_GIB = 2**30

# Uncached-fraction buckets. The first and last exist to expose bimodality: a
# workload that is mostly complete hits plus mostly complete misses has a mean
# that looks like a healthy partial-hit workload while containing almost no
# partial hits at all.
_BUCKETS: list[tuple[str, float, float]] = [
    ("complete hit (<1%)", 0.0, 0.01),
    ("1-10%", 0.01, 0.10),
    ("10-30%  <- pipelining peak", 0.10, 0.30),
    ("30-60%", 0.30, 0.60),
    ("60-99%", 0.60, 0.99),
    ("complete miss (>=99%)", 0.99, 1.01),
]


def per_request_payoff(
    cached_tokens: float,
    uncached_tokens: float,
    kv_bytes_per_token: float,
    link_bytes_per_s: float,
    active_params: float,
    gpu_flops: float,
    layers: int,
) -> dict[str, float]:
    """Price one request under the all-or-nothing and pipelined protocols.

    Args:
        cached_tokens: Tokens served from cache, so fetched over the network.
        uncached_tokens: Tokens still requiring prefill.
        kv_bytes_per_token: KV cache bytes per token, all layers combined.
        link_bytes_per_s: Sustained transfer rate.
        active_params: Active parameters per token (not total).
        gpu_flops: Effective prefill throughput in FLOP/s.
        layers: Layer count; sets the one-layer floor that cannot be hidden.

    Returns:
        Keys ``transfer_ms``, ``prefill_ms``, ``all_or_nothing_ms``,
        ``pipelined_ms`` and ``saving_ms``.

    Raises:
        ValueError: If ``layers`` is not positive or either rate is not
            positive, since each would make the model degenerate rather than
            merely inaccurate.
    """
    if layers <= 0:
        raise ValueError(f"layers must be positive, got {layers}")
    if link_bytes_per_s <= 0 or gpu_flops <= 0:
        raise ValueError("link rate and GPU throughput must both be positive")

    transfer_s = cached_tokens * kv_bytes_per_token / link_bytes_per_s
    prefill_s = uncached_tokens * 2.0 * active_params / gpu_flops

    all_or_nothing_s = transfer_s + prefill_s
    # One layer's transfer is irreducible; the rest can overlap with whatever
    # prefill remains.
    pipelined_s = transfer_s / layers + max(
        transfer_s * (layers - 1) / layers, prefill_s
    )
    return {
        "transfer_ms": transfer_s * 1e3,
        "prefill_ms": prefill_s * 1e3,
        "all_or_nothing_ms": all_or_nothing_s * 1e3,
        "pipelined_ms": pipelined_s * 1e3,
        "saving_ms": (all_or_nothing_s - pipelined_s) * 1e3,
    }


def analyse(
    events: list[dict[str, Any]],
    capacity_bytes: int,
    link_bytes_per_s: float,
    active_params: float,
    gpu_flops: float,
    layers: int,
) -> dict[str, Any]:
    """Simulate the trace and price every request.

    Args:
        events: Lookup-hash events, as loaded by the simulator.
        capacity_bytes: Cache capacity to simulate.
        link_bytes_per_s: Sustained transfer rate.
        active_params: Active parameters per token.
        gpu_flops: Effective prefill throughput in FLOP/s.
        layers: Layer count.

    Returns:
        The simulator's results plus ``rows`` (one payoff dict per request,
        with ``uncached_fraction`` and ``seq_len`` added) and
        ``mean_saving_ms``.

    Raises:
        ValueError: If the trace is empty or implies zero bytes per chunk.
    """
    if not events:
        raise ValueError("no lookup events to analyse")

    kv_bytes_per_chunk = compute_kv_bytes_per_chunk(events[0])
    if kv_bytes_per_chunk <= 0:
        raise ValueError(
            "trace implies zero KV bytes per chunk; check its shapes and dtypes"
        )
    chunk_size = int(events[0].get("chunk_size", 0))
    if chunk_size <= 0:
        raise ValueError("trace carries no usable chunk_size")
    kv_bytes_per_token = kv_bytes_per_chunk / chunk_size

    results = simulate(
        events,
        cache_capacity_bytes=capacity_bytes,
        kv_bytes_per_chunk=kv_bytes_per_chunk,
    )

    rows: list[dict[str, float]] = []
    for seq_len, hit_rate in zip(
        results["input_lengths"], results["per_request_token_hit_rates"], strict=True
    ):
        cached = hit_rate * seq_len
        row = per_request_payoff(
            cached_tokens=cached,
            uncached_tokens=seq_len - cached,
            kv_bytes_per_token=kv_bytes_per_token,
            link_bytes_per_s=link_bytes_per_s,
            active_params=active_params,
            gpu_flops=gpu_flops,
            layers=layers,
        )
        row["uncached_fraction"] = 1.0 - hit_rate
        row["seq_len"] = float(seq_len)
        rows.append(row)

    results["rows"] = rows
    results["kv_bytes_per_token"] = kv_bytes_per_token
    results["mean_saving_ms"] = (
        sum(r["saving_ms"] for r in rows) / len(rows) if rows else 0.0
    )
    return results


def report(results: dict[str, Any], label: str) -> None:
    """Print the histogram and the traffic-weighted saving.

    Args:
        results: Output of :func:`analyse`.
        label: Name for the workload, printed in the header.
    """
    rows: list[dict[str, float]] = results["rows"]
    total = len(rows)

    print()
    print("=" * 78)
    print(f"Workload: {label}")
    print("=" * 78)
    print(f"  requests            : {total:,}")
    print(f"  tokens              : {results['total_tokens']:,}")
    print(f"  aggregate hit rate  : {results['token_hit_rate']:.2%}")
    print(f"  KV per token        : {results['kv_bytes_per_token'] / 1024:.1f} KiB")
    print(f"  evictions           : {results['eviction_count']:,}")
    print()
    print("  Uncached fraction per request -- the regime distribution")
    print(f"  {'bucket':<30} {'requests':>9} {'share':>7} {'mean saving':>12}")
    print(f"  {'-' * 30} {'-' * 9} {'-' * 7} {'-' * 12}")
    for name, low, high in _BUCKETS:
        bucket = [r for r in rows if low <= r["uncached_fraction"] < high]
        if not bucket:
            continue
        mean_saving = sum(r["saving_ms"] for r in bucket) / len(bucket)
        print(
            f"  {name:<30} {len(bucket):>9,} {len(bucket) / total:>6.1%} "
            f"{mean_saving:>9.1f} ms"
        )
    print()

    mean_saving = results["mean_saving_ms"]
    mean_all = sum(r["all_or_nothing_ms"] for r in rows) / total
    mean_transfer = sum(r["transfer_ms"] for r in rows) / total
    mean_prefill = sum(r["prefill_ms"] for r in rows) / total
    print("  Traffic-weighted means (each is a mean over requests; the saving is")
    print("  computed per request and is NOT recoverable from the means below,")
    print("  because min() does not commute with averaging)")
    print(f"    transfer T        : {mean_transfer:8.1f} ms")
    print(f"    remaining prefill : {mean_prefill:8.1f} ms")
    print(f"    all-or-nothing    : {mean_all:8.1f} ms")
    print(f"    pipelined         : {mean_all - mean_saving:8.1f} ms")
    print(f"    SAVING            : {mean_saving:8.1f} ms  "
          f"({mean_saving / mean_all:.1%} of the cache-hit path)")
    print()

    # The caching win, for scale. Without it the saving above reads as though
    # it were the value of the cache, which it is not.
    mean_cached = sum(r["seq_len"] * (1 - r["uncached_fraction"]) for r in rows) / total
    mean_seq = sum(r["seq_len"] for r in rows) / total
    if mean_cached > 0 and mean_prefill > 0:
        avoided_ms = mean_prefill * (mean_cached / max(mean_seq - mean_cached, 1e-9))
        print(f"  For scale, caching itself avoids roughly {avoided_ms:8.1f} ms of "
              f"prefill per request.")
        print("  Pipelining's saving is the marginal gain on top of that, not "
              "a measure of\n  whether caching is worthwhile.")
    print()


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Expected pipelining saving (AIE-101)")
    parser.add_argument("-i", "--input", required=True, help="Lookup-hash JSONL.")
    parser.add_argument("--label", default="", help="Name for the report header.")
    parser.add_argument("--capacity-gib", type=float, default=64.0)
    parser.add_argument(
        "--link-gbps",
        type=float,
        default=12.2,
        help="Sustained transfer rate in GB/s (default: 12.2, the M0 NIC ceiling).",
    )
    parser.add_argument("--gpu-tflops", type=float, default=400.0)
    parser.add_argument(
        "--active-params-b",
        type=float,
        default=8.0,
        help="ACTIVE parameters in billions, not total (default: 8).",
    )
    parser.add_argument("--layers", type=int, default=32)
    parser.add_argument("--json-out", default="", help="Optional raw results path.")
    args = parser.parse_args()

    try:
        events = load_lookup_events([Path(args.input)])
        results = analyse(
            events,
            capacity_bytes=int(args.capacity_gib * _GIB),
            link_bytes_per_s=args.link_gbps * 1e9,
            active_params=args.active_params_b * 1e9,
            gpu_flops=args.gpu_tflops * 1e12,
            layers=args.layers,
        )
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    report(results, args.label or Path(args.input).stem)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "config": vars(args),
                    "token_hit_rate": results["token_hit_rate"],
                    "mean_saving_ms": results["mean_saving_ms"],
                    "rows": results["rows"],
                },
                handle,
            )
        print(f"Raw results written to {out}")


if __name__ == "__main__":
    main()
