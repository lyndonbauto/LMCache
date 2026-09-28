# C9 wiring: pipelined retrieve in the daemon

**Status:** design, being built on `track/c-planning` (2026-09-28). It
implements option A of [fetch-start-proposal.md](fetch-start-proposal.md),
which Track A agreed to, and folds in their review (F1 to F5, Q2, Q3 in
[track-a-questions-for-track-c.md](track-a-questions-for-track-c.md)).

The lookup reports an L2 hit without loading it. The retrieve then leases a
window, fetches layer by layer, and hands each layer to the loader as it
lands. Everything is behind one daemon flag and off by default.

## Flow

```text
vLLM scheduler             daemon
  LOOKUP ----------------> LookupModule.lookup
                             static eligibility (below)  -> spec.l2_deferral
                           PrefetchController
                             L1 read-lock, L2 lookup          (as today)
                             trimmed plan
                             deferral accepts the plan?
                               yes: no L1 reservation, no load task,
                                    L2 locks released, hit reported,
                                    result = (retained, deferred keys)
                               no:  reserve + load                (as today)
  QUERY_PREFETCH_STATUS -> session.record_deferred_keys(keys)
  schedule (next step)
  RETRIEVE (worker) -----> LMCacheDrivenTransferModule.retrieve
                             deferred keys for this request?  no -> today's path
                             shared keys (F3): reuse readable, then policy
                             L1 part: read_prefetched_results  (as today)
                             deferred part: run_pipelined_retrieve
                               lease -> plan -> sink over lease + L1 objects
                               pump.run_resumable
                               wait for GPU copies, release lease    (F2)
  forward pass, waiting per layer
```

## Configuration

Four daemon options, all in `MPServerConfig` and on the command line:

| Option | Default | Meaning |
|---|---|---|
| `--pipelined-fetch` | off | Enable the deferred lookup and pipelined retrieve. Needs `--use-layerwise`; the daemon refuses to start otherwise. |
| `--pipelined-max-chunks` | 64 | Most chunks one request may defer. The lookup defers only up to this, and registration checks that one window holds it. |
| `--pipelined-shared-keys` | `recompute` | What a retrieve does when another request is still fetching one of its deferred keys (see "Shared keys"). `recompute` or `wait`. |
| `--pipelined-shared-wait-seconds` | 1.0 | Budget for `wait`. |

`--pipelined-max-chunks` is Track A's Q2 byte cap, expressed in chunks,
because chunks are what the lookup counts. Registration turns it into bytes
with `FetchModel.request_bytes` and checks it against the window.

## Registration

`register_kv_cache` already builds the model's `FetchModel`. With
`--pipelined-fetch` on, it also asks storage for a placer:

```python
placer = storage_manager.pipelined_window_placer(group_layout_descs)
check_window_holds_request(
    storage_manager.rdma_window_bytes(), model, max_chunks, align_bytes
)
```

`StorageManager` builds the one `RdmaWindowLeaser` at init from the adapter
that enables RDMA, since it owns the L1. Each placer gets the
prefetch policy's `select_l1_retentions` (Track A's F1). Any refusal,
whether no RDMA adapter, the path not ready, a multi-node cluster (F5), or a
window too small, is logged once. The model is then registered without a
placer, and every lookup for it takes today's path.

The record cap the objects were written under comes from the same adapter,
through a new `StorageManager.pipelined_max_record_bytes()` that follows
Track A's accessor pattern (`pipelined_max_slots_per_request`).

## Lookup: deciding to defer

`LookupModule.lookup` checks what it can know up front. It sets
`PrefetchRequestSpec.l2_deferral` only when all of these hold; otherwise the
spec carries `NO_L2_DEFERRAL` and nothing changes:

- `--pipelined-fetch` is on and the model registered a placer;
- `world_size == 1` and `num_kv_readers == 1` (Q3);
- the policy is `PREFIX`, the default.

The rest is only known after the L2 lookup, inside the controller. After
`_transition_to_load_phase` computes the trimmed plan and releases stale L1
locks, it asks `spec.l2_deferral.accepts(adapter_id, keys)`. The deferral
accepts only if:

- the whole plan is on one adapter, the one with the pipelined path;
- the plan names at most `--pipelined-max-chunks` chunks;
- the plan's slot count is at most `pipelined_max_slots_per_request()` (F4).

On accept, `_defer_request` replaces steps 3 to 5. It reserves nothing and
submits no load, releases every L2 lock, reports the hit, and completes the
request with a `PrefetchResult(retained, deferred_keys)`. L1 hits stay
read-locked as today.

**Why release L2 locks.** The only pipelined adapter, Aerospike, takes none,
and holding locks across scheduler steps would need a retrieve-side unlock
path that no adapter uses. A record that vanishes before retrieve is handled
like an evicted one (below). This answers open question 4 of the proposal.

`query_prefetch_result` still returns the retained bitmap, so existing
callers are unchanged. The new `query_prefetch_outcome` returns both parts
and pops the result once, like the old method.

## Session: remembering what was deferred

`query_prefetch_status` records the deferred keys on the session
(`Session.record_deferred_keys`). Retrieve takes them with
`Session.claim_deferred_keys()`, which returns them once, so a repeated
retrieve cannot fetch twice. Two release paths must skip them, because
deferred keys hold no L1 read lock:

- `free_lookup_locks`;
- `_release_failed_retrieve_locks`.

Releasing a lock we do not hold would drop another request's lock on the
same key, since L1 read locks are anonymous counts.

## Retrieve

When the session holds deferred keys and the model has a placer, retrieve
splits each group's in-window keys into the L1 part and the deferred part.
The L1 part is read exactly as today. The deferred part goes through
`run_pipelined_retrieve`, which now owns the sink as well as the lease:

```python
class PipelinedLoader(Protocol):
    def sink_for(self, lease: WindowLease) -> LayerLoadSink: ...
    def wait_for_copies(self) -> None: ...
    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None: ...

run_pipelined_retrieve(model, keys, max_record_bytes, placer, source, loader)
```

1. **Lease and plan.** A refusal here raises `PipelinedRetrieveRefused`.
   No sink exists yet, so retrieve loads the deferred objects whole into
   general L1 and runs today's layerwise path over all objects.
2. **Sink.** `loader.sink_for(lease)` builds the sink over one table of
   memory objects, `(object group, chunk) -> MemoryObj`. The table holds the
   L1 part's objects and the lease's `memory_obj`s. The sink reads the table
   when it loads a layer, not when it is built. That is what lets step 4 swap
   objects in.
3. **Pump.** `LayerArrivalPump(source, sink).run_resumable(plan)`.
   On success, `loader.wait_for_copies()` waits for the sink's GPU copies,
   and only then is the lease released `FINISHED` (F2). Under the `default`
   policy that frees the objects, and the window is reusable at once.
4. **Fallback** on `LoadLeftOpenError`: wait for the sink's copies, release
   the lease `ABANDONED`, then `loader.reload_whole(objects)`, which loads the
   deferred objects into fresh general-L1 objects and swaps them into the
   table. Then continue the same sink over `remaining_layers` and finish it.
   The release comes first because the lease still write-locks the same keys,
   so a fresh reservation of them would be refused.
5. **Anything else**, or a failed fallback: the sink is abandoned, which
   sets the worker's failure flag, and vLLM recomputes (R5). Retrieve returns
   `False` as a backstop.

**The sink factory is the hand-off to Track B.** `sink_for` builds Track
B's `MultiprocessLayerLoadSink` over the table, the worker's
`retrieve_generation`, the block ids and the registered schedule. Until it
exists, `--pipelined-fetch` refuses to start unless a sink factory is
registered, and tests register one that returns `RecordingLayerLoadSink`.
The sink maps the pump's generation to the worker's, because the two are
numbered independently.

**Per-layer staging.** Today's layerwise path stages whole objects to the
GPU before the first layer that needs them. Window objects are incomplete
until their last layer lands, so Track B's sink must stage per layer. This is
what their question about reading still-landing objects was about.

## Shared keys (F3)

Two requests with the same prefix can both defer the same keys at lookup,
since neither finds them in L1. At retrieve, the second one's placer would
be refused (`reserve_write(mode="new")` on a write-locked key), and so would
its fallback. So retrieve checks its deferred keys against L1 before leasing,
with a new `StorageManager.lock_resident_keys(keys)`. That call read-locks
the readable keys and reports the rest as busy (write-locked) or absent:

- **Readable** keys, such as another fetch that finished under `retain`,
  are read-locked and move to the L1 part. Both policies reuse them.
- **Absent** keys stay deferred and are fetched.
- **Busy** keys, which another request is still fetching, depend on
  `--pipelined-shared-keys`:
  - `recompute`: fail the retrieve. vLLM recomputes the request.
  - `wait`: poll every 10 ms, up to `--pipelined-shared-wait-seconds`.
    Keys that become readable are reused, and keys that vanish are fetched.
    Under `default`, the other request's fetch frees its objects when it
    finishes, so they usually vanish. A key still busy at the deadline fails
    the retrieve.

**The budget is shared with the worker.** The worker's wait for layer 0
starts when it enters attention, and it covers leasing, any shared-key wait,
the pump's first layer (2.5 s) and any fallback. It is 5 s by default. So
the `wait` budget must stay small, and the daemon refuses to start if it
plus the pump's layer timeout reaches the worker's wait.

## Failure summary

| Where | What happens | Result |
|---|---|---|
| Lookup: not eligible | Today's path | Unchanged |
| Retrieve: busy shared key (`recompute`, or `wait` past its budget) | Sink never begun; failure flag set | vLLM recomputes |
| Lease or plan refused | Whole-object load into general L1, then today's layerwise path | Served, slower |
| Layer declined or timed out | Fallback continues the same load | Served, slower |
| Record gone, fallback fails, or loader error | Sink abandoned | vLLM recomputes |
| Lookup never followed by retrieve | `END_SESSION` drops the deferred record; nothing was leased | Nothing to undo |

## Risks

- **A generation timeout is silent.** The connector swallows
  `LayerProgressRetrieveGenerationTimeoutError` and computes on KV that was
  never loaded. Any retrieve that fails to begin its load within the
  worker's wait produces wrong output, not an error. That is true today; the
  pipelined path adds leasing and the shared-key wait before the load
  begins, which is why the `wait` budget is checked at startup. Track B
  should make that timeout report failed blocks, as R5 does for the others.
- **A pipelined retrieve holds a GPU worker thread** for the whole fetch.
  `RETRIEVE` runs with client affinity, so the same worker's next STORE
  waits behind it. The windows (4 by default) bound how many run at once.
- **Metric meaning.** `MP_LOOKUP_PREFETCH_END` counts deferred hits as found
  before they are loaded (unchanged from the proposal).

## Pieces and owners

| Piece | Where | Owner |
|---|---|---|
| Deferred mode, `PrefetchResult`, `query_prefetch_outcome` | `prefetch_controller.py`, `storage_manager.py`, `api.py` | Track C |
| Config options and startup checks | `multiprocess/config.py`, `engine_context.py` | Track C |
| Leaser at init, `pipelined_window_placer`, `rdma_window_bytes`, `pipelined_max_record_bytes`, `lock_resident_keys`, whole-object load | `storage_manager.py`, adapter accessors | Track C, reviewed by Track A |
| Eligibility, deferred keys on the session, release paths | `modules/lookup.py`, `session.py` | Track C |
| `run_pipelined_retrieve` with the loader protocol and fallback | `layerwise/pipelined_retrieve.py` | Track C |
| Retrieve wiring and the sink factory hook | `lmcache_driven_transfer.py` | Track C |
| The sink behind `sink_for`, staging per layer | `multiprocess/layerwise_sink.py` | Track B |
| Generation timeout reported as failed blocks | `lmcache_mp_connector.py` | Track B |

Built in this order, each step committed with its tests: deferred mode;
storage accessors and registration; lookup and session; orchestration;
retrieve wiring.
