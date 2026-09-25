# Track B -- Consumption: goals and acceptance criteria

Read [exercise-goal.md](exercise-goal.md) for why, then
[system-design.md](system-design.md) for how the tracks fit together. This
document is what Track B is responsible for and how we will know it is done.

## What this track is for

Take a layer that has landed in host memory, get it onto the GPU, and unblock
vLLM's attention for that layer -- without the engine ever waiting for layers
it does not yet need.

## Scope

You own:

- `lmcache/v1/multiprocess/layerwise_schedule.py`
- `lmcache/v1/multiprocess/layer_progress.py`
- `lmcache/v1/multiprocess/object_group_transfer.py::transfer_kv_layerwise_h2d`
- `lmcache/v1/multiprocess/transfer_context/worker_transfer.py`, layerwise parts
- `lmcache/integration/vllm/lmcache_mp_connector.py`, including
  `requires_piecewise_for_cudagraph` and `wait_for_layer_load`
- `docs/design/v1/multiprocess/layerwise-load.md`

You do not own: anything that talks to Aerospike or RDMA, and you do not
decide *which* layers get fetched.

## What you implement

`lmcache/v1/layerwise/contract.py::LayerLoadSink`. That file is frozen; if it
is wrong, say so and we change it together rather than working around it.

## Acceptance criteria

### B1. The contract is implemented and passes the conformance suite

The multiprocess loader satisfies `LayerLoadSink`, and `tests/v1/layerwise/`
passes against it, not only against `RecordingLayerLoadSink`.

### B2. Attention for layer N waits for layer N and nothing later

`wait_for_layer_load(layer_name)` returns as soon as layer N's copy has
completed. Prove it with a source that delivers layers slowly: attention for
layer 0 must proceed while later layers are still outstanding. A test where
all layers are already resident cannot distinguish a working pipeline from a
barrier, so it does not count as evidence.

### B3. Launch order and event ordering hold

Launches follow `LayerwiseSchedule` order, global layer-major, and the
per-ordinal CUDA event is recorded **before** the progress watermark is
bumped. Both already have tests; keep them passing. Transfers share a stream,
so out-of-order issue makes an earlier layer appear ready before its copy was
queued -- wrong output, no crash.

### B4. Failure wakes every waiter

`abandon_load` fails all parked waiters. A declined layer upstream must
surface in vLLM as a fallback to a whole-request load, never as a hang. Test
it with `UnservableLayerArrivalSource`, which exists so this path is actually
reached rather than merely present.

### B5. The default vLLM configuration works

Layerwise must run in vLLM's default config, not only with CUDA graphs
disabled. The mechanism is already there: the connector overrides
`requires_piecewise_for_cudagraph` keyed on `lmcache.mp.use_layerwise`, and
vLLM's config layer then selects `CUDAGraphMode.PIECEWISE`.

Two things to confirm rather than assume. First, that vLLM loads LMCache's
connector class and not its own vendored copy -- `_resolve_lmcache_mp_connector()`
prefers the external one, but `LMCACHE_USE_UPSTREAM_MP` forces the builtin, so
check which is live in your environment. Second, that the negotiation actually
fires end to end; a unit test on the override method does not prove vLLM
consumed it.

### B6. Shared memory has a clean lifetime

The worker holds the `SharedMemory` object for the full lifetime of the
progress record and closes and unlinks it on teardown. Dropping the handle
early caused a use-after-free of the memoryview mid-retrieve. Cover the
teardown path, including teardown after a failed load.

### B7. Per-batch setup stays hoisted

Per-batch work runs once per batch, not once per layer per batch. A test
asserts the call counts; keep it.

### B8. A first TTFT number

**This is the highest-value item on the track and you do not need Track A to
do it.**

Drive the load path from `ScriptedLayerArrivalSource` with delays
representing a plausible remote fetch, and measure TTFT against the same
prompt with layerwise disabled on the same GPU. Report the number with the
assumptions it rests on.

The project currently has no TTFT measurement of any kind, which means nobody
knows whether the overlap is worth the complexity. A number from a simulated
source is not the final answer, but it is the difference between an informed
project and a hopeful one. Get it early; if it is disappointing, that changes
what everyone else should be doing.

### B9. No RDMA anywhere in your test suite

If a Track B test needs an RDMA fabric or an Aerospike server, the split has
broken. Use the fakes.

## How to work without the rest of the system

`ScriptedLayerArrivalSource` is a complete stand-in for Track A and delivers
nothing until you tell it to, which is what lets you simulate a slow fetch
precisely. `UnservableLayerArrivalSource` covers the failure path. Build
`LayerFetchPlan` directly rather than waiting on Track C's planner.

## Done

B1--B7 and B9 are green, and B8 has a written number with its assumptions
stated.

## Implementation log

A running record of what Track B has built, the decisions made along the way,
and how each step was tested. Newest entries at the bottom. Tests run in the
WSL2 environment on an RTX 3060 laptop unless stated otherwise; WSL2 cannot do
cross-process CUDA IPC, so anything that depends on it is marked as not yet
proven here.

### Step 1 -- How the sink gets its per-request state (2026-09-24)

The frozen `LayerLoadSink` methods carry only a generation and layer ids, but
copying a layer needs the host buffers, GPU block ids, cache context, progress
record and event pool of one specific retrieve. Decisions:

- **D1. Two generations, kept separate.** The *fetch generation* comes from the
  transport through the pump and is what the four contract methods receive.
  The *retrieve generation* is the one the vLLM worker assigned and waits on
  in the shared progress record. They are different number spaces owned by
  different processes. The sink is bound to one retrieve generation at
  construction and uses the fetch generation only for the contract's own
  checks (stale-generation rejection, matching an abandon to its load).
  Conflating them would let a waiter wake on the wrong retrieve.
- **D2. One sink per retrieve, single use.** The sink is constructed with that
  retrieve's state already bound, and accepts exactly one `begin_load`. A
  second `begin_load` raises `LayerwiseContractError`, the same error the
  contract already specifies for "a load is in progress". Reuse would
  republish the same retrieve generation and reset its watermark under
  waiters that may still be parked on it.
- **D3. The sink depends on a small launcher, not on transfer internals.**
  The sink owns contract bookkeeping (ordering, generation, finish/abandon
  rules). The GPU work sits behind a three-method `LayerLauncher` protocol --
  `begin()`, `launch_layer(layer_id)`, `mark_failed()` -- implemented in
  production by the existing layerwise H2D machinery. This keeps all GPU
  transfer code in `object_group_transfer.py`, and lets the sink be tested on
  CPU with a fake launcher (B9).
- **D4. `begin_load` must receive exactly the schedule's layer order.** The
  worker waits on a watermark over schedule ordinals. If a fetch plan skipped
  a scheduled layer, that layer's ordinal would either never be reached (a
  hang in attention) or be passed with no data behind it (wrong tokens). The
  sink rejects any mismatch with `LayerwiseContractError`.

None of the four frozen method signatures change. What changes is the
*constructor* of Track B's own skeleton, which previously took no arguments.

**For Track C:** the pump cannot construct this sink by itself; whatever
wires a real request to the pump must build the launcher for that retrieve
and pass it in. The factory lives with the retrieve path in
`lmcache_driven_transfer.py`, because that is where all of the per-retrieve
state already exists.

**Tested:** baseline before any code change -- `tests/v1/layerwise`,
`test_object_group_layerwise_transfer.py`, `test_layer_progress.py`,
`test_layerwise_schedule.py`: 57 passed.

### Step 2 -- Split the whole-retrieve loop into one-layer launches (2026-09-24)

`transfer_kv_layerwise_h2d` used to copy every scheduled layer in a single
call, so nothing could hold a layer back until the transport said it had
arrived. It is now a thin wrapper over a new class,
`object_group_transfer.LayerwiseH2DRetrieve`, with three phases:

- `begin()` -- builds the per-batch descriptors once (B7 stays hoisted) and
  then publishes the retrieve generation.
- `launch_layer(layer_id)` -- copies exactly the next layer in schedule
  order, records its event, then advances the watermark (B3 order kept).
- `mark_failed()` -- publishes a failure so waiters wake with an error.

Decisions:

- **D5. The launcher enforces schedule order itself**, independently of the
  sink. An out-of-order `launch_layer` raises `ValueError` and changes
  nothing, so a caller bug cannot silently advance the watermark.
- **D6. Setup failures leave the record untouched.** Descriptor building
  runs before the generation is published, exactly as before the split, so
  the existing retrieve path's own failure reporting is unchanged. Copy
  failures inside `launch_layer` mark the retrieve failed before re-raising,
  also as before.
- **D7. `mark_failed()` works in every state.** If the generation was never
  published it publishes it first, so the failure lands on *this* retrieve
  rather than on whatever the record held before. Marking a completed
  retrieve failed is allowed on purpose: a waiter that has not returned yet
  falls back to a full load, which is slower but never wrong.

The call site in `lmcache_driven_transfer.py` is unchanged; it still calls
`transfer_kv_layerwise_h2d`, which now drives the class layer by layer.

**Tested:**

- The two pre-existing tests in `test_object_group_layerwise_transfer.py`
  (event recorded before watermark; setup once per batch) pass unchanged,
  which is the evidence the split did not alter behaviour.
- Eight new tests in the same file: one launch publishes exactly one layer;
  hybrid groups launch interleaved `0, 1, 2, 3`; an out-of-order launch is
  rejected without advancing; launch before `begin` and past the last layer
  are rejected; a failed copy marks the retrieve failed and blocks further
  launches; failing before `begin` publishes this generation; generation `0`
  is rejected. File total: 10 passed.
- Retrieve-path tests that reach this code (`test_lmcache_driven_*`,
  `test_event_ipc_handle_path.py`): 22 passed.
- `ruff check` and `ruff format --check` clean on the changed files.

### Step 3 -- Implement `MultiprocessLayerLoadSink` (2026-09-24)

`lmcache/v1/multiprocess/layerwise_sink.py` now implements the four contract
methods on top of a `LayerLauncher` protocol (`begin`, `launch_layer`,
`mark_failed`). `LayerwiseH2DRetrieve` from Step 2 satisfies that protocol, so
production wiring is:

```python
MultiprocessLayerLoadSink(schedule, LayerwiseH2DRetrieve(...))
```

Behaviour, per method:

- `begin_load` -- refuses a used sink, the reserved generation `0`, or any
  layer order other than the schedule's (D4); then runs launcher setup.
- `load_layer` -- an unknown layer raises `LayerNotInPlanError`; an
  out-of-order one raises plain `LayerwiseContractError`, matching the
  contract text (the old skeleton docstring used `LayerNotInPlanError` for
  both). Nothing is copied on either error.
- `finish_load` -- `StaleGenerationError` for the wrong generation or no
  active load; `LayerwiseContractError` naming any layer never issued.
- `abandon_load` -- marks the retrieve failed (see D8); a no-op after a clean
  finish or a previous abandon.

Decisions:

- **D8. `abandon_load` ignores the generation it is given.** The contract's
  fake sink only abandons when the generation matches the active load. That
  is wrong for this sink: if `begin_load` itself was refused, no generation
  was ever accepted, the pump still calls `abandon_load`, and a matching rule
  would silently skip it -- leaving the worker parked until its timeout. Since
  a sink is bound to exactly one retrieve (D2), there is no other load an
  abandon could wrongly hit, so any abandon before a clean finish fails the
  retrieve.
- **D9. Abandon after a clean finish does nothing.** Every layer's data
  landed; failing it then would push the worker into a needless full reload.
- **D10. The sink stays thread-unsafe**, as the contract states calls come
  from a single thread (the pump's). No locking was added.

Also fixed along the way: `_LayerwiseBatchDescriptor.object_group_buffers`
was typed `tuple[object, ...]` but always holds tensors (the base cache
context returns `torch.Tensor`). mypy flagged this once torch was installed;
it is now `tuple[torch.Tensor, ...]`. No behaviour change.

The skeleton shape tests were updated: the sink now needs a schedule and a
launcher to construct, and "refuses to pretend it worked" now asserts a
`LayerwiseContractError` for loading before a load begins, instead of
`NotImplementedError`.

**Tested:**

- `tests/v1/layerwise/test_multiprocess_sink.py` (new, 18 tests, CPU): full
  load order; each `begin_load` refusal; single use; ordering vs unknown-layer
  errors; stale and incomplete `finish_load`; every abandon case. Three run
  through the real `LayerArrivalPump`: a layer is copied only after it
  arrives, with later layers still outstanding (B2 at the logic level); an
  `UnservableLayerArrivalSource` fetch fails the retrieve (B4); a plan that
  does not match the schedule still wakes waiters (D8).
- Two new integration tests in `test_object_group_layerwise_transfer.py`
  connect pump, sink, the real `LayerwiseH2DRetrieve`, the real progress
  record and the real `LayerProgressWaiter`: with every layer delivered, the
  worker can wait on each layer and the watermark reaches the end; with an
  unservable fetch, the worker's wait fails with
  `LayerProgressRetrieveFailedError` in under a second despite a 30-second
  timeout -- the B4 property on the real classes. File total: 12 passed.
- `tests/v1/layerwise`: 48 passed.
- `ruff check`, `ruff format --check`, `isort --check-only` clean on all
  changed files; mypy clean on both changed source files.

**Not yet proven:** the same flow across two processes with real CUDA IPC
events, which WSL2 cannot run. The retrieve path in
`lmcache_driven_transfer.py` does not construct a sink yet; wiring a live
request to the pump is the integration step (C9).

### Step 4 -- Broad regression and GPU check (2026-09-24)

- **CPU regression**, `pytest -m 'not cuda' tests/v1/layerwise
  tests/v1/multiprocess`: 843 passed, 1 skipped, 16 failed. All 16 failures
  are in `test_mq.py` (spawned clients time out) and
  `http_apis/test_common_api.py` (run-script tests). **They are
  pre-existing:** with `object_group_transfer.py` temporarily restored to
  `HEAD`, the same two files fail 17 tests (one `test_mq` timeout flickers
  between runs). `test_mq` spawns client processes and fits the WSL2
  cross-process limitation; neither file touches the code changed here.
  Worth rerunning on native Linux CI before anyone relies on them.
- **GPU harness**, `test_layerwise_gpu_overlap.py` on the RTX 3060: 1 passed.
- **Line endings:** this checkout uses `core.autocrlf=true`, so files are
  CRLF on disk. Windows git reports the true diff; WSL's git (no autocrlf)
  shows every line as changed. Commit from Windows git.

**Status against the acceptance criteria:**

| Item | State |
| --- | --- |
| B1 contract implemented, conformance suite passes | Done on CPU (`tests/v1/layerwise`, 48 passed) |
| B2 layer N ready while later layers outstanding | Ordering proven at logic level with the real pump, and on GPU in one process. Per-layer staging of arriving data proven with real tensors on CPU and GPU (Step 6). Needs the C9 hand-off to read partly written objects; cross-process not proven |
| B3 launch order, event before watermark | Pre-existing tests still pass; enforced per layer by `LayerwiseH2DRetrieve` |
| B4 failure wakes every waiter | *Wake with an error* proven on the real record and waiter (< 1 s, not at timeout). The *fall back to a whole-request load* half is not built: the error propagates into attention (Review 1, R5) |
| B5 default vLLM config (piecewise CUDA graphs) | Not started; needs a real vLLM run on native Linux |
| B6 shared-memory lifetime | Done (Step 7): teardown after a failed load, already-removed segment, double close, re-register, stale segment, failed register; daemon no longer deletes live workers' segments on exit |
| B7 per-batch setup hoisted | Pre-existing call-count test still passes |
| B8 first TTFT number | First *simulated* number done (Step 8): 40 ms saved at line rate, 119 ms transfer-bound, of a 32-layer, 2048-token prefill. Not a vLLM TTFT |
| B9 no RDMA in Track B tests | Holds; every new test uses the fakes |

**Next:** B6's teardown-after-failed-load test, then B8. Both can run on
this laptop; B8 in its simulated, single-process form. (Superseded by the
order in Review 1.)

### Review 1 -- Gaps found in Steps 2 and 3 (2026-09-24)

A review of the new code against how it will actually be driven. Items are
ordered by severity. "New" means introduced or exposed by this work;
"pre-existing" means it was already in Track B-owned code.

**Correctness**

- **R1. Staging copies whole objects, so later layers can be stale (new).**
  `LayerwiseH2DRetrieve._launch_scheduled` copies each memory object to its
  GPU staging buffer in full the first time any layer of its object group is
  launched, then reuses that staged copy for every later layer. That is
  correct for `transfer_kv_layerwise_h2d`, where the retrieve only starts
  after the L2 prefetch has finished and every object is complete. It is
  **wrong for arrival-driven loading**, which is the whole point of the sink:
  when the pump launches layer 0, the transport is still writing layers 1..N
  into the same host object, so their bytes are copied before they arrive
  and never refreshed. Result: silently wrong KV for every layer but the
  first. The Step 3 tests use stubbed copies and could not see this.
  Fix: stage per layer -- copy only layer N's byte ranges (`kv_size` disjoint
  planes per kernel group, see `layerwise_transfer_data_model.md`) at
  `launch_layer(N)` time. Related: today's retrieve reads objects through
  `read_prefetched_results`, which refuses write-locked objects; an
  arrival-driven retrieve needs a sanctioned way to read an object whose
  later layers are still being written. That hand-off is part of C9 but
  shapes this fix.
- **R2. A failed retrieve can fail the next one's waiters (pre-existing,
  `layer_progress.py`).** `LayerProgressWaiter` checks the failure flag
  before it checks the generation. After retrieve `G` fails, a worker that
  starts waiting on `G+1` before the daemon has published `G+1` sees `G`'s
  flag and raises `LayerProgressRetrieveFailedError` for a retrieve that did
  nothing wrong. Fix: honour the flag only when the snapshot's generation is
  the waiter's own.
- **R3. A late failure can poison a newer retrieve (new).**
  `LayerwiseH2DRetrieve.mark_failed` sets the flag on whatever generation the
  record currently holds, and if it never began it republishes its own,
  possibly older, generation. A late abandon from an old retrieve would
  therefore fail -- or rewind -- a newer one. The MP server serialises
  retrieves per worker today, which hides this, but the pump's lifetime is not
  tied to that serialisation. Fix: skip when the record already holds a newer
  generation.
- **R4. If the transport refuses the fetch, nobody wakes the worker, and the
  connector then proceeds without KV (boundary with Track C).**
  `LayerArrivalPump.run` calls `begin_fetch` outside its `try`, so a refused
  fetch never reaches `abandon_load`. The retrieve generation is never
  published, the worker's wait times out with
  `LayerProgressRetrieveGenerationTimeoutError`, and
  `LMCacheMPConnector.wait_for_layer_load` deliberately swallows that one
  error and lets attention run -- on KV that was never loaded. Two parts:
  the pump (Track C) should abandon the sink on a refused fetch (D8 makes a
  caller-side `abandon_load` backstop safe); and the connector's swallow needs
  investigating, since in layerwise mode it turns a load failure into wrong
  tokens.
- **R5. B4's "fall back" half does not exist yet (pre-existing).** When a
  wait fails, `LMCacheDrivenTransferContext.wait_for_layer_load` re-raises,
  and the connector does not catch `LayerProgressRetrieveFailedError` or
  `LayerProgressRetrieveProgressTimeoutError`, so the error surfaces inside
  vLLM's forward pass rather than as a recompute. Fixing it needs a vLLM-side
  decision on how to abandon a request mid-forward.
- **R6. The two timeouts are ordered the wrong way (pre-existing
  defaults).** The worker gives up on a layer after 5 s
  (`layerwise_wait_timeout_seconds`); the pump waits 30 s
  (`DEFAULT_LAYER_TIMEOUT_SECONDS`). A slow layer makes the worker fail first
  while the pump is still waiting. The pump's timeout should be strictly
  smaller, checked at startup -- the same pattern as the RDMA write-lock TTL
  check.
- **R7. D4 may be too strict (needs Track C).** `begin_load` requires the
  plan to cover every scheduled layer. The existing retrieve already publishes
  some ordinals with no transfer, for kernel groups it does not serve (aux
  groups, a CacheBlend leg). If Track C's plans omit those layers, every such
  request would be refused. A possible refinement: allow omitting exactly the
  layers whose object group this retrieve does not serve, and publish their
  ordinals automatically.

**Optimisation**

- **O1. Per-layer lookups are not hoisted.** Each `launch_layer` calls
  `get_kernel_group_kv_pointers`, `get_shape_desc`,
  `get_slots_per_chunk_in_sw` and `get_engine_kv_format`, and rebuilds the
  pointer list, once per batch. These depend only on the kernel group, so
  they belong in `begin()`. Small, but it is the latency-critical path.
- **O2. The pump busy-polls.** A 100 µs sleep loop per in-flight request
  costs CPU, and CPU offload is half the reason for the RDMA work. The
  system design already allows the contract to grow a blocking wait; worth
  raising with Tracks A and C.
- **O3. Membership check.** `load_layer` scans a tuple per call (O(L) per
  layer). Negligible at ≤ 128 layers; a `frozenset` makes it O(1).

**Test gaps**

- No test exercises the real launcher with data that arrives over time, so
  R1 went undetected. The GPU harness proves the waiting mechanism but
  bypasses `LayerwiseH2DRetrieve`.
- After a copy failure the sink stays in its loading state; further
  `load_layer` calls surface the launcher's `RuntimeError`. Behaviour is
  safe, but a dedicated failed state would give a clearer error.

**Revised order of work:** R2 and R3 (small, self-contained, testable now);
R1 (per-layer staging, plus a test that feeds data progressively); O1 while
in that code; then B6 and B8. R4, R5, R6 and R7 need Track C or vLLM-side
decisions and are raised rather than fixed here.

### Step 5 -- Fix R2 and R3: failures stay with their own generation (2026-09-24)

- **R2 fixed** in `LayerProgressWaiter` (`layer_progress.py`). The failure
  flag now counts only when the snapshot's generation equals the waiter's.
  A flag left by an older generation is ignored and the waiter keeps
  polling until its own generation is published, or times out as before. A
  newer generation still raises `LayerProgressStaleGenerationError`. The
  unreachable `else` branch after the generation checks was removed.
- **R3 fixed** in `LayerwiseH2DRetrieve.mark_failed`. It now reads the
  record's current generation first: newer means write nothing; equal means
  set the flag; older (never published) means publish this generation, then
  set the flag. It assumes a single writer per record, which the MP server's
  per-worker serialisation of retrieves guarantees.

Not changed: `_publish_layerwise_retrieve_terminal` in
`lmcache_driven_transfer.py` also republishes a generation unconditionally.
It only runs inside the serialised retrieve handler, so it cannot rewind a
newer retrieve today. Worth the same guard if that ever changes.

**Tested:**

- New `test_a_previous_generations_failure_does_not_fail_the_next_wait`:
  generation 1 fails, then a waiter on generation 2 starts before 2 is
  published. It waits and succeeds. **Mutation check:** against the original
  `layer_progress.py` this test fails with exactly the spurious
  `LayerProgressRetrieveFailedError` R2 describes, so the bug was real and the
  test catches it.
- New `test_a_late_failure_leaves_a_newer_retrieve_untouched` (record keeps
  generation 5, its watermark, and no failure flag) and
  `test_failing_after_an_older_generation_publishes_this_one`.
- `test_object_group_layerwise_transfer.py`, `test_layer_progress.py` and
  `tests/v1/layerwise`: 75 passed. Ruff check and format clean.

### Step 6 -- Fix R1: stage one layer at a time; fold in O1 and O3 (2026-09-24)

**R1 fixed.** `LayerwiseH2DRetrieve` now takes a keyword-only
`staging: LayerStaging`:

- `PER_LAYER` (the default) copies only the launched layer's bytes, read at
  the moment it launches. A layer is located inside its kernel group's
  staging view from the view's own strides: `kv_size` disjoint planes in the
  `(kv_size, num_layers, slots, hidden)` layout used on CUDA and CPU, or one
  block in MUSA's `(num_layers, slots, hidden)` MLA layout. The offset of a
  kernel group's region is the difference between its staging view's pointer
  and its object group buffer's pointer; the memory object is laid out
  byte-for-byte like that buffer, so the same offsets address both.
- `WHOLE_OBJECT` keeps the old behaviour. `transfer_kv_layerwise_h2d` passes
  it explicitly, because its objects are complete before it starts and one
  copy per object is cheaper than `kv_size` copies per layer.

New helper `gpu_ops.lmcache_memcpy_async_h2d_range(memory_obj, gpu_buffer,
byte_offset, nbytes)`, the partial counterpart of
`lmcache_memcpy_async_h2d`. For lazy-allocator objects it shifts source,
destination and the allocator's virtual host offset by the same amount, which
is what the native copy needs to split at pin-chunk boundaries.

Decisions:

- **D11. `PER_LAYER` is the default.** A caller that forgets to choose gets
  a correct, slightly slower load rather than a fast, silently wrong one.
- **D12. GDS objects are refused in `PER_LAYER`**, at `begin()` and before the
  generation is published. They transfer whole only; staging them per layer
  is not possible, and silently falling back to whole-object staging would
  reintroduce R1.
- **D13. Cost accepted.** Per-layer staging issues `kv_size` small copies per
  chunk per layer instead of one per object. Whether that launch overhead
  matters is a question for B8's measurement, not a reason to keep incorrect
  staging.

**O1 done:** the kernel-group-invariant kernel arguments (KV pointers, shape
descriptor, slots per chunk, engine format) are looked up once in `begin()`
instead of once per layer per batch. **O3 done:** the sink's membership check
uses a `frozenset`.

**Tested:**

- `test_per_layer_staging_copies_each_layer_as_it_arrives`, parametrised over
  both layouts, with **real tensors**: the host object starts empty; each
  layer is written, then launched; after every launch each already-launched
  layer holds the value it had at its own launch, and every unlaunched layer's
  staging bytes are still the sentinel.
- `test_per_layer_staging_on_gpu`, same scenario with pinned host memory and a
  real CUDA staging buffer, so the async H2D range copies actually run. Both
  layouts pass on the RTX 3060.
- `test_whole_object_staging_would_miss_a_late_arrival`: the same arrival
  sequence under `WHOLE_OBJECT` leaves layer 2's staging at its pre-arrival
  value. This documents R1 and is the evidence that the per-layer test checks
  something the old mode fails.
- GDS objects rejected before the generation is published; range copy writes
  exactly its range; empty, negative and overrunning ranges raise.
- Track B suites plus the retrieve-path tests: 108 passed. Ruff, isort and
  mypy (four source files) clean.

**Still open from Review 1:** R4, R5, R6 and R7 (need Track C or vLLM-side
decisions), O2 (pump polling, Tracks A/C). The hand-off that lets a retrieve
read an object whose later layers are still being written (part of C9) is
still needed before this path can run on live data.

### Step 7 -- B6: a clean lifetime for the shared progress segment (2026-09-24)

The worker creates the progress segment and the daemon attaches to it.
Reviewing that lifecycle turned up three defects, one of them serious:

- **S1. The daemon deleted live workers' segments on exit (pre-existing).**
  CPython registers even *attached* segments with the attaching process's
  resource tracker, which unlinks them when the process exits. Verified on
  this machine's Python 3.10 before changing anything: a child interpreter
  that attaches and exits removes the segment, and the owner's own `unlink()`
  then raises `FileNotFoundError`. So a daemon exit or restart deleted every
  live worker's segment. Fixed with `layer_progress.attach_layer_progress_shm`,
  which attaches untracked (`track=False` on 3.13+, an explicit unregister
  before), used at both daemon attach sites in `lmcache_driven_transfer.py`.
- **S2. Worker teardown was not failure-tolerant (pre-existing).**
  `LMCacheDrivenTransferContext.close()` called `unlink()` unguarded, so an
  already-removed name (S1, or an operator cleaning up) aborted teardown
  halfway. Teardown now goes through one private method that drops the
  waiter, schedule and active generation *before* closing the segment (so a
  wait starting during teardown finds no waiter rather than a released
  buffer), and treats an already-removed name as success.
- **S3. A failed registration leaked the segment (pre-existing).** If
  `register()` failed after creating the segment (config mismatch, wrong
  handle count, timeout), the named segment stayed behind. The post-creation
  part of `register()` now releases it before re-raising.

Also:

- `wait_for_layer_load` reads the waiter and schedule once, so teardown
  cannot swap them out between the check and the call. Its docstring claimed
  it raises `RuntimeError` when unregistered; it returns immediately, and the
  docstring now says so.
- New `LayerProgressRecord.from_shared_memory(segment)`, used at all three
  places a record is built from a segment. A closed segment's `buf` is
  `None`; mypy flagged the three bare `LayerProgressRecord(segment.buf)` calls
  (pre-existing, not previously type-checked with torch installed), and the
  new constructor turns that case into a clear `ValueError`.

**Not addressed:** a wait already *in progress* when another thread tears
the context down can still hit a released buffer and raise `ValueError`.
Closing that window needs a lock around every wait, on the attention hot
path; teardown during a live forward pass is a shutdown-only race, so it is
recorded rather than fixed.

**Tested** (`tests/v1/multiprocess/test_layer_progress_lifetime.py`, new, 9
tests, real POSIX shared memory):

- Teardown after a failed load: the worker's wait raises
  `LayerProgressRetrieveFailedError`, `close()` removes the segment, and a
  wait after teardown is a no-op.
- Teardown after another process already removed the name (reproduced by a
  child interpreter doing a plain attach and exiting); double `close()`;
  re-registering after teardown starts with a clean record; a crashed
  worker's stale segment (with its failure flag set) is replaced; a failed
  registration removes the segment; a record cannot attach to a closed
  segment.
- Daemon side, two child-interpreter tests: a plain attach removes the
  segment when the attacher exits (the hazard, kept as evidence), and
  `attach_layer_progress_shm` leaves it in place with no "leaked
  shared_memory" warning.
- **Mutation check:** against the original `worker_transfer.py`, exactly the
  two tests for S2 and S3 fail.
- Track B suites plus worker-liveness, IPC-reclaim, event-IPC and GPU tests:
  121 passed. Broad `-m 'not cuda'` regression over `tests/v1/multiprocess`
  and `tests/v1/layerwise`: 861 passed; the 17 failures are the same
  pre-existing `test_mq.py` and `test_common_api.py` ones as in Step 4 (the
  count flickers between 16 and 17 with one `test_mq` timeout).
- Ruff, isort and mypy clean on the three changed source files.

### Step 8 -- B8: a first, simulated TTFT number (2026-09-24)

**This is a mechanism-level simulation, not a vLLM TTFT.** A real vLLM run
with layerwise on and off needs a live request wired through the pump (C9)
and cross-process CUDA IPC, which WSL2 cannot do. What this measures instead
is how much of a remote KV fetch the Track B machinery hides behind prefill
compute, on a real GPU, with every stage real except the network.

**Tool:** `benchmarks/layerwise/simulate_ttft.py`. One simulated prefill is 32
layers at 8 MiB of KV each (Llama-3-8B shape, 2048 prompt tokens, 4 KiB per
token per layer). Each layer passes three real stages: arrival on a fixed
schedule (the fake transport), a real pinned H2D copy on a transfer stream,
and real GPU matmuls on a compute stream calibrated to a target time. Three
modes are timed from the same start instant to the end of the last layer's
compute:

- **barrier** -- today's behaviour: wait for every layer, one contiguous copy
  of all of them (the most favourable case for this mode), then compute;
- **streamed copy** -- copy each layer as it lands, but compute only after the
  last copy. Needs no per-layer waits in vLLM;
- **layerwise** -- the real Track B path: `LayerArrivalPump` ->
  `MultiprocessLayerLoadSink` -> per-layer copy + event + watermark through
  the real `LayerProgressRecord` and event pool -> the real
  `LayerProgressWaiter` before each layer's compute.

Per-layer arrival times come from the M0 bandwidths in
`layerwise_transfer_data_model.md` (12.2 GB/s NIC ceiling, 1.88 GB/s single
object). Compute targets come from that document's transfer/compute ratios,
not from this GPU's speed, because the ratio decides the overlap.

**Results** -- RTX 3060 Laptop GPU, median of 9 interleaved runs per mode.
A second full run matched to within ~1 ms everywhere.

| Scenario | Remote ms/layer | H2D ms/layer | Compute ms/layer (in-run) | Barrier ms | Streamed ms | Layerwise ms | Saved ms | of which: copy overlap | of which: compute overlap |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Line rate, compute-bound | 0.688 | 0.66 | 1.98 | 107.5 | 86.3 | 67.3 | **40.2** (37%) | 21.2 | 19.0 |
| Line rate, balanced | 0.688 | 0.66 | 0.68 | 66.4 | 45.3 | 25.0 | **41.5** (62%) | 21.2 | 20.3 |
| Single-object rate, transfer-bound | 4.462 | 0.65 | 3.19 | 266.0 | 244.8 | 146.7 | **119.4** (45%) | 21.3 | 98.1 |
| Near-complete hit (little prefill) | 0.688 | 0.65 | 0.04 | 46.2 | 25.0 | 23.6 | **22.6** (49%) | 21.1 | 1.5 |

A three-stage pipeline model (`modeled_ttft_ms`) predicts every measured
value to within about 3%. Layerwise measured minus model -- the cost of the
pump's polling, per-layer events, watermark and host waits -- is 0.1 to
2.5 ms over 32 layers, at most about 80 µs per layer.

**What the numbers say:**

1. **The mechanism works and costs little.** Layer 0 is consumed while later
   layers are still arriving, the pipeline behaves as modelled, and the
   machinery's own overhead is small next to the savings.
2. **About 21 ms of every saving needs no vLLM changes.** It comes from
   overlapping the host-to-GPU copy with the fetch, which "streamed copy"
   gets with an ordinary barrier before compute. It is large here because
   this laptop's PCIe link (~12.9 GB/s, 0.65 ms per 8 MiB layer) is about as
   slow as the network at line rate; on a PCIe Gen5 server GPU it would be
   roughly four times smaller.
3. **The part only per-layer waits deliver** is about
   `min(total transfer, total compute) x (1 - 1/L)`, the shape the data-model
   document predicts: ~19-20 ms at line rate, ~98 ms when the fetch is the
   bottleneck, ~1.5 ms on a near-complete hit, where there is almost no
   prefill left to hide behind.
4. **Layerwise also absorbed compute jitter.** In the transfer-bound case the
   GPU ran ~65% slower during the timed runs than in its warm calibration,
   reproducibly (laptop clocks/thermals). Barrier and streamed copy paid for
   that directly; layerwise hid it behind the network.

**Suggestion for the team** (not a decision): "streamed copy" -- copy each
layer from L1 to the GPU as it lands, but keep today's single wait in vLLM
-- captures a real share of the saving with none of the vLLM-side risk
(per-layer waits in attention, piecewise CUDA graphs, R5's missing fallback).
Per-layer waits then add the compute-overlap part, which matters mainly in
the partial-hit, transfer-bound regimes. That ordering is worth discussing
before C9 wiring.

**Assumptions and limits** -- read these before quoting the numbers:

- Arrivals are perfectly regular. Real fetches have jitter and tails; the
  data-model document notes that a per-layer barrier waits on the slowest
  transfer of *each* layer, so p99 behaviour will be worse than this.
- Compute is calibrated matmuls, not a model forward pass; its size is set
  by ratio, and this GPU is far slower than the H100 the ratios describe.
- One process, two threads: no cross-process CUDA IPC event cost, no vLLM,
  no piecewise-CUDA-graph cost.
- Copies are one contiguous block per layer, not production's per-layer plane
  staging (`kv_size` copies per chunk) plus the paged-KV kernel.
- Percentages depend entirely on the regime; quote the absolute milliseconds
  (as the data-model document asks for AIE-97).
- The tool measures compute *inside* the timed runs and drives the model with
  that value. An earlier version used only the warm calibration; in the
  transfer-bound case that inflated the barrier baseline by ~19% and would
  have flattered layerwise. Found and fixed before recording these numbers.

**Tested:** `tests/benchmarks/test_layerwise_simulate_ttft.py` (new, 9
tests): the pipeline model against hand-computed limits (barrier serialises
everything; compute-bound hides all but layer 0's transfer; transfer-bound
hides all but the last layer's compute; streamed copy hides only the copy;
one layer overlaps nothing; bad inputs rejected; scenario bandwidths), plus a
`cuda`-marked smoke test running a tiny scenario end to end through the real
Track B path on the RTX 3060. All pass; ruff, isort and mypy clean. The Track
B CI workflow now triggers on the benchmark and runs its tests (CPU job) and
the script itself (dormant GPU job).

**Reproduce:**

```bash
cd /mnt/c/Repos/LMCache
.venv/bin/python benchmarks/layerwise/simulate_ttft.py --repeats 9 --json out.json
```
