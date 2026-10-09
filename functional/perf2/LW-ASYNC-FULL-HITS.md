# Layerwise: load full hits outside the forward pass (Task C design)

Status: design for approval; no code yet. Code base: `prototype-stage-1f` (`8191c2ca`).
Plan: `LW-ITL-FIX-PLAN.md` Task C. Evidence: `LW-PER-STEP-GRAPH.md`.

## Problem

With layerwise load on, every load runs inside a forward pass, and the whole batch
waits for each layer to arrive. A partial hit with a long new suffix hides that wait
behind its own prefill. A full hit has one token to compute, so the batch pauses for
the whole transfer. The c = 4-32 grid (8k and 16k full hits, 0 errors) shows it:

| | aon | lw per-step, budget off | lw per-step, 1 GiB |
|---|---|---|---|
| Time between tokens, 8k c=32 | 8.8 ms | 34.3 ms | 40.4 ms |
| Time between tokens, 16k c=32 | 9.7 ms | 58.0 ms | 75.1 ms |
| Max gap, 16k c=32 | 27 ms | 6617 ms | 374 ms |

aon avoids this because a full hit waits for its KV outside the forward pass, in
vLLM's `WAITING_FOR_REMOTE_KVS`, while other requests keep decoding. The per-step
graph fix (Task B) cannot help: the pause is the transfer, not the graph mode.

## Proposal

Choose the load mode per request:

- **Layerwise** when the request's own prefill is long enough to hide the transfer.
- **Async (the aon path)** otherwise. This covers full hits and short new suffixes.

The server's lookup decides, because only it knows both numbers at the point where
it must commit: the hit length (after the L2 lookup) and the prompt length (the
lookup carries the token ids). It already makes a related decision there:
`PipelinedDeferral.accepts` decides whether a lookup's L2 hits are deferred to a
layer-by-layer fetch at retrieve time or loaded whole now. The rule becomes:

> A request loads layer by layer if and only if its lookup deferred its L2 hits.

So one decision drives everything, and the two sides cannot disagree.

```
lookup (server)                              scheduler (connector)      worker / daemon
---------------                              ---------------------      ---------------
L2 lookup: hit_tokens
new_tokens = prompt_tokens - hit_tokens
defer iff  new_tokens >= R * hit_tokens
       and the existing checks pass
  |-- deferred --> result: hits, LAYERWISE --> load_async=False   --> retrieve with generation > 0
  |                                            admission gate         layer waits in forward
  |                                            step needs PIECEWISE
  '-- loaded whole into L1 -> result: hits, ASYNC --> load_async=True --> retrieve with generation 0
                                               WAITING_FOR_REMOTE_KVS   one H2D from L1, FULL graphs
                                               no gate, no flag         finished_recving reported
```

Why this split:

- An async request's KV is already in L1 when vLLM allocates its blocks, so its retrieve
  is one H2D copy (about 20-40 ms for 1 GiB). That is exactly what aon does today.
- Deferral can be refused for existing reasons (no free RDMA window, too many chunks,
  keys already in L1). Under the rule, those requests go async, which is the right
  outcome: their KV is in L1 and layer-by-layer loading would save little.
- It needs no new vLLM change. The per-step patch stays, and flags only steps with a
  layerwise retrieve.

### The threshold R

Layerwise pays off when prefilling the new tokens takes at least as long as moving
the hit tokens:

```
new_tokens / prefill_rate >= hit_tokens * kv_bytes_per_token / link_rate
R = prefill_rate * kv_bytes_per_token / link_rate
```

On the MI300X box (Soft-RoCE about 7 GiB/s; Llama-3.1-8B, 128 KiB per token) the
link moves about 57k tokens/s. Prefill of an 8B model at 8-16k context is
roughly 15-25k tokens/s, which puts R at about 0.25-0.45. This estimate is
unmeasured. The proposal is a server flag `--layerwise-min-new-token-ratio`
(default 0.25, tuned in the measurement step). Full hits have
`new_tokens = 1`, so they go async at any useful R.

## Changes

### Server (daemon)

1. **Deferral rule.** `PipelinedDeferral.accepts` also gets `prompt_tokens` and
   applies the R rule. `Lookup` passes `len(key.token_ids)`.
2. **Lookup result reports the mode.** `QUERY_PREFETCH_STATUS` returns the hit count
   and whether the lookup deferred (an enum, `LoadMode.LAYERWISE` or `LoadMode.ASYNC`,
   not a bool). This is an IPC protocol change; old clients must keep working, so
   either version the message or add a new query and fall back.
3. **Retrieve with generation 0 on a layerwise context.** `start_retrieve` today sets
   `layerwise_active` from the context and raises `ValueError` on generation 0. It
   becomes `layerwise_active = context is layerwise and retrieve_generation > 0`; a
   generation-0 retrieve takes the existing non-layerwise path (H2D from L1 on the
   affinity thread, under the `TransferGate`).
4. **Mismatch guard.** If a generation-0 retrieve still claims deferred keys (it
   should not), the existing whole-load fallback (`load_into_l1`) would block the
   worker's affinity thread for the whole RDMA fetch. Log a warning and count it;
   do not crash.

### Connector, scheduler side

5. `get_num_new_matched_tokens`: read the mode with the hit count. Run the 1e
   admission gate only for `LAYERWISE`. Return `load_async = need_to_load > 0 and
   mode is ASYNC`.
6. The request tracker stores the mode. Retrieve metadata carries it to the worker
   (`LMCacheMPRequestMetadata` gains a `load_mode` field).
7. `requires_piecewise_for_step`: True only when some retrieve in the step is
   `LAYERWISE` (and, as today, in every step when the dispatcher is on).

### Connector and adapter, worker side

8. `start_load_kv`: pass the mode to `submit_retrieve`.
   `LMCacheDrivenTransferContext.submit_retrieve` assigns a generation, and sets the active generation for layer
   waits, only for `LAYERWISE`. An async retrieve sends generation 0 and leaves the
   layerwise waiter untouched, so `wait_for_layer_load` ignores it.
9. `get_finished`: today it returns `finished_recving = None` whenever layerwise is
   on, because vLLM asserts on a finished-receiving report for a running request.
   It becomes: report finished async retrieves (including ones dropped while the
   server was unhealthy, exactly once, as the aon path already does), and never
   report layerwise ones. Their futures are still drained so failures are logged.

### Unchanged

- vLLM. The per-step patch is still needed for layerwise steps; async loads add no hook.
- The aon path when layerwise is off.
- Failure handling per mode: async loads fail through `get_block_ids_with_load_errors`
  plus `finished_recving`, as aon does today, including the bypass after a failed
  async load (`BYPASS_LMCACHE`). Layerwise loads keep their current handling and the
  vllm#49250 recompute warning.

## Failure paths to cover

| Case | Expected |
|---|---|
| Async request aborted while in `WAITING_FOR_REMOTE_KVS` | Existing aon handling; read locks released by the retrieve or by `FREE_LOOKUP_LOCKS` |
| Async retrieve fails | Blocks reported as load errors; `finished_recving` reported once; request recomputes or fails per policy |
| Server unhealthy at submit | Async: dropped and reported once (existing). Layerwise: reported through load errors, never in `finished_recving` |
| Same step has async and layerwise retrieves | Only layerwise ones get generations; step runs PIECEWISE; async H2D shares the stream |
| Preemption of a layerwise request | Unchanged (lookup again from scratch; the mode may change) |
| Deferral refused for lack of an RDMA window | Request goes async; no window or budget is held |

## Risks

1. **Stream head-of-line.** Async H2D copies and layerwise layer copies share the
   context's transfer stream. A 1 GiB async copy enqueued just before a layerwise
   load delays its first layers by about 20-40 ms. That is far below today's pauses,
   but if the measurement shows it, split async H2D into per-group units
   (the gate is already held per unit) or give async copies their own stream.
2. **Link contention.** Async full-hit fetches (at lookup) and layerwise fetches
   share the RDMA link, so layerwise prefills may wait longer. This is inherent, not
   new: today the same bytes move anyway.
3. **First token for full hits.** Full hits get aon's TTFT, not lw's: at c=1, 8k
   0.199 vs 0.164 s, 16k 0.379 vs 0.308 s. In exchange, other requests stop pausing.
4. **Protocol change** to the lookup result: needs compatibility for old clients.
5. **vLLM scheduler assumptions** about mixing async and synchronous loads from one
   connector. The API allows it per request, but this path is not exercised today.
   Budget 1-2 days in the estimate for surprises.

## Measurement and pass criteria

Harness addition: a mixed point that runs full hits and partial hits at the same
time, for example two `perf_client.py` streams in one session (`point mix=...`),
reported separately. About half a day.

Runs on the MI300X box, 32 prompts per point, 0 errors required:

- **Full hits at c = 4-32** (the existing grid): time between tokens and max gap close
  to aon's (9-10 ms, under 40 ms).
- **Partial hits** (8k prefix plus 8k suffix, c = 1 and 4): TTFT p50 as good as lw today.
- **Mixed**: full-hit decode close to aon; partial-hit TTFT close to lw.
- **Threshold sweep**: R at 0.1, 0.25, 0.5, 1 on 8k prefixes with 1k, 2k, 4k and 8k
  suffixes, to set the default.
- **Outputs**: lw equals aon on the same prompts (the corrected check from
  `LW-PER-STEP-GRAPH.md`), full and partial hits.

## PR breakdown

1. **Server**: deferral rule and flag, lookup result mode with compatibility,
   generation-0 retrieves on layerwise contexts, mismatch guard. Unit tests per item.
2. **Connector and adapter**: mode in tracker and metadata, async return, gate and
   piecewise flag only for layerwise, generation only for layerwise,
   `finished_recving` filtering. Unit tests, including mixed-mode steps and the
   failure table above.
3. **Docs and measurement**: `docs/design/v1/multiprocess/layerwise-load.md` section,
   harness mixed point, box runs, report.

## Estimate

| Item | Days |
|---|---|
| PR 1, server | 1-1.5 |
| PR 2, connector and adapter | 1.5-2 |
| Tests across both | 1 |
| Harness mixed point and box runs | 1 |
| Risk reserve (vLLM scheduler, protocol compatibility) | 0.5-1.5 |
| **Total** | **5-7** |

Code review is extra. This is about one day more than the earlier rough estimate
(4-6), because the lookup result needs a protocol change.

## Decisions needed

1. Approve the design (server decides at lookup; layerwise iff deferred).
2. Which branch: a new `prototype-stage-1g` on top of 1f (proposed), or elsewhere.
3. Default R: start at 0.25 and tune from the sweep, or keep layerwise for every
   deferrable request (R = 0) until the sweep is done.
