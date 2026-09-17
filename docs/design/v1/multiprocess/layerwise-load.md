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

Failure paths:

- Retrieve fails partway: daemon sets a failure flag; the worker raises
  `LayerProgressRetrieveFailedError` after a bounded poll instead of hanging.
- Stale generation in shared memory: worker raises `LayerProgressStaleGenerationError`.
- Layer not in the schedule: connector `wait_for_layer_load` is a no-op.

## Staging vs overlap

Per-layer kernels still read from GPU staging buffers filled by **whole-object**
H2D copies (same as the default path). Staging is not split per layer. Overlap is
therefore:

- **Yes** between attention on layer *L* and the daemon stream processing layer
  *L+1* (kernel launch, and staging for batches not yet copied).
- **No** between staging an object and the first layer drawn from that object’s
  staging buffer within the same batch—the full object must land before any of its
  layer slices can launch.

## Scheduling bargain (vLLM)

With layerwise enabled, `get_num_new_matched_tokens` returns `False` for the
async-load flag even when tokens must be loaded. Otherwise vLLM parks the request in
`WAITING_FOR_REMOTE_KVS` until the **entire** retrieve finishes, which prevents any
per-layer wait from running.

Tradeoff: the forward pass starts while KV is still arriving. If the daemon cannot
keep ahead of vLLM, the stall happens **inside** attention (holding GPU execution
resources) instead of cleanly outside the forward pass.

Config (must match on worker and server):

- Server: `--use-layerwise` / `MPServerConfig.use_layerwise`
- Worker: `lmcache.mp.use_layerwise` in vLLM `kv_connector_extra_config`

When the flag is off, layerwise code paths are inert.

## Unverified on this machine

This development environment has **no GPU** and a CPU-only PyTorch build. The
following were **not** compiled or executed here:

- CUDA changes to `multi_layer_block_kv_transfer` (`layer_offset`, `n_layers`)
- End-to-end layerwise retrieve with real IPC events and overlap measurements
- Hybrid models where worker `LayerwiseSchedule` built from `EngineGroupInfo` must
  exactly match the daemon schedule from `kernel_groups` (mis-match would cause
  wrong waits without crashing)

CI with CUDA remains the authority for kernel and integration correctness.
