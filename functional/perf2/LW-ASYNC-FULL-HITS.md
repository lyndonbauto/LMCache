# Layerwise per-request load mode (Task C): test results

Code: `prototype-stage-1g` (on 1f's `8191c2ca`; tested at `1a70cf40`, default R
changed to 0.05 in `18db855b`). Design: `docs/design/v1/multiprocess/layerwise-load.md`
on 1g, section "Per-request load mode".

With layerwise on, the server now picks a mode for each request at lookup:

```text
LAYERWISE  iff  new_chunks >= R * hit_chunks     R = --layerwise-min-new-token-ratio
ASYNC      otherwise (the aon path: loaded before the request is scheduled)
```

## Conclusion

The per-request mode keeps what layerwise is good at and removes what it was
bad at. Before (1f, every request layerwise, R = 0 below):

- **Full hits stalled the batch.** A full hit has almost nothing to compute, so
  every decode in its steps waited for each loaded layer: time between tokens
  (TBT) 41.9 ms at 8k c=32 and 76.5 ms at 16k c=32, with gaps of about 600 ms,
  against aon's 9-10 ms and 16-35 ms.
- **Partial hits were faster than aon** (TTFT 0.02-0.31 s lower at c = 1),
  because the load overlaps the prefill instead of running before it.

Now (R = 0.05 or 0.1), full hits load async and partial hits load layerwise:

- **Full hits match aon:** TBT 6.8-9.3 ms, max gap 19-27 ms, TTFT within noise
  of aon at every concurrency (c = 4-32, 8k and 16k).
- **Partial hits keep layerwise's TTFT** whenever the rule picks layerwise, with
  no TBT cost at c = 1.
- So the new code is at least as good as the better of aon and 1f's layerwise in
  every full-hit and partial-hit point measured. The mixed points (full and
  partial hits at once) are inconclusive; see below.

**R = 0.05 is the default** (it was 0.1 in these runs). Layerwise won TTFT for
every partial hit tested, down to 16k cached + 1k new (new/hit 0.0625: 0.34 s
layerwise, 0.59 s async). R = 0.1 sends that case async and loses 0.25 s;
R = 0.05 keeps every tested partial hit layerwise and still sends every full
hit (0 new chunks) async.

## Setup

- MI300X droplet `165.245.143.182`, Llama-3.1-8B, TP=1, vLLM 0.27.1 with the
  per-step piecewise patch, `--distributed-executor-backend mp`.
- LMCache server with an Aerospike kv-sink L2 on `/mnt/scratch` (one 576 GiB data
  file), Soft-RoCE `rxe0`, 16 queue pairs, `--pipelined-fetch`. L1 is emptied
  before every point (server restart), so every hit comes from L2.
- Sessions, one vLLM each: **aon** (layerwise off), and **lw** at R = 0, 0.05,
  0.1, 0.2, 0.4 (`chain_c.sh`). 0 errors in every session.
- TTFT and TBT are medians over the point's requests; TBT is
  `(total - TTFT) / (tokens - 1)`, 128 output tokens. Max gap is the longest gap
  between two tokens in the point.
- Raw results: `lwaon3/c` (local, untracked). Tables: `functional/perf2/scripts/report_c.py`.
- Mode per point from the server log (`pipelined_outcome`): `pipelined` means
  layerwise; `not_deferred` means async.

## Full hits (32 prompts per point)

Each cell: TTFT (s) / TBT (ms) / max gap (ms), mode (L layerwise, A async). R = 0.05
was not run for full hits; it picks async for them, as R = 0.1 does.

| Prompt, c | aon | lw R=0 (before) | lw R=0.1 |
|---|---|---|---|
| 8k, c=4 | 0.233 / 6.9 / 18 A | 0.545 / 7.4 / 291 L | 0.235 / 6.8 / 19 A |
| 8k, c=8 | 0.274 / 8.7 / 22 A | 0.853 / 9.7 / 562 L | 0.262 / 8.6 / 21 A |
| 8k, c=16 | 1.276 / 9.0 / 16 A | 1.398 / 18.6 / 602 L | 1.190 / 9.0 / 20 A |
| 8k, c=32 | 2.498 / 9.0 / 23 A | 2.442 / 41.9 / 592 L | 2.613 / 9.0 / 19 A |
| 16k, c=4 | 0.409 / 8.4 / 24 A | 0.754 / 11.8 / 672 L | 0.439 / 8.3 / 21 A |
| 16k, c=8 | 1.053 / 9.5 / 31 A | 1.193 / 19.9 / 599 L | 1.178 / 9.0 / 21 A |
| 16k, c=16 | 3.400 / 9.0 / 33 A | 2.402 / 33.2 / 640 L | 3.339 / 9.3 / 24 A |
| 16k, c=32 | 4.780 / 9.7 / 35 A | 4.882 / 76.5 / 605 L | 5.078 / 9.0 / 27 A |

## Partial hits (c = 1, 4 prompts per point)

Each cell: TTFT (s) / TBT (ms), mode. **Bold**: the rule picked async.

| Cached + new | new / hit | aon | lw R=0 (before) | R=0.05 | R=0.1 | R=0.2 | R=0.4 |
|---|---|---|---|---|---|---|---|
| 2k + 1k | 0.5 | 0.134 / 5.0 A | 0.092 / 5.0 L | 0.097 / 5.0 L | 0.100 / 5.0 L | 0.109 / 5.0 L | 0.099 / 5.0 L |
| 2k + 2k | 1 | 0.176 / 5.1 A | 0.152 / 5.1 L | 0.150 / 5.1 L | 0.152 / 5.1 L | 0.150 / 5.1 L | 0.154 / 5.1 L |
| 2k + 4k | 2 | 0.319 / 5.3 A | 0.290 / 5.3 L | 0.285 / 5.3 L | 0.280 / 5.3 L | 0.278 / 5.3 L | 0.289 / 5.3 L |
| 2k + 8k | 4 | 0.694 / 5.6 A | 0.644 / 5.6 L | 0.668 / 5.7 L | 0.672 / 5.7 L | 0.671 / 5.7 L | 0.668 / 5.6 L |
| 8k + 1k | 0.125 | 0.325 / 5.5 A | 0.195 / 5.5 L | 0.179 / 5.5 L | 0.181 / 5.6 L | **0.320 / 5.5 A** | **0.331 / 5.5 A** |
| 8k + 2k | 0.25 | 0.432 / 5.6 A | 0.276 / 5.6 L | 0.259 / 5.6 L | 0.270 / 5.6 L | 0.275 / 5.6 L | **0.417 / 5.6 A** |
| 8k + 4k | 0.5 | 0.656 / 5.8 A | 0.500 / 5.8 L | 0.496 / 5.8 L | 0.492 / 5.8 L | 0.504 / 5.8 L | 0.499 / 5.8 L |
| 8k + 8k | 1 | 1.216 / 6.2 A | 1.071 / 6.2 L | 1.052 / 6.2 L | 1.062 / 6.2 L | 1.060 / 6.2 L | 1.062 / 6.2 L |
| 16k + 1k | 0.0625 | 0.611 / 6.2 A | 0.340 / 6.2 L | 0.339 / 6.2 L | **0.593 / 6.3 A** | **0.588 / 6.2 A** | **0.614 / 6.2 A** |
| 16k + 2k | 0.125 | 0.721 / 6.3 A | 0.411 / 6.3 L | 0.419 / 6.3 L | 0.408 / 6.3 L | **0.727 / 6.3 A** | **0.727 / 6.4 A** |
| 16k + 4k | 0.25 | 1.069 / 6.5 A | 0.774 / 6.5 L | 0.774 / 6.5 L | 0.764 / 6.5 L | 0.767 / 6.5 L | **1.089 / 6.6 A** |
| 16k + 8k | 0.5 | 1.877 / 6.9 A | 1.566 / 6.9 L | 1.557 / 7.0 L | 1.544 / 7.0 L | 1.568 / 6.9 L | 1.542 / 6.9 L |

- The mode switched exactly where `new / hit` crosses R.
- Layerwise cells are 0.02-0.31 s faster than async cells for the same request,
  and every async cell lands at aon's TTFT.
- TBT does not depend on the mode at c = 1: with no other requests running, a
  layerwise load delays nobody.

## Mixed: full and partial hits at once (c = 4 each, 8 prompts each)

Each cell: TTFT (s) / TBT (ms), mode.

| Point | Stream | aon | lw R=0 | R=0.05 | R=0.1 | R=0.2 | R=0.4 |
|---|---|---|---|---|---|---|---|
| 8k + 8k | full hits | 0.303 / 41.7 A | 3.896 / 15.1 L | 3.787 / 14.4 A | 2.784 / 22.0 A | 3.264 / 18.6 A | 3.882 / 14.1 A |
| 8k + 8k | partial | 3.658 / 18.1 A | 3.747 / 15.7 L | 3.237 / 18.1 L | 3.170 / 18.6 L | 3.328 / 18.4 L | 2.729 / 22.5 L |
| 16k + 4k | full hits | 1.953 / 21.0 A | 2.754 / 18.2 L | 2.313 / 23.2 A | 2.443 / 23.1 A | 2.946 / 17.6 A | 0.756 / 32.3 A |
| 16k + 4k | partial | 2.472 / 20.1 A | 2.134 / 21.9 L | 1.957 / 23.5 L | 2.764 / 14.5 L | 2.246 / 19.0 L | 2.857 / 19.1 A |

Inconclusive. Max gaps are 0.8-1.6 s in every column, aon included: they come
from the partial hits' 8k- and 4k-token prefills, not from loads. At 8k + 8k, full
hits wait much longer for their first token with layerwise partial hits (2.8-3.9 s
against aon's 0.30 s), because those partial hits are scheduled at once and their
prefills run first; under aon the partial hits wait for their own loads and the
full hits go first. Full hits decode faster in the same runs (TBT 14-22 ms against
41.7 ms). A fair mixed test needs more prompts and staggered arrivals.

## Noise

`8k + 8k, c = 4` (8 prompts) loaded layerwise in every lw session; TTFT ranged
2.61-3.63 s and TBT 13.0-20.7 ms (aon 2.77 s / 20.6 ms). Differences below about
0.5 s at c = 4 with 8 prompts are noise. The c = 1 partial-hit points repeat to
within about 0.03 s where the mode is the same.

## Unit tests

- **Changed files** (11, on the box with the GPU): 240 passed.
- **Wider run on the box** (`tests/v1/layerwise`, `tests/v1/multiprocess`, the
  connector and adapter tests): 1550 passed, 4 failed, 12 errors, 1 skipped, none
  caused by 1g:
  - `test_client.py::test_business_callers_create_clients_through_factory`: a
    source scan that flags `test_deferred_response.py`, unchanged by 1g.
  - `test_cache_server.py` (2 failed, 12 errors) and `test_mq.py::test_mq_register_kv_cache`:
    they expect `register_kv_cache` to return `None`; it returns
    `RegisterKvCacheResponse` since `b795e69f` (before 1f).
  - The gRPC stubs were copied from the VM build (same protobuf 6.33.6 and
    grpcio 1.78.0), since `lmc-c` has no `grpc_tools`.
- **CPU VM** (`lmcache-vm`, vLLM 0.27.1 without deps): 1484 passed, 51 skipped (GPU,
  CUDA, cupy or Rust only), the same `test_client.py` failure.

## Not tested

- Partial hits with very short suffixes (one or two chunks on a long prefix) at
  high concurrency. They load layerwise at R = 0.05, and a large layerwise load
  can stall other requests' decodes the way full hits did.
- R = 0.05 for full hits directly (the code path is the same as R = 0.1: 0 new
  chunks is async at any R > 0).
- Mixed traffic with realistic arrivals.
- The output check (lw outputs equal aon's) on 1g.
