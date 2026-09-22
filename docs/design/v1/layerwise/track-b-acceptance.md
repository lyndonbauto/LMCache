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
