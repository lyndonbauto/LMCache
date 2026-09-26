# Multiprocess layerwise H2D load

## Problem

In default MP mode the daemon copies an entire retrieve—every object group, every
layer in each kernel group—before vLLM runs the request. The worker blocks once on
a single CUDA IPC event, so no attention layer overlaps the transfer.

Layerwise mode launches one `multi_layer_block_kv_transfer` slice per global layer
(in hybrid-safe order) and lets vLLM call `wait_for_layer_load` inside attention
so layer *L* can compute while layer *L+1* is still in flight on the daemon
stream.

## Launch-order trap (hybrid models)

Kernel groups are defined by transfer identity, not by model depth. A Mamba/GDN
hybrid puts attention layers `[0, 2, 4, …]` in one group and recurrent layers
`[1, 3, 5, …]` in another. Launching object-group-major enqueues every attention
layer before any recurrent layer. vLLM’s second layer is recurrent; waiting for
it would still drain almost the entire transfer.

`LayerwiseSchedule` sorts launches by **global layer index**, interleaving groups
the same way the RDMA fetch schedule does. Example with four layers split
`[0, 2]` / `[1, 3]`: launch order is 0 → 1 → 2 → 3, not 0 → 2 → 1 → 3.

## Generation and watermark (worker wait)

The daemon runs H2D on its own CUDA stream in another process; the worker must
hold its **compute** stream per layer. Shared state is a fixed record
(generation + watermark) plus a pool of IPC events, one per launch ordinal,
created at registration and re-recorded each retrieve.

```
Worker                          Shared memory              Daemon
  | begin retrieve (gen G)          |                          |
  |------------------------------>| begin_retrieve(G)          |
  |                               | watermark=0                |
  |                               |                            | for ordinal o:
  |                               |                            |   enqueue layer kernel
  |                               |                            |   record event[o]
  |                               |<---------------------------| watermark=o+1
  | wait_for_layer(L):            |                            |
  |   poll until gen==G and       |                            |
  |   watermark>=wait_ordinal(L)  |                            |
  |   stream.wait(event[o])       |                            |
```

**Generation** tags one retrieve. Without it, a worker could wait on an IPC event
that still holds a *previous* retrieve’s recording—the same stale-completion
concern as the RDMA layer pipeline’s generation field.

**Watermark** counts how many ordinals the daemon has enqueued and recorded. Launches
share one transfer stream and follow `LayerwiseSchedule`, so the watermark is a
tight bound for “layer *L* has landed.”

**Segment ownership.** The worker creates the segment (name
`lmcache_mp_layer_progress_<instance_id>`) and is the only process that
unlinks it; every other process attaches through
`layer_progress.attach_layer_progress_shm`. A plain
`SharedMemory(name=...)` attach is not safe here: CPython registers attached
segments with the attaching process's resource tracker and unlinks them when
that process exits, so a daemon exit or restart would delete every live
worker's segment. Worker teardown drops the waiter before closing the segment,
treats an already-removed name as success, and a registration that fails
after creating the segment removes it.

Failure paths:

- Retrieve fails partway: daemon sets a failure flag; the worker raises
  `LayerProgressRetrieveFailedError` after a bounded poll instead of hanging.
- Stale generation in shared memory: worker raises `LayerProgressStaleGenerationError`.
- Layer not in the schedule: connector `wait_for_layer_load` is a no-op.

**What vLLM sees.** The four "this layer's KV did not land" errors share the
base class `LayerProgressLoadError`: retrieve failed, generation never
published, watermark stalled, stale generation. The connector's
`wait_for_layer_load` catches that class and calls the worker adapter's
`report_failed_layer_load()`, which adds the blocks of every retrieve submitted
this step to `get_block_ids_with_load_errors()`. The forward pass then
continues; later layer waits return at once because the failed retrieve is no
longer active. vLLM's scheduler truncates each affected request's computed
tokens to the first bad block and discards this step's output for it, then
either recomputes (`kv_load_failure_policy="recompute"`, the default) or fails
the request (`"fail"`).

```
layer 7 wait raises LayerProgressLoadError
  -> connector: report_failed_layer_load()        # this step's retrieves only
  -> layers 8..N: no active retrieve, return
  -> get_block_ids_with_load_errors() -> {blocks}  # same step, as sync loads need
  -> vLLM: drop this step's tokens, recompute from first bad block
```

Why every retrieve of the step, not only the one whose wait failed: each
request submits its own retrieve, and the transfer context waits on the latest
one alone, so the earlier ones are unproven too. Retrieves from earlier steps
are not flagged: their forward pass already passed its waits, and flagging them
would recompute a running request's prefix. A retrieve reported this way is not
reported again when its future later resolves as failed; a second report in a
later step would discard the recompute step as well, or hit blocks that now
belong to another request.

Limits:

- Models with more than one vLLM KV cache group (hybrid models) raise
  `RuntimeError` instead. vLLM rejects block-level load-error reports for them,
  and its alternative (`finished_recving` failures) covers only requests parked
  in `WAITING_FOR_REMOTE_KVS`, which layerwise loads never are.
- Configuration errors (`LayerProgressIncompatibleWithCudaGraphError`,
  `LayerProgressLayerNotScheduledError`) are not load errors and still raise.
- A missing generation costs one full `layerwise_wait_timeout_seconds` before
  the step gives up.

## Arrival-driven launch

`transfer_kv_layerwise_h2d` launches every scheduled layer back to back. When
bytes arrive from a remote store layer by layer, launches must instead wait
for each layer to land. `object_group_transfer.LayerwiseH2DRetrieve` exposes
the same work in three phases so a caller can pace it:

```text
retrieve.begin()               # per-batch setup once; publish generation G
retrieve.launch_layer(L)       # copy layer L; record event[o]; watermark=o+1
retrieve.mark_failed()         # publish failure under G; waiters raise
```

`launch_layer` accepts only the next layer in `LayerwiseSchedule` order, so the
watermark stays a tight bound. `transfer_kv_layerwise_h2d` is now a thin loop
over `launch_layer` and behaves exactly as before.

`mark_failed` never touches a record that already holds a newer generation,
and the worker's waiter honours a failure flag only under its own generation,
so one retrieve's failure cannot fail or rewind the next.

`layerwise_sink.MultiprocessLayerLoadSink` presents this as the
`LayerLoadSink` contract so `LayerArrivalPump` can drive it from a transport's
arrivals. Production builds one sink per retrieve:

```python
sink = MultiprocessLayerLoadSink.for_retrieve(schedule, LayerwiseH2DRetrieve(...))
```

- The pump's fetch generation and the worker's retrieve generation are
  distinct; each load's launcher is already bound to its retrieve generation.
- A load is any strictly ascending subset of the schedule (the plan's
  `layer_ids()`). Scheduled layers the load skips are launched on the way
  past, and trailing ones at `finish_load`, so the watermark always tracks
  schedule position -- never a count of copies, which would report a layer
  ready early.
- `abandon_load` fails only the active load's waiters; a stale or late
  abandon changes nothing.

It passes the shared loader suite, `tests/v1/layerwise/test_load_sink_conformance.py`,
through `tests/v1/layerwise/multiprocess_sink_harness.py`. See
[`../layerwise/track-b-acceptance.md`](../layerwise/track-b-acceptance.md)
(implementation log) for the decisions behind this split.

## Staging vs overlap

Per-layer kernels read from GPU staging buffers. How those buffers are filled is
chosen per retrieve with `LayerStaging`, and the choice decides correctness:

| Mode | Copies | Correct when | Used by |
| --- | --- | --- | --- |
| `WHOLE_OBJECT` | each object in full, on its first layer's launch | every object is complete before the retrieve starts | `transfer_kv_layerwise_h2d` |
| `PER_LAYER` (default) | only the launched layer's bytes, at its launch | always, including while later layers are still arriving | `MultiprocessLayerLoadSink` |

`WHOLE_OBJECT` on arriving data is silently wrong: layer 0's launch copies the
bytes of layers not yet written, and later launches reuse that stale copy.

Per-layer staging works because each kernel group's staging view sits at a
fixed offset inside its object group's staging buffer, and a memory object is
laid out byte-for-byte like that buffer. A layer is `kv_size` disjoint planes
in the `(kv_size, num_layers, slots, hidden)` layout, or one block in the
`(num_layers, slots, hidden)` layout, so it costs `kv_size` range copies per
chunk instead of one copy per object. GDS objects transfer whole only and are
refused in `PER_LAYER` mode.

Overlap is therefore:

- **Yes** between attention on layer *L* and the daemon stream processing layer
  *L+1*, in both modes.
- In `WHOLE_OBJECT` mode, **no** overlap between staging an object and the first
  layer drawn from it -- the full object must be in host memory first.
- In `PER_LAYER` mode a layer can launch as soon as its own bytes have landed.

## Scheduling bargain (vLLM)

With layerwise enabled, `get_num_new_matched_tokens` returns `False` for the
async-load flag even when tokens must be loaded. Otherwise vLLM parks the request in
`WAITING_FOR_REMOTE_KVS` until the **entire** retrieve finishes, which prevents any
per-layer wait from running.

Tradeoff: the forward pass starts while KV is still arriving. If the daemon cannot
keep ahead of vLLM, the stall happens **inside** attention (holding GPU execution
resources) instead of cleanly outside the forward pass. A scheduler step that
mixes one loading request with several already-running ones can stall the whole
batch inside attention, because every request in the step executes the same
forward pass.

Config (must match on worker and server):

- Server: `--use-layerwise` / `MPServerConfig.use_layerwise`
- Worker: `lmcache.mp.use_layerwise` in vLLM `kv_connector_extra_config`
- Worker per-layer wait budget: `lmcache.mp.layerwise_wait_timeout_seconds`
  (default ``5.0``; must be positive)

When the flag is off, layerwise code paths are inert.

## CUDA graphs

Layerwise load calls ``wait_for_layer_load`` from inside attention. That path
polls shared memory and enqueues ``cudaStreamWaitEvent`` on the compute stream
from the host. Full CUDA graph capture cannot record that host-side
synchronization; on graph replay the wait would be elided and attention could
run before the daemon finished the matching layer transfer.

vLLM resolves this at config time: connectors implement
``KVConnectorBase_V1.requires_piecewise_for_cudagraph``. When it returns
``True`` and the deployment asked for full CUDA graphs, vLLM logs a warning and
sets ``cudagraph_mode`` to ``PIECEWISE``. In piecewise mode the attention op is
a graph split point, so the per-layer wait runs in an eager segment and remains
correct.

``LMCacheMPConnector.requires_piecewise_for_cudagraph`` returns True when
``lmcache.mp.use_layerwise`` is set—the same spelling as the rest of this
feature. That mirrors the non-multiprocess ``LMCacheConnectorV1`` hook, which
returns True when ``use_layerwise`` is enabled in extra config.

This hook only fires for the connector class vLLM actually loads, and that is
not always this one. vLLM vendors a fallback copy of the multiprocess connector
(``LMCacheMPConnectorUpstream``) and, at import time, prefers the external class
from ``lmcache.integration.vllm.lmcache_mp_connector`` whenever ``lmcache`` is
importable. So an LMCache deployment gets this implementation, but a deployment
that sets ``LMCACHE_USE_UPSTREAM_MP`` or runs an LMCache too old to ship the
submodule falls back to vLLM's copy, which has no layerwise support and does not
override the hook. There ``lmcache.mp.use_layerwise`` is silently ignored: no
layerwise load, and no piecewise downgrade either. That is safe—the feature is
simply off—but it means the flag alone is not evidence that layerwise is running.

Trade-off: piecewise graphs retain most decode-graph wins but not the last
slice of performance a single full graph would give. Layerwise overlap (attention
on layer *L* while layer *L+1* transfers) is the intended win; forcing full
graphs would silently break correctness.

``LayerProgressWaiter`` still raises if the compute stream is capturing when a
wait runs; that is an invariant backstop, not operator configuration advice.

## Unverified on this machine

This development environment has **no GPU** and a CPU-only PyTorch build. The
following were **not** compiled or executed here:

- CUDA changes to `multi_layer_block_kv_transfer` (`layer_offset`, `n_layers`)
- End-to-end layerwise retrieve with real IPC events and overlap measurements
- Hybrid schedule invariant: at registration the daemon asserts that
  ``LayerwiseSchedule`` built from worker ``EngineGroupInfo`` matches the schedule
  from registered ``kernel_groups`` (ordinal ranks in globally sorted layer order).
  ``KVLayerGroupsManager`` already rejects kernel/engine group layer mismatches at
  context creation.

CI with CUDA remains the authority for kernel and integration correctness.
