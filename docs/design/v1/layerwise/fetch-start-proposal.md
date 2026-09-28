# Proposal: where a pipelined fetch starts

**Status:** proposal, for Track A, Track B and the storage-manager
maintainers. Nothing here is implemented. Track C's orchestration
(`run_pipelined_retrieve`) is built and tested but is not called from
production code until this is agreed.

## The problem

Layer-by-layer delivery only cuts TTFT if the forward pass starts while the
remote fetch is still running. Today it cannot, because the whole remote load
happens before vLLM schedules the request:

```text
vLLM scheduler                  LMCache daemon
  LOOKUP ---------------------> prefetch controller:
                                  L1 read-lock hits
                                  L2 lookup (Aerospike: batch_exists)
                                  reserve L1 buffers, submit L2 loads
  QUERY_PREFETCH_STATUS ------>   ... answers None until every load lands
  (polled every step)             found bitmap
  schedule request
  RETRIEVE (worker) ----------> read_prefetched_results: L1 only
  forward pass                    L1 -> GPU (per layer in layerwise mode)
```

- The scheduler polls `QUERY_PREFETCH_STATUS`
  (`vllm_multi_process_adapter.py`, `check_lookup_result`). It answers only
  when the prefetch controller has finished the L2 load
  (`StorageManager.query_prefetch_status`).
- `retrieve` reads nothing but L1 (`read_prefetched_results`, which treats a
  missing key as a lock/eviction anomaly, not a miss).

So every byte from Aerospike arrives before the forward pass begins, and
pipelining it layer by layer saves nothing. The same finding is Phase C of
the layer-band pipelining study.

## What already exists and helps

- **The controller already knows the hit before loading.** It runs a lookup
  phase, then a load phase, and calls `_report_lookup_hit` right after
  submitting the loads. `query_prefetch_lookup_hits` exposes that early
  count, but no engine uses it.
- **Layerwise mode already schedules at once.** With `use_layerwise`,
  `get_num_new_matched_tokens` reports no async load, so vLLM runs the
  request in the next step and waits per layer in `wait_for_layer_load`
  (`docs/design/v1/multiprocess/layerwise-load.md`, "Scheduling bargain").
- **A failed retrieve is recomputed.** `retrieve` returning `False` puts the
  request's blocks in `error_block_ids`, and vLLM recomputes them.

## Proposal: report the remote hit at lookup, fetch it at retrieve

```text
vLLM scheduler                  LMCache daemon
  LOOKUP ---------------------> prefetch controller:
                                  L1 read-lock hits                 (as today)
                                  L2 lookup                         (as today)
                                  eligible? -> DEFER the L2 part:
                                    no L1 reservation, no load task
                                    record the deferred keys on the session
  QUERY_PREFETCH_STATUS ------>   found bitmap, right after the lookup
  schedule request (next step)
  RETRIEVE (worker) ----------> L1 part: read-locked objects        (as today)
                                deferred part: run_pipelined_retrieve
                                  lease window -> plan -> pump -> release
                                sink: per layer, L1 objects + landed window
                                  objects -> GPU, bump the watermark
  forward pass, waiting per layer
```

1. **Eligibility is decided at lookup, per request.** A request takes the
   deferred path only if all of these hold; otherwise it takes today's path
   unchanged:
   - pipelined fetch is enabled, layerwise mode is on, and the L2 hits come
     from an adapter with a layer arrival source (Aerospike over RDMA);
   - `world_size == 1` and `num_kv_readers == 1` (see open question 3);
   - the deferred objects fit one window:
     `FetchModel.request_bytes(deferred_chunks, align) <= window_bytes`.

   Deciding at lookup means a request that cannot be pipelined never
   commits to it. That matters because after the lookup answers, vLLM
   starts the forward pass.
2. **The controller skips the load phase for deferred keys.** After the
   lookup phase it computes the trimmed plan as today. Then, instead of
   `_reserve_load_buffers` and `_submit_load_tasks`, it completes the request
   with the found bitmap and hands back the deferred keys. L1 hits stay
   read-locked exactly as today. For adapters whose lookup takes a real
   lock, the deferred keys keep their L2 lock until retrieve ends or
   `FREE_LOOKUP_LOCKS` runs. Aerospike's lookup takes no lock.
3. **The session remembers what was deferred.** `retrieve` reads the
   deferred keys from the session. It does not re-derive them, so lookup and
   retrieve cannot disagree.
4. **Retrieve fetches the deferred part.** It calls `run_pipelined_retrieve`
   for the deferred objects. The placer allocates them inside the leased
   window, following the controller's existing retention policy
   (`select_l1_retentions`). The `default` policy marks every L2-loaded
   object temporary, and temporary objects are deleted when the read
   finishes. So under `default` a finished window empties as soon as its
   retrieve completes, and reclaim-by-eviction is needed only under the
   `retain` policy.
5. **The loader copies both parts per layer.** For layer *L*, the sink
   copies the L1-resident objects and the landed window objects for *L*,
   then bumps the watermark. The pump only gates on arrival.

### Failure handling

After the lookup answers, the forward pass may already be running, and a
retrieve failure raises `LayerProgressRetrieveFailedError` inside attention.
Today the connector swallows only `LayerProgressRetrieveGenerationTimeoutError`,
so the error escapes the forward pass and kills vLLM's engine. R5
([vllm-load-failure.md](vllm-load-failure.md)) found the recoverable route.
The connector reports the step's blocks as load errors instead of raising
(Track B), and vLLM recomputes them under
`kv_load_failure_policy: "recompute"`. With that in place, abandoning the
sink is a safe way to fail a retrieve. The daemon's own recovery below
avoids the recompute, which on a hybrid model is a full prefill.

| Failure | Handling |
|---|---|
| No free window, or the request outgrows it (`LayerwiseContractError`) | Load the deferred objects whole through the adapter's normal load into fresh L1 objects, then copy the remaining layers. Slower, but the request still succeeds. |
| Transport declines or times out mid-fetch | Same fallback for the objects not yet resident. The window is released `ABANDONED` (quarantined). If the fallback fails too, abandon the sink. |
| A record is gone (evicted after the lookup) | Abandon the sink. The worker reports the step's blocks as failed, and vLLM recomputes them. |
| Loader error | As today in layerwise mode. |
| Lookup never followed by retrieve (abort, preemption) | `FREE_LOOKUP_LOCKS` / `END_SESSION` drop the deferred record. Nothing was leased, so nothing is released. |

**The fallback continues the same load; it does not abandon the sink.**
Raised by Track B. Neither obvious option works:

- *Abandon, then fall back.* Abandoning the sink sets the failure flag, and
  the worker raises `LayerProgressRetrieveFailedError` at its next wait,
  before the fallback has loaded anything.
- *Fall back under a new generation.* The worker raises
  `LayerProgressStaleGenerationError` as soon as shared memory shows a
  generation newer than the one it is waiting on
  (`LayerProgressWaiter._wait_for_watermark`).

So the retrieve keeps the sink's load open. On a transport failure (a layer
`UNSERVABLE` or timed out) the pump abandons the source only, and reports
the layers it has loaded. The retrieve then loads the objects not yet
resident whole, continues the same sink load from the next layer, and
finishes it. A loader failure still abandons both sides, since nothing can
continue it. The pump side exists: `LayerArrivalPump.run_resumable` raises
`LoadLeftOpenError` with the layers still to load. PR 2 calls it from the
fallback.

**Budget.** The pump gives up on a layer after `layer_timeout_seconds`
(2.5 s by default), which must stay below the worker's per-layer wait
(`lmcache.mp.layerwise_wait_timeout_seconds`, 5 s) so the pump always gives
up first. That leaves the fallback about the difference, 2.5 s by default,
to land the stalled layer; later layers are resident as soon as it
finishes. See open question 2.

**Reading window objects before they are complete.** The sink copies a
layer while the placed objects' later layers are still landing. Those
objects are write-reserved in L1 until the lease is released `FINISHED`, so
an L1 read (`reserve_read`) refuses them, which also keeps other requests
from seeing half-written objects. The sanctioned access is the placement's
own handle: `WindowLease.memory_obj(chunk, group)` returns each placed
object's memory (Track A's `WindowPlacement.memory_obj` implements it).
Retrieve passes those objects to the sink when it builds it (Track C), so it
leases before it builds the sink.
Reading layer *L* is safe once the source reports *L* `RESIDENT`, which is
exactly when the pump calls `load_layer(L)`.

## Alternatives

| Option | For | Against |
|---|---|---|
| **A. Report at lookup, fetch at retrieve (proposed)** | Matches the M4 decision (the pump begins the fetch). A window is held only while its fetch runs. Nothing is leased for requests that are never retrieved. | The fetch starts one scheduler step later than it could. |
| B. Lease at lookup, fetch at retrieve | A placer refusal happens before vLLM commits, so it falls back through today's path. | Holds one of a few windows while the request queues, without using it. |
| C. Fetch at lookup, attach the loader at retrieve | The fetch overlaps queueing; the data may be resident before retrieve. | Holds a window while queued. Splits the pump into begin and drain, which changes the contract. An aborted request abandons a running fetch, which quarantines the window. |

A is proposed because windows are the scarce resource (4 × 512 MiB is
already 2 GiB of L1). When the request is not queued, the step it gives up
is small next to a remote fetch. C becomes worth it only if measurements
show long queueing ahead of pipelined requests.

## What changes, and who owns it

| Change | Where | Owner |
|---|---|---|
| Deferred L2 mode: skip the load phase, complete with the found bitmap, return the deferred keys | `prefetch_controller.py`, `storage_manager.py` | Storage-manager maintainers (Track C can write it) |
| Eligibility check at lookup; deferred keys on the session | `modules/lookup.py`, session manager | Track C |
| Retrieve: pipelined part via `run_pipelined_retrieve`; whole-object fallback into fresh L1 | `lmcache_driven_transfer.py` | Track C |
| Pump leaves the sink open on a transport failure; retrieve continues it | `pump.py`, `pipelined_retrieve.py` | Track C |
| Per-layer copy of L1 plus window objects | `LayerLoadSink` | Track B |
| A failed retrieve becomes failed blocks in the same step, not an exception (R5) | `lmcache_mp_connector.py`, `vllm_multi_process_adapter.py` | Track B |
| Placer, lease (including `memory_obj`), source accessor | `ChunkPlacer`, `AerospikeLayerArrivalSource` | Track A |

It can ship in three PRs, each behind the pipelined-fetch flag:

1. The deferred mode in the controller, with tests on a fake adapter.
2. Retrieve wiring with `RecordingLayerLoadSink`, plus the fallback.
3. Enabling it with Track B's sink.

## Risks

- **A record can vanish between lookup and retrieve.** Aerospike's lookup is
  `batch_exists` and locks nothing. Today the load follows the lookup
  immediately; under this proposal it follows by at least one scheduler
  step, and by longer for queued requests.
- **Stalls move inside attention.** This is the existing layerwise
  trade-off: a slow fetch holds the whole batch's forward pass. The
  eligibility check is where to exclude requests that cannot keep ahead.
- **Metric meaning.** `MP_LOOKUP_PREFETCH_END` would report deferred L2 hits
  as found before they are loaded. The retrieve-side events must be the
  source of truth for "loaded".

## Open questions

1. ~~**A record gone at retrieve.**~~ Answered by R5
   ([vllm-load-failure.md](vllm-load-failure.md)). vLLM v0.30.0 discards a
   same-step load failure's output and recomputes it under
   `kv_load_failure_policy: "recompute"`. The failure has to be reported in
   the same step, so the daemon abandons the sink rather than finishing the
   watermark and returning `False`, whose reply can arrive a step late. The
   connector turns the resulting failure flag into failed blocks (Track B).
2. **Fallback budget.** Is the time left after the pump gives up (2.5 s by
   default) enough for a whole-object fallback on the largest eligible
   request, or does eligibility need a byte cap below the window size?
3. **More than one reader.** With `world_size > 1`, each worker's retrieve
   reads its own rank's keys, while the lease and the transport session are
   per request. One lease per (request, rank) needs concurrent fetches,
   which is a later Track A item. Until then, such requests take today's
   path.
4. **Adapters with real L2 locks.** Holding their locks from lookup to
   retrieve is correct, but it is a longer hold than today. Should the
   deferred mode be limited to adapters without locks?
