# General LMCache features (not Aerospike-specific)

New features on this fork that any LMCache deployment could use, whatever its
L2 backend.

Companion files:

- [bug_fixes.md](bug_fixes.md): fixes to code upstream already has. It also
  gives the baseline commits and the diff recipe.
- [aerospike_rdma.md](aerospike_rdma.md): Aerospike and RDMA work.

Baseline: fork `prototype-stage1` (`49f18d12`) vs upstream `dev`
(`8d7d2c47`), merge base `caba24c2`.

**Update for the `prototype-stage-1a` PR (2026-10-01).** The PR changes
almost nothing here.

- **GF-1 to GF-11.** The PR doesn't touch them.
- **GF-12.** The PR only rewords `contract.py` docstrings: "RDMA immediate"
  becomes "fetch table" and "batch-read row". That removes part of GF-12's
  "RDMA vocabulary" pushback.
- **Changes since `49f18d12`** that come from `prototype-stage1`'s own
  commits, not from the PR:
  - **D-17**, an S1 wrong-output defect after a failed layerwise load under
    recompute. It was later traced to a vLLM bug,
    [vllm#49250](https://github.com/vllm-project/vllm/issues/49250).
    LMCache still needs a startup guard (see GF-5).
  - `read_write_ids` added to `contract.py` (D-14, `e9cd0689`).

**PR isolation check (2026-10-01).** Each item was re-checked against
upstream `dev`. The question was whether it would build, pass its own tests
and do something with only its stated prerequisites merged. The result:

- **GF-7 and GF-1 stand alone.**
- **GF-2 to GF-5 can't be split as originally listed.** The daemon wiring,
  worker side and protocol were written together; commit `614b04fa` already
  touches the protocol, worker and tests.
- **The new cut is five PRs, PR-G1 to PR-G5.** Two of them (PR-G3 and
  PR-G4) are library PRs with unit tests.
- **GF-6 is dissolved.**
- **Part 2 becomes PR-G6 to PR-G8.** GF-8 and GF-9 move into the L1-windows
  PR in [aerospike_rdma.md](aerospike_rdma.md).

The [PR plan](#pr-plan) at the end has the details. Every item section
starts with a **PR** line. No commit cherry-picks cleanly: cut each PR from
the final file contents.

The file has two parts:

- **Part 1** is useful on its own today.
- **Part 2** is generic in shape, but its only caller today is the Aerospike
  RDMA pipelined fetch. Upstream reviewers will ask "who uses this?", so these
  usually ship just before, or together with, the RDMA PRs.

---

## Summary

| ID | Feature | Size (+, incl. tests) | Really depends on | Ships in |
| --- | --- | --- | --- | --- |
| GF-1 | MP KV kernel: transfer a layer sub-range | ~+220 | none | PR-G2 (alone) |
| GF-2 | Layerwise schedule and cross-process layer progress | ~+1,650 | none (not GF-1) | PR-G3 (library) |
| GF-3 | Daemon-side per-layer H2D retrieve | ~+2,650 | GF-1, GF-2; its module half needs GF-4 | **Split:** library half in PR-G4; module half in PR-G5 |
| GF-4 | Protocol and server config for layerwise | ~+180 (+ test fixes) | GF-3 module half, GF-5 | **Combined** into PR-G5 |
| GF-5 | Worker and vLLM connector per-layer wait | ~+590 | GF-3 module half, GF-4 | **Combined** into PR-G5, plus a new D-17 guard |
| GF-6 | Layerwise design doc, TTFT simulator, CI workflow | ~+1,490 | the simulator needs GF-12 and AS-M3 | **Dissolved:** doc split into PR-G3 and PR-G5; simulator deferred; CI workflow dropped |
| GF-7 | Cache simulator `hash-trace` tool | ~+735 | none | PR-G1 (alone) |
| GF-8 | Transport-agnostic memory registration on `L1MemoryDesc` | small | its only consumer is AS-P1 | **Moved** into PR-A9 ([aerospike_rdma.md](aerospike_rdma.md)) |
| GF-9 | L1 helpers: `abort_write`, `delete_if_none_locked`, `RangeMemoryAllocator`, reserved prefix | ~+600 | its only consumers are AS-P2 and AS-P3 | **Moved** into PR-A9 |
| GF-10 | Prefetch controller: deferred L2 mode | ~+300 | none, but must be rebuilt on prefetch v2 | PR-G8, with GF-11 |
| GF-11 | `StorageManager.load_into_l1` and `lock_resident_keys` | ~+200 | GF-10 | PR-G8, with GF-10 |
| GF-12 | Layerwise arrival/sink contract, pump and fakes | ~+1,500 | none, once `__init__` is trimmed | PR-G6 (library); the MP sink follows in PR-G7 |

GF-1 to GF-6 together are **MP layerwise load**: one user-visible feature.
It is cut into four building blocks (PR-G2 to PR-G4) and one feature PR
(PR-G5). Only PR-G5 is reachable by a user.

---

# Part 1 — Usable on its own

## MP layerwise load (GF-1 to GF-6) — overview

**What.** In multiprocess (MP) mode, the LMCache server copies a retrieved
KV cache into vLLM's paged buffers one layer at a time. vLLM's forward pass
for layer *i* waits only for layer *i*, not for the whole retrieve. Before
this, MP mode loaded everything before the first layer ran, a "barrier".
Non-MP LMCache has had layerwise load for a long time; MP mode had none.

**Why.** It hides H2D copy time behind compute. It is also the consumer side
that any per-layer *arrival* source needs, such as Aerospike RDMA (see
[aerospike_rdma.md](aerospike_rdma.md)). Track B's simulation (RTX 3060,
32 layers, 2048-token prefill) saved 40 ms at line rate and 119 ms when
transfer-bound (`docs/design/v1/layerwise/track-b-acceptance.md`, B8). **No
real vLLM TTFT gain has been measured yet.**

**Correctness evidence** (MI300X, `functional/day1/SUMMARY.md`):

- Llama-3.1-8B under `VLLM_BATCH_INVARIANT=1`: 130/130 cold and 130/130 warm
  token-exact, with layerwise off and on.
- gpt-oss-120b (hybrid): P-exact 20/20 equal; P-shared 10/10 equal to vLLM's
  own prefix cache.
- Stage 2 (`functional/stage2/SUMMARY.md`) ran every test layerwise off and
  on.

**How it flows.**

```
worker (vLLM)                          LMCache MP server (daemon)
-------------                          --------------------------
REGISTER_KV_CACHE(..., layer_event_ipc_handles)
      ----------------------------->   creates LayerProgressRecord (shm)
      <-----------------------------   RegisterKvCacheResponse(server_use_layerwise,
                                          layer_event_ipc_handles,
                                          layer_publish_budget_seconds)
RETRIEVE(..., retrieve_generation)
      ----------------------------->   LayerwiseH2DRetrieve: for each layer in
                                       LayerwiseSchedule order:
                                          H2D copy of that layer
                                          record layer event, bump watermark
forward pass:
  wait_for_layer_load(layer i)  <-- waits on layer i's IPC event / watermark
  attention(layer i)
```

**Competing upstream work.** Upstream PR #4460 (zxue2, "Add batch layerwise
for MP LMCache driven mode") targets the same gap, differently:

- a `--layerwise-batch N` option;
- an interleaved `[L, 2, T, D]` layout;
- per-layer IPC events;
- a `REGISTER_KV_CACHE` response changed from `None` to `int`.

**Decide with the maintainers which design goes forward before opening
GF-2 to GF-5.** Otherwise both PRs stall. Our selling points:

- no layout change;
- hybrid and multi-kernel-group models, through a global layer order;
- generation-checked progress, so a stale retrieve can't satisfy a new wait;
- recoverable failures, where the engine recomputes instead of dying.
  **Caveat (D-17, S1):** on current vLLM, a failed synchronous load under
  `recompute` recovers without crashing, but its output is wrong. The bug is
  vLLM's ([vllm#49250](https://github.com/vllm-project/vllm/issues/49250));
  see GF-5. Make this claim only together with PR-G5's startup guard, and
  name the vLLM fix it needs.

---

## GF-1. MP KV transfer kernel: transfer a layer sub-range

> **PR: PR-G2, standalone after a mechanical re-port.** Upstream still has
> `layer_idx = blockIdx.z` and the same signatures.
>
> - **Moved files.** `musa` and `rbln` `device_ops.py` now live under
>   `platform/devices/...`.
> - **Exclude** the BF-3 hunks (`_GpuMemcpy`, `runtime_libs`) from the fork's
>   `torch_ops.py` diff.
> - **"Try `cuda_ops` first" hunk.** It now goes in
>   `torch_ops/mp_mem_kernels.py`. It is probably unnecessary on CUDA, where
>   `CudaDeviceOps` binds `cuda_ops` directly (unverified for ROCm).
> - **New upstream layout.** Upstream added `NL_X_NB_BS_NH_HS` (SGLang) to
>   `test_mp_mem_kernels.py`. Cover it in the sub-range test and guard.
> - **Expected question.** #5308's direct-copy path has no layer range.
> - **Tests:** `test_mp_mem_kernels.py` (+164); needs a CUDA GPU.
> - **No caller until PR-G4.** The kernel API is complete and tested on its
>   own.

**What.** `multi_layer_block_kv_transfer` gains `layer_offset=0` and
`n_layers=-1` (meaning all layers), so one launch can move one layer, or a
contiguous range, of a kernel group. The defaults keep every existing call
unchanged.

**Where.**

- **Kernel.** `csrc/cuda/mp_mem_kernels.cu` and `csrc/cuda/mp_mem_kernels.cuh`
  (parameters documented in the header).
- **Binding.** `csrc/cuda/pybind.cpp`: `py::arg("layer_offset") = 0,
  py::arg("n_layers") = -1`.
- **Device ops.** `lmcache/v1/platform/base/device_ops.py` passes the range
  through. `lmcache/v1/platform/musa/device_ops.py` and
  `lmcache/v1/platform/rbln/device_ops.py` raise `NotImplementedError` for a
  sub-range.
- **Python fallback.** `lmcache/v1/platform/torch_ops.py`:
  `multi_layer_block_kv_transfer` now tries `lmcache.cuda_ops` first, and
  raises `NotImplementedError` for a sub-range on the pure-Python path.

**Commits.** `34f0c898` (kernel range), `3a839927` (route through
`DeviceOps`), `f5ff2bbb` (guard `layer_offset` on stride-sensitive layouts in
tests), and parts of `061fb52a`.

**Tests.** `tests/v1/test_mp_mem_kernels.py` (+164).

**Upstream overlap.**

- **Merge conflict.** `tests/v1/test_mp_mem_kernels.py` conflicts in the
  dry-run merge.
- **Moved file.** `torch_ops.py` is now a package upstream
  (`lmcache/v1/platform/torch_ops/mem_kernels.py`), so the fallback change
  must be re-ported.
- **New upstream path.** Upstream #5308 moved the LMCache-driven transfer to
  `cudaMemcpyBatchAsync`. Check that the layer range still applies on that
  path.

**Likely pushback.**

- **Behavior change.** The Python fallback now prefers the CUDA extension
  when it is present. Make that explicit in the PR, or split it out.
- **No consumer in this PR.** A PR with GF-1 alone is small, but reviewers may
  want to see GF-3 first. Link the design doc.

---

## GF-2. Layerwise schedule and cross-process layer progress

> **PR: PR-G3, a library PR with unit tests. Its consumer is PR-G4.**
>
> - **No GF-1 dependency.** `layerwise_schedule.py` and `layer_progress.py`
>   import only `kv_layer_groups`, `lmcache.torch_dev` and `base/event_ipc`.
> - **Ship the core of `layerwise-load.md`** with it (see GF-6).
> - **Tests:**
>   - `test_layerwise_schedule.py` and `test_layer_progress.py` run on CPU
>     (the native extension builds under `NO_GPU_EXT=1`);
>   - `test_layerwise_gpu_overlap.py` needs a GPU; on ROCm it also needs
>     PR-B4, because it re-records exported events.
> - **Move `test_layer_progress_lifetime.py` to PR-G5.** It imports
>   `RegisterKvCacheResponse` and `worker_transfer`.

**What.**

- **`LayerwiseSchedule` / `LayerLaunch`** (`layerwise_schedule.py`): one
  global layer order across every kernel group of a model. Hybrid models,
  where kernel groups own disjoint layer sets, still complete in forward-pass
  order. `assert_registration_schedules_agree` checks that the worker and the
  daemon derived the same schedule.
- **`LayerProgressRecord`** (`layer_progress.py`): a shared-memory record per
  instance holding a retrieve generation and a per-layer watermark. The
  daemon publishes it; the worker reads it.
  - `fail_retrieve(generation)` marks a retrieve failed without overwriting a
    newer one.
  - Errors are distinct types: `LayerProgressRetrieveFailedError`,
    `...GenerationTimeoutError`, `...ProgressTimeoutError`,
    `...StaleGenerationError`, `...LayerNotScheduledError` and
    `...IncompatibleWithCudaGraphError`.
- **IPC event pools.** `LayerLaunchEventPool`,
  `WorkerComputeLayerLaunchEventPool` and `DaemonLayerLaunchEventPool` give
  each layer a GPU event the worker's stream can wait on without a host
  round trip.
- **`LayerProgressWaiter`** is the worker-side wait loop.
- **Shared memory helpers:** `layer_progress_shm_name` and
  `attach_layer_progress_shm`.

**Where.** `lmcache/v1/multiprocess/layerwise_schedule.py` (new) and
`lmcache/v1/multiprocess/layer_progress.py` (new).

**Commits.**

- `d2cfcb48`: order by layer, not by kernel group.
- `558b3e6e`: cross-process progress.
- `b795e69f`: rework so retrieves can complete.
- `a84f19b9`: reject ordering bugs in tests.
- `9e1b9494`: the CUDA graph check.
- `5e3d5443`: `fail_retrieve`.
- `061fb52a`: tests and the segment lifetime.

**Tests.**

- `tests/v1/multiprocess/test_layerwise_schedule.py`
- `tests/v1/multiprocess/test_layer_progress.py`
- `tests/v1/multiprocess/test_layer_progress_lifetime.py`

**Upstream overlap.** New files, so no merge conflicts. They use the platform
event-IPC API (`lmcache/v1/platform/base/event_ipc.py`). On ROCm they need
BF-4 ([bug_fixes.md](bug_fixes.md)), because they re-record exported events.

**Likely pushback.**

- **Another shared-memory segment per instance.** Reviewers will ask about
  its lifecycle and cleanup after a crash. Step 7 of `track-b-acceptance.md`
  covers the resource-tracker interaction.
- **Six error types** may look like a lot. Each maps to a distinct worker
  action.

---

## GF-3. Daemon-side per-layer H2D retrieve

> **PR: split in two.**
>
> **Library half → PR-G4,** a library PR after PR-G2 and PR-G3.
>
> - **Contents:** `object_group_transfer.py` (`LayerwiseH2DRetrieve` and
>   per-layer staging, +833; it imports only GF-2 and `gpu_ops`) and
>   `lmcache_memcpy_async_h2d_range` in `gpu_ops.py` (+77).
> - **Exclude `per_layer_staging_ranges`.** Only AS-M3 calls it, so it moves
>   to PR-A11.
> - **Reword** the `MemoryObjectLookup` docstring that points at
>   `pipelined_loading.ObjectTable`.
> - **Ship a trimmed copy of `test_object_group_layerwise_transfer.py`.** The
>   fork's file imports `lmcache.v1.layerwise`, `layerwise_sink`,
>   `pipelined_loading` and `pipelined_sink`. These parts move to PR-G7 and
>   PR-A11:
>   - `_plan_for`;
>   - the real-sink and unservable-source tests (about lines 693-760);
>   - `_check_objects_read_at_each_launch` and its tests;
>   - `_DeclinesLayerTwo` and the factory-sink tests.
> - **Keep** the `e34fa420` regression case
>   (`test_every_batch_is_staged_from_its_own_chunks`). The trimmed file runs
>   on CPU with the kernel mocked, plus three CUDA cases.
>
> **Module half → PR-G5.** The `lmcache_driven_transfer.py` hunks return
> `RegisterKvCacheResponse` and take `retrieve_generation`, so they can't
> merge before GF-4.
>
> **Module test: written on `lmcache-pr`.**
> `tests/v1/multiprocess/test_lmcache_driven_layerwise_retrieve.py` drives
> `LMCacheDrivenTransferModule.retrieve` with `use_layerwise=True` on the
> plain path (no pipelined sink, nothing deferred). It runs on CPU with mocks
> and checks four things:
>
> - one `transfer_kv_layerwise_h2d` call carries every object and the
>   worker's generation;
> - a key missing from L1 publishes `fail_retrieve(generation)`;
> - a transfer that raises does the same;
> - generation 0 is refused.
>
> It patches only names that PR-G5's module also has. The other module test
> with layerwise on, `test_lmcache_driven_deferred_retrieve.py`, covers the
> pipelined path and ships with PR-A11.

**What.** The LMCache-driven retrieve can stage and copy KV one layer at a
time, publishing each layer's progress (GF-2) as it lands.

- **`LayerwiseH2DRetrieve`** (in `object_group_transfer.py`) is a retrieve
  state machine.
  - It walks `LayerwiseSchedule`.
  - Per batch, it tracks which batch currently holds the shared staging
    slots, and restages when needed.
  - It computes per-batch launch descriptors once per object group
    (`_LayerwiseBatchDescriptor`, `_build_layerwise_batch_descriptors`), not
    once per layer.
- **`transfer_kv_layerwise_h2d`**: the entry point.
- **`LayerStaging`** (`WHOLE_OBJECT` / `PER_LAYER`): stage the whole object
  once, or stage each layer's byte ranges as needed.
  - `transfer_kv_layerwise_h2d` uses `PER_LAYER` for every retrieve unless it
    holds a GDS object (`65738f1c`). It used to pick `WHOLE_OBJECT` whenever
    each object group fit in one batch (four chunks or fewer), so the first
    layer's wait covered every layer's copy. GDS objects only transfer
    whole, so they keep `WHOLE_OBJECT`.
  - `per_layer_staging_ranges` and `_layer_plane_geometry` compute which bytes
    of an object are layer *i*.
  - `FixedMemoryObjects` serves a fixed list of objects.
- **`gpu_ops.lmcache_memcpy_async_h2d_range(memory_obj, gpu_buffer,
  byte_offset, nbytes)`**: copies one byte range of a memory object.
  - For CUDA it is a single raw `lmcache_memcpy_async` with no GIL. That costs
    about half the CPU time of slicing two tensors (317 to 175 us to issue a
    16-copy layer).
  - Lazy-allocator objects split at pin-chunk boundaries.
  - GDS objects are refused.
- **`lmcache_driven_transfer.retrieve`** takes the layerwise path when the
  server context has `use_layerwise`.
  `_publish_layerwise_retrieve_terminal` marks completion or failure.
- **`transfer_context/worker_transfer.py`**: the worker-side
  `wait_for_layer_load`, `_release_layerwise_state` and
  `_check_layerwise_wait_covers_budget` (refuses a per-layer wait shorter
  than the daemon's publish budget plus 0.5 s).

**Where.**

- `lmcache/v1/multiprocess/object_group_transfer.py` (+833)
- `lmcache/v1/gpu_connector/gpu_ops.py`
- `lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py` (the
  layerwise half; the pipelined half is AS-M3)
- `lmcache/v1/multiprocess/transfer_context/worker_transfer.py` (+247)

**Commits.**

- `614b04fa`: launch the MP retrieve one layer at a time.
- `cae968bb`: hoist batch setup out of the layer loop.
- `7f75a0ba`: per-layer staging.
- `e34fa420`: the multi-batch restage fix.
- `5e3d5443`: the faster copy.
- `1138782d`: report only failures, after the copies land.
- `65738f1c`: per-layer staging for every non-GDS retrieve.

**Tests.**

- `tests/v1/multiprocess/test_object_group_layerwise_transfer.py` (includes
  the three-chunk, two-per-batch staging case that failed before `e34fa420`,
  and `test_a_one_batch_retrieve_stages_each_layer_at_its_own_launch`, which
  failed before `65738f1c`).
- `tests/v1/multiprocess/test_layerwise_gpu_overlap.py`.

**Upstream overlap.**

- **Dry-run merge conflicts:** `lmcache_driven_transfer.py` and
  `worker_transfer.py`.
- **#5308** rewrote the LMCache-driven copy around `cudaMemcpyBatchAsync`.
  Expect a real redesign of the hunks in `lmcache_driven_transfer.py`, not a
  mechanical rebase.
- **#5169** binds the CUDA context for IPC, which matters for the event
  import.

**Likely pushback.**

- **`object_group_transfer.py` grows by more than 800 lines.** Consider moving
  the layerwise half to its own module, for example
  `layerwise_object_group_transfer.py`.
- **CPU cost per layer in the daemon.** Have the measurements ready.
- **Hard-coded alignment.** `_SINGLE_COPY_ALIGNMENT = 1 << 62` in `gpu_ops`
  stops the native copy splitting. It needs a clear comment, which it has.

---

## GF-4. Protocol and server config for layerwise (wire change)

> **PR: combined into PR-G5,** with GF-3's module half and GF-5. Neither
> side of the protocol works without the other.
>
> **It must be re-ported.** Upstream #5161 deleted `protocols/` and moved
> `mq.py`. RPC contracts now come from typed `@rpc_method` signatures on
> `RequestClient` (`transport/base.py`). GF-4 becomes new parameters on
> `register_kv_cache` and `retrieve`, plus a new response type.
>
> **Trim it:**
>
> - **Drop the request field `layer_event_ipc_handles`.** It is "reserved;
>   must be empty": the worker always sends `[]`.
> - **Defer `layer_publish_budget_seconds` to PR-A11.** It is always 0 until
>   the pipelined path exists. The struct isn't array-encoded, so adding the
>   field later is compatible.
> - **Exclude these hunks:**
>   - the PING hunk (BF-1);
>   - the `--pipelined-*` flags in `config.py`;
>   - in `engine_context.py`, the `layerwise.deferral` / `request_fetch`
>     imports and the `pipelined_*` members;
>   - in `server.py`, everything except `use_layerwise=`.
>
> **Fix the test list:**
>
> - The `test_config.py` hunk tests only `--pipelined-*`. Write a new
>   `--use-layerwise` test.
> - The `test_qstore.py` hunk is AS-M3.
> - `test_layerwise_wait_budget.py` is AS-M5, so it goes to PR-A11.
> - **Include the `test_cache_server` / `test_mq` regression fixes**
>   ([bug_fixes.md](bug_fixes.md)).
>
> **Not in `dev`:** PR #4460. The upstream `wait_for_layer_load` is still a
> bare `return`.

**What.**

- **`REGISTER_KV_CACHE` request** gains a `list[bytes]` payload: the
  worker's per-layer event IPC handles.
- **`REGISTER_KV_CACHE` response** changes from `None` to
  `RegisterKvCacheResponse(server_use_layerwise, layer_event_ipc_handles,
  layer_publish_budget_seconds)`.
  - The worker learns whether the server will publish per layer.
  - It also learns how long the daemon may take per layer.
- **`RETRIEVE`** gains an `int` `retrieve_generation`.
- **Transports.**
  - The zmq client defaults the new arguments.
  - The gRPC proto and codec follow (`9327d98c`).
  - The payload-count error in `mq.py` is clearer.
- **Server config.**
  - `MPServerConfig.use_layerwise` / `--use-layerwise` in `config.py`.
  - `MPCacheServerContext.use_layerwise` in `engine_context.py`.
  - Wiring in `server.py`.

**Where.**

- `lmcache/v1/multiprocess/custom_types.py`: `RegisterKvCacheResponse`.
- `lmcache/v1/multiprocess/protocols/engine.py` and `protocols/controller.py`.
- `lmcache/v1/multiprocess/transport/base.py`.
- `lmcache/v1/multiprocess/transport/zmq_impl/client.py`.
- `lmcache/v1/multiprocess/transport/grpc_impl/protos/lmcache_driven_service.proto`
  and `grpc_impl/proto_codec.py`.
- `lmcache/v1/multiprocess/mq.py`.
- `lmcache/v1/multiprocess/config.py`, `engine_context.py` and `server.py`.

**Commits.** `a58c9260` (config), `1a6e4ee8` (wait timeout; require
`use_layerwise` on the server context), `9327d98c` (gRPC), `b88ec0ff`
(`layer_publish_budget_seconds`).

**Tests.**

- `tests/v1/multiprocess/test_grpc_transport.py`
- `test_mq.py`, `test_mq_handler_helpers.py`, `test_config.py`
- the ripple updates in `test_lmcache_driven_transfer_skip.py`,
  `test_lmcache_driven_missing_registration.py`, `test_ipc_memory_reclaim.py`,
  `test_event_ipc_handle_path.py` and `test_qstore.py`

**Must fix first.** `tests/v1/multiprocess/test_cache_server.py` and
`test_mq.py` still assert that registration returns `None`. They fail because
of this change, not before it. See "Regression to fix" in
[bug_fixes.md](bug_fixes.md).

**Upstream overlap.** Dry-run merge conflicts in `protocols/controller.py`,
`protocols/engine.py`, `transport/base.py`, `zmq_impl/client.py`, `mq.py`,
`engine_context.py` and `test_mq.py`. PR #4460 makes a competing change to
the same response.

**Likely pushback.**

- **Version skew.** The server and workers must be upgraded together, and the
  PR needs to say what an old worker sees from a new server and vice versa.
- **A response type that grows by feature.** Reviewers may prefer an
  extensible struct, or a separate capability query, over a response that
  gains a field for each feature.

---

## GF-5. Worker and vLLM connector: per-layer wait

> **PR: combined into PR-G5.**
>
> - **The connector diff is all GF-5.**
> - **In the adapter, exclude:**
>   - `ping.py`, `probe_server` and the `HeartbeatThread` changes (BF-1);
>   - the `send_ping` → `probe_server` test hunks (BF-1).
> - **In the adapter, reword** the `ExtraConfigDefault` comment that quotes
>   pipelined budgets.
> - **In `worker_transfer.py`, exclude** `_check_layerwise_wait_covers_budget`
>   (AS-M5 → PR-A11).
> - **New code for PR-G5, now written on `lmcache-pr`** (see D-17 below):
>   - the startup guard;
>   - the `wait_for_layer_load` docstring fix.
> - **Tests:**
>   - `test_mp_connector_layerwise_scheduler.py` needs vLLM installed;
>   - the layerwise cases in `test_vllm_mp_adapter.py` run on CPU;
>   - `test_layer_progress_lifetime.py` needs POSIX;
>   - `test_grpc_transport.py` runs on CPU;
>   - `test_layerwise_recompute_guard.py` (new, the D-17 guard) runs on CPU
>     without vLLM;
>   - `test_lmcache_driven_layerwise_retrieve.py` (new, the module test) runs
>     on CPU.

**What.**

- **vLLM worker adapter** (`vllm_multi_process_adapter.py`).
  - Extra config: `ExtraConfigDefault.use_layerwise` (key
    `lmcache.mp.use_layerwise`) and `layerwise_wait_timeout_seconds` (default
    5.0).
  - `is_layerwise_enabled(extra_config)` parses strings. Before `16d471e8`,
    `"false"` meant True in one place and False in another.
  - `wait_for_layer_load(layer_id)` and `report_failed_layer_load()`.
  - Per-step retrieve tracking (`_current_step_retrieves`,
    `_retrieves_reported_failed`, `_report_failed_retrieve`). A failed layer
    load becomes "recompute these blocks", not an engine crash.
- **vLLM connector** (`lmcache_mp_connector.py`).
  - `wait_for_layer_load(layer_name)` maps names through
    `kv_cache_groups.layer_names_to_global_index`.
  - When layerwise is on, `get_num_new_matched_tokens` returns
    `load_async=False`, and `get_finished` no longer reports recving ids.
  - `requires_piecewise_for_cudagraph(extra_config)` makes vLLM drop from
    full to piecewise CUDA graphs, because per-layer waits can't be captured
    in a full graph.
  - Failed loads reach vLLM through `get_block_ids_with_load_errors`.

**Where.**

- `lmcache/integration/vllm/vllm_multi_process_adapter.py`
- `lmcache/integration/vllm/lmcache_mp_connector.py`
- `lmcache/integration/vllm/kv_cache_groups.py`

**Commits.**

- `83bc7f1c`: wire per-layer waits.
- `9e1b9494`: piecewise CUDA graphs.
- `1a6e4ee8`: the wait timeout.
- `48288b94` and `1138782d`: recompute failed layer loads.
- `e34fa420`: `get_finished`.
- `16d471e8`: parse once.
- `b88ec0ff`: the budget check.

**Tests.**

- `tests/v1/test_mp_connector_layerwise_scheduler.py` (new). It also works
  around vLLM 0.30's read-only `KVConnectorBase_V1.role`.
- `tests/v1/test_vllm_mp_adapter.py`
- `tests/v1/multiprocess/test_layerwise_wait_budget.py`

**Upstream overlap.** Dry-run merge conflicts in `lmcache_mp_connector.py`
and `vllm_multi_process_adapter.py`. Both are hot files upstream, also
touched by BF-1.

**Likely pushback.**

- **Piecewise graphs cost decode throughput.** The trade-off is written up in
  `docs/design/v1/multiprocess/layerwise-load.md`.
- **Synchronous loading.** `load_async=False` puts the retrieve on the
  forward-pass critical path.
- **A 5 s default wait** may stall a step on a slow daemon.

**D-17 (S1): the cause is in vLLM, but LMCache needs a guard.** It was
found in Stage 3, after `49f18d12`, and narrowed on 2026-10-01.

- **Trigger.** A layerwise retrieve fails mid-forward under
  `kv_load_failure_policy=recompute`: a layer deadline passes, or the
  whole-object fallback can't load a missing record.
- **Symptom.** vLLM logs `Recovered from KV load failure: 1 request(s)
  rescheduled`, yet the tokens are wrong.
- **Cause: [vllm#49250](https://github.com/vllm-project/vllm/issues/49250).**
  Its fixes, #49252 and #53298, are unmerged.
  - The V2 runner never rewinds `num_computed_tokens` on the GPU side.
  - Under async scheduling, `num_output_placeholders` isn't rolled back.

  Late LMCache copies are ruled out. The same LMCache build is exact on the
  V1 runner with async off, and on a patched V2.
- **Why LMCache is exposed.** Layerwise loads are synchronous
  (`load_async=False`), and *any* synchronous load rejection triggers the
  bug. That includes PR-G5's plain H2D path, not just the pipelined one.
- **Required in PR-G5 (written on `lmcache-pr`, uncommitted):**
  - **The startup guard.** In `vllm_multi_process_adapter.py`,
    `vllm_rewinds_rejected_kv_loads()` detects the fix: #53298 adds a
    dataclass field, `CachedRequestData.rewound_req_ids`. Next to it,
    `layerwise_recompute_is_unsafe(extra_config, policy)` is True only for
    layerwise plus `"recompute"` on an unfixed vLLM.
    - The connector's scheduler role calls it in `__init__` and logs an
      error. It does **not** refuse to start, because a vLLM patched with
      #49252 only has no marker to detect.
    - A vLLM without the policy field counts as `"recompute"`.
    - Test: `tests/v1/test_layerwise_recompute_guard.py`.
  - **The `wait_for_layer_load` docstring fix.** It now says vLLM
    reschedules the request, and that the recompute is correct only with
    the vllm#49250 fix. The failure warning no longer says "so vLLM
    recomputes them".
- **Evidence:**
  - `functional/stage3/D17-NARROWING.md`;
  - the ledger row in `functional/LEDGER.md`.

---

## GF-6. Layerwise design doc, TTFT simulator, CI workflow

> **PR: dissolved. It isn't its own PR.**
>
> **Design doc** (`layerwise-load.md`, 269 lines):
>
> - the core goes with PR-G3;
> - the vLLM scheduling and CUDA-graph sections go with PR-G5;
> - **drop** the "Arrival-driven launch" section (it describes
>   `MultiprocessLayerLoadSink` and `LayerArrivalPump`, so move it to PR-G7)
>   and the links to the fork-only `../layerwise/` docs.
>
> **TTFT simulator:**
>
> - it imports `lmcache.v1.layerwise` and `layerwise_sink`, so it **can't
>   ship before PR-G6 and PR-G7**;
> - either rewrite it on `transfer_kv_layerwise_h2d` and ship it with PR-G5,
>   or defer it.
>
> **CI workflow `layerwise_track_b.yml`: drop it.**
>
> - It runs `tests/v1/layerwise` (GF-12 and AS-L code).
> - Its GPU job is `if: false`.
> - Upstream's Buildkite unit-test step already runs `tests/` on GPU.
>   Whether vLLM is installed there is unverified.

**What.**

- **Design doc.** `docs/design/v1/multiprocess/layerwise-load.md`: design,
  gaps, and the vLLM scheduling and CUDA graph trade-off.
- **Benchmark.** `benchmarks/layerwise/simulate_ttft.py`: a barrier vs
  layerwise TTFT simulator with configurable per-layer remote, H2D and
  compute times. Tested by `tests/benchmarks/test_layerwise_simulate_ttft.py`.
- **CI.** `.github/workflows/layerwise_track_b.yml`. It is named after the
  internal "Track B"; rename it before upstreaming, or fold it into the
  existing test workflow.

**Commits.** `6c10f090`, `a58c9260`, `9e1b9494` and `061fb52a` (docs); the
benchmark arrived with the Track B work (B8).

**Notes.** The simulator is optional; reviewers may see it as a prototype
tool. See the PR line above for where the doc's sections go.

---

## GF-7. Cache simulator: `hash-trace` tool

> **PR: PR-G1, standalone.**
>
> - **`5b4ef06d` is clean:** +735 / -1 in 4 files, touching nothing else.
> - **Every import is unchanged upstream:** `TokenHasher`,
>   `cache_simulator.simulator` and `BaseCommand`.
> - **`transformers` is lazy,** and already in `requirements/common.txt`.
> - **Tests:** `tests/tools/test_trace_hasher.py`, on CPU.
> - **Add** `docs/design/cli/commands/tool/cache_simulator/hash-trace.md` to
>   follow the design-doc mirror convention.

**What.** `lmcache tool cache-simulator hash-trace` hashes a request trace
offline into LMCache chunk hashes, so a cache simulation can run on a trace
without serving it. The simulated hit-rate distribution in
`docs/design/v1/layerwise/` came from it.

**Where.**

- `lmcache/tools/cache_simulator/trace_hasher.py` (new).
- `lmcache/cli/commands/tool/cache_simulator/hash_trace_command.py` (new).
- `lmcache/cli/commands/tool/cache_simulator/__init__.py` registers it.

**Commits.** `5b4ef06d`.

**Tests.** `tests/tools/test_trace_hasher.py`.

**Size.** About +735 / -1. No dependencies and no expected conflicts.
**Easiest standalone PR on the fork.**

**Likely pushback.**

- **Does the hashing match the server's?** Reviewers will ask about every
  model and chunk size. `tests/tools/test_trace_hasher.py` hashes through the
  server's own `lmcache.v1.multiprocess.token_hasher.TokenHasher`; point to
  it.
- **No design doc.** `docs/design/cli/commands/` has no `tool/` subtree yet.
  Under the mirror convention, a short
  `docs/design/cli/commands/tool/cache_simulator/hash-trace.md` would be
  expected.

---

# Part 2 — Generic in shape, only used by Aerospike RDMA today

## GF-8. Transport-agnostic memory registration on `L1MemoryDesc`

> **PR: moved into PR-A9 (L1 RDMA windows, [aerospike_rdma.md](aerospike_rdma.md)).**
> It has no other consumer, so alone it would be dead code.
>
> - **Its only consumer:** `growth` is read only by `RdmaWindowPlan.validate_against`
>   (AS-P1).
> - **Drop `registration` or leave it unused.** Nothing ever sets it; it is
>   always `UNREGISTERED_MEMORY`.
> - **Tests:** the `TestMemoryRegistration` part of `test_rdma_registration.py`.

**What.** `L1MemoryDesc` (the description of L1's pinned memory that adapters
receive) gains two things:

- **`registration: MemoryRegistration`**: how the slab is registered for
  zero-copy I/O. The transport is one of `UNREGISTERED`, `IB_VERBS`,
  `MOONCAKE` or `NIXL`; the default is `UNREGISTERED_MEMORY`.
- **`growth: MemoryGrowthPolicy`**: `FIXED` for the mixed allocator,
  `GROWABLE` for the lazy one. A remote writer needs a fixed slab.

**Where.**

- `lmcache/v1/distributed/internal_api.py`
- `lmcache/v1/distributed/memory_manager/l1_memory_manager.py`
  (`get_l1_memory_desc`)

**Commits.** `92c26c82`.

**Why generic.** Mooncake and NIXL adapters register L1 too. A shared
descriptor saves each one from inventing its own.

**Pushback.** "Add it when a second transport needs it." Possibly best
bundled with AS-P1/AS-P2.

---

## GF-9. L1 helpers: `abort_write`, `delete_if_none_locked`, `RangeMemoryAllocator`, reserved prefix

> **PR: moved into PR-A9.** None of these helpers has a caller outside the
> RDMA windows:
>
> - `abort_write` is called only by `rdma_window_placer` (AS-P3);
> - `RangeMemoryAllocator` and `reserved_prefix_bytes` exist only for the
>   windows;
> - `delete_if_none_locked` is called only by tests (the real path is
>   `reclaim_rdma_window`).
>
> **Port it onto upstream's L1 changes:**
>
> - #5247 replaced `mode` with `tag`;
> - #5068 added `finish_write_and_delete`, which may already cover
>   `abort_write`;
> - #5393 (hugepages) touches the same allocators.
>
> **Tests:** `test_range_memory_allocator.py` and part of
> `test_l1_rdma_windows.py`, on CPU.

**What.**

- **`L1Manager.abort_write(keys)`**: drop a reserved-but-unfinished write,
  without the store notification that `finish_write` would send. Useful for
  any loader that fails mid-write.
- **`L1Manager.delete_if_none_locked(keys)`**: delete only if no read or
  write lock is held.
- **`RangeMemoryAllocator`** (new): a simple allocator over a fixed byte
  range.
- **`MixedMemoryAllocator(reserved_prefix_bytes=...)`**: keep a prefix of the
  slab out of general allocation.
- **Internal refactor:** `_remove_object`, `_clear_objects`,
  `_free_and_publish_evicted` and `_delete_all_if_none_locked`.

**Where.**

- `lmcache/v1/distributed/l1_manager.py`
- `lmcache/v1/memory_allocators/range_memory_allocator.py` (new)
- `lmcache/v1/memory_allocators/mixed_memory_allocator.py`

**Commits.** `83f391d3` (with the RDMA windows).

**Tests.**

- `tests/v1/test_range_memory_allocator.py`
- the generic cases in `tests/v1/distributed/test_l1_rdma_windows.py`

**Entanglement.** `83f391d3` mixes these with the RDMA window pool
(`L1Pool`, `reserve_write(pool=)`, `reclaim_rdma_window`), which is AS-P2 in
[aerospike_rdma.md](aerospike_rdma.md). They ship together in PR-A9, so no
hand-split is needed.

**Upstream overlap.** Dry-run merge conflicts in `l1_manager.py` and
`l1_memory_manager.py`. Upstream has since changed the same area:

- **#5247:** `reserve_write` semantics.
- **#5068:** `finish_write_and_delete`, which overlaps `abort_write`. Check
  whether it already covers our need.
- **#5393:** hugepages L1, which touches the allocators.

---

## GF-10. Prefetch controller: deferred L2 mode

> **PR: PR-G8, combined with GF-11, and *reimplemented* on prefetch v2.**
> This is not a rebase.
>
> **What upstream changed** (#5245, #5309, #5346, #5356):
>
> - `PrefetchRequestSpec`, `TrimPolicy.SPARSE` and `query_prefetch_result`
>   were removed. The replacement is
>   `PrefetchTaskSpec(key_groups, lock_mode, fetching_policy)`.
> - The controller and `lookup.py` were rewritten.
> - Upstream already has a `PrefetchResult(hit_cells, l1_hit_cells,
>   l2_hit_cells)`, which clashes with this item's
>   `PrefetchResult(retained, deferred_keys)`. Rename ours.
>
> **Library PR.** With the default `NO_L2_DEFERRAL`, no behavior changes. The
> only caller that sets a deferral is AS-M2 (PR-A11).
>
> **Tests:**
>
> - `test_prefetch_deferral.py` (mock adapter), rewritten for
>   `PrefetchTaskSpec`;
> - `L2_PREFETCH_DEFERRED` plus `EVENTS.md`. v2 also removed
>   `L2_PREFETCH_LOOKUP_COMPLETED`.

**What.** A lookup can ask the prefetch controller to *count* L2 hits without
loading them. The caller then fetches those keys itself at retrieve time.
The pipelined RDMA path does this so that retrieve can stream them layer by
layer.

- **API** (`api.py`): an `L2Deferral` protocol (`accepts(adapter_indices,
  keys) -> bool`), `NO_L2_DEFERRAL`, `PrefetchRequestSpec.l2_deferral`, and
  `PrefetchResult(retained, deferred_keys)`.
- **Controller** (`prefetch_controller.py`): `_defer_request` replaces the
  reserve-and-load steps when the deferral accepts.
  - It releases all L2 locks.
  - It keeps the L1 hit locks.
  - It publishes `L2_PREFETCH_DEFERRED`.
  - It completes with the deferred keys.
- **New query.** `query_prefetch_outcome(request_id)` returns the full
  `PrefetchResult`. `query_prefetch_status` stays as a wrapper.
- **Storage manager.** `StorageManager.query_prefetch_outcome`.
- **Event.** `EventType.L2_PREFETCH_DEFERRED` in
  `lmcache/v1/mp_observability/event.py`, documented in
  `docs/design/v1/mp_observability/EVENTS.md`.

**Where.**

- `lmcache/v1/distributed/api.py`
- `lmcache/v1/distributed/storage_controllers/prefetch_controller.py`
  (about +100)
- `lmcache/v1/distributed/storage_manager.py`
- `lmcache/v1/mp_observability/event.py`

**Commits.** `0644e859` (C9 design and deferred mode), `aecc663e`.

**Tests.** `tests/v1/distributed/test_prefetch_deferral.py`, plus the updates
in `tests/v1/multiprocess/test_lookup_wait_prefetch.py`.

**Upstream overlap: high.** Upstream rewrote the prefetch controller in the
"prefetch controller v2" series (#5245, #5309, #5346, #5356). Expect to
re-implement `_defer_request` against v2, not rebase it.

**Likely pushback.**

- **Deferred records have no lock**, so they may be evicted from L2 before
  the retrieve fetches them. The caller must handle that, and ours falls back
  to recompute.
- **Who else would defer?** That is a fair question. The only answer today is
  "a streaming L2 reader".

---

## GF-11. `StorageManager.load_into_l1` and `lock_resident_keys`

> **PR: PR-G8, with GF-10.**
>
> - **Why combined:** it shares the v2 rewrite and the `ResidentKeys` type in
>   `api.py`.
> - **Rewrite:** `load_into_l1` uses the removed `PrefetchRequestSpec` and
>   `SPARSE`.
> - **Callers:** `lock_resident_keys` (AS-L3, PR-A7) and `load_into_l1`
>   (AS-M3, PR-A11).
> - **Tests:** `test_storage_manager_load_into_l1.py` (mock adapter),
>   rewritten for v2.

**What.** Two narrow storage-manager entry points for a caller that does its
own L2 reads:

- **`load_into_l1(...)`** loads named keys from L2 into L1 outside the
  prefetch path. The pipelined retrieve uses it as its whole-object fallback.
- **`lock_resident_keys(keys) -> ResidentKeys`** read-locks whichever keys
  are already in L1, for keys another request shares.
- `_release_late_load` covers the cleanup.

**Where.** `lmcache/v1/distributed/storage_manager.py` and
`lmcache/v1/distributed/api.py` (`ResidentKeys`).

**Commits.** `aecc663e`.

**Tests.** `tests/v1/distributed/test_storage_manager_load_into_l1.py`.

**Notes.** These only make sense next to GF-10. Both ship in PR-G8.

---

## GF-12. Layerwise arrival/sink contract, pump and fakes

> **PR: PR-G6, a library PR, standalone after a re-cut.**
>
> - **Trim `lmcache/v1/layerwise/__init__.py`** to `contract`, `pump` and
>   `fakes`. The fork's version imports `planner` and `native_fetch` (AS-L).
> - **Remove the `"aerospike"` entry** from `tests/v1/layerwise/conftest.py`.
>   `aerospike_harness.py` imports `layerwise_source` at module level, so it
>   would error instead of skipping. It comes back in PR-A10.
> - **Tests**, all pure Python:
>   - `test_layer_arrival_pump`;
>   - `test_layer_fetch_plan`;
>   - `test_fakes`;
>   - `test_arrival_source_conformance` (scripted source only);
>   - `test_load_sink_conformance` (recording sink).
> - **Commits to exclude:** most of the listed commits also carry other
>   items' code:
>   - `d6d48818` is `layerwise_source` (AS-P4);
>   - `4c119cd7` is `layerwise_sink` (PR-G7);
>   - `5aa8271c`, `93a77e56` and `e9cd0689` touch the planner.
> - **`read_write_ids`.** Leave it off the contract here; PR-A6 adds it. If
>   it moves to a separate `RecordWriteIdReader`, the fakes and
>   `pipelined_retrieve` follow.
>
> **Follow-up: PR-G7, the MP layer load sink.**
>
> - **Contents:** `layerwise_sink.py`, listed in the fork under AS-M3. It is
>   generic: it implements `LayerLoadSink` on GF-3's per-layer staging. It
>   also takes the "Arrival-driven launch" doc section.
> - **Prerequisites:** PR-G4 and PR-G6.
> - **Why its own PR:** it gives the contract a real implementation without
>   any Aerospike code.
> - **Tests:** `test_multiprocess_sink`, `test_track_b_sink_shape`, the
>   multiprocess case of the conformance test, and the sink tests moved out
>   of PR-G4's file. CPU torch; no GPU.

**What.** The transport-agnostic half of `lmcache/v1/layerwise/`:

- **`contract.py`** defines the producer and consumer protocols.
  - `LayerArrivalSource` (`begin_fetch` / `poll_layer` / `finish_fetch` /
    `abandon_fetch`) reports per-layer status as `PENDING`, `RESIDENT` or
    `UNSERVABLE`, under explicit non-zero generations.
  - `LayerLoadSink` (`begin_load` / `load_layer` / `finish_load` /
    `abandon_load`) is the consumer.
  - `LayerFetchPlan` and `SlotPlacement` describe the fetch.
  - Typed errors, including `PlanTooLargeError`.
  - Since D-14 (`e9cd0689`), `LayerArrivalSource.read_write_ids(keys)` too.
    It returns each object's write ID, so the planner can name per-write
    segments. That is Aerospike's record layout leaking into the generic
    contract (see BF-8 in [bug_fixes.md](bug_fixes.md)). Before upstreaming,
    consider moving it onto the Aerospike source or behind a key-resolver
    hook.
- **`pump.py`**: `LayerArrivalPump.run` / `run_resumable` drives any source
  into any sink with per-layer timeouts. `LoadLeftOpenError` signals a
  transport failure that leaves the load open.
- **`fakes.py`**: `ScriptedLayerArrivalSource`, `RecordingLayerLoadSink`,
  `UnservableLayerArrivalSource`, and the `ArrivalDriver` and `LoadObserver`
  conformance hooks.

Any streaming L2 backend could implement `LayerArrivalSource`, for example
NIXL or Mooncake reading layer by layer. Then the pump and the MP sink (GF-3
plus AS-M3) give it layerwise load for free.

**Where.** `lmcache/v1/layerwise/contract.py`, `pump.py`, `fakes.py` and
`__init__.py`.

**Commits.** `a9177fc8`, `e9f11c89`, `d6d48818`, `4c119cd7`, `600aa620`,
`5aa8271c`, `ca94e5c4`, `3a57aad1`, `6c913eb7`, `83210bf1` and `93a77e56`.

**Tests.**

- `tests/v1/layerwise/test_layer_arrival_pump.py`
- `test_layer_fetch_plan.py`, `test_fakes.py`
- `test_arrival_source_conformance.py`, `test_load_sink_conformance.py`

**Entanglement.** The rest of the package (`planner.py`, `request_fetch.py`,
`native_fetch.py`, `deferral.py`, `pipelined_retrieve.py`) assumes
Aerospike-style records, RDMA windows and slots. It is listed under AS-L in
[aerospike_rdma.md](aerospike_rdma.md). `track-c-status.md` reports that the
whole package passes its 254 tests when copied onto `dev`, once
`native_connector_l2_adapter._object_key_to_string` is made public.

**Likely pushback.**

- **Test fakes in the library package.** `fakes.py` lives under `lmcache/`
  and is imported by `lmcache.v1.layerwise` (ledger D-04). Move it under
  `tests/` or a `testing` submodule.
- **Generations and slots** read as RDMA-specific vocabulary in a "generic"
  contract. After the PR, the docstrings no longer mention RDMA immediates.
  `MAX_SLOTS_PER_REQUEST` is justified by the native fetch table instead,
  which helps. The `contract.py` module docstring still draws the Aerospike
  data flow.
- **`read_write_ids`** is a backend-specific method on the generic source
  (see above).

---

## PR plan

Every PR below builds and passes its own tests on upstream `dev` plus its
listed prerequisites. The **Kind** column says whether a user can reach it:

- **Feature:** a user can turn it on.
- **Library:** real feature code plus its unit tests. CI runs those tests
  from the day the PR merges, but no user path reaches the code until the
  PR named after "used by" wires it in.

**Decision (2026-10-01): keep the stack.** Library PRs stay separate rather
than merging into their consumers. Each library PR's description must name
the PR that uses it, and link the whole series. Upstream merges stacks like
this ("[3/N] …" #5247, "RFC-4465 step 2" #4940).

| PR | Title | Contents | Prerequisites | Kind | How it is tested | Size (approx.) |
| --- | --- | --- | --- | --- | --- | --- |
| PR-G1 | Cache simulator: `hash-trace` | GF-7, plus its design doc | none | Feature | `test_trace_hasher.py`, CPU | +735 (+doc) |
| PR-G2 | MP kernel: transfer a layer sub-range | GF-1, re-ported, without BF-3 hunks | none | Library (kernel API); used by PR-G4 | `test_mp_mem_kernels.py`, CUDA GPU | ~+250 / -15 |
| PR-G3 | MP layerwise: schedule and cross-process layer progress | GF-2, plus the core of `layerwise-load.md` | none | Library; used by PR-G4, PR-G5 | CPU unit tests; GPU overlap test (ROCm also needs PR-B4) | ~+1,570, +~150 doc |
| PR-G4 | MP layerwise: daemon per-layer H2D retrieve | GF-3 library half; trimmed tests | PR-G2, PR-G3 | Library; used by PR-G5 | CPU with mocked kernel, plus 3 CUDA cases | ~+1,900 |
| PR-G5 | MP layerwise load (feature) | GF-3 module half; GF-4, re-ported onto `RequestClient`; GF-5; regression fixes; D-17 guard (written); new `--use-layerwise` config test; module-level test (written) | PR-G4 | **Feature** (`--use-layerwise`, `lmcache.mp.use_layerwise`; off by default) | CPU, vLLM-installed and CUDA tests | ~+1,950 (estimate) |
| PR-G6 | Layerwise arrival/sink contract, pump and fakes | GF-12, with trimmed `__init__` and conftest | none | Library; used by PR-G7, PR-A6, PR-A7, PR-A10 (in production first by PR-A11) | pure Python | ~+3,100 incl. tests |
| PR-G7 | MP layer load sink | `layerwise_sink.py` and its harness and tests (from AS-M3) | PR-G4, PR-G6 | Library; used by PR-A11 | CPU torch | ~+1,040 |
| PR-G8 | Storage: deferred L2 lookup and caller-driven L1 load | GF-10 + GF-11, rebuilt on prefetch v2 | none | Library (default off); used by PR-A7, PR-A11 | mock adapter | ~+700 after rewrite |

**Before opening PR-G3 to PR-G5,** agree the design against upstream PR
#4460 with the maintainers. Otherwise both stall.

**Fallback if maintainers refuse library PRs:** merge PR-G3, PR-G4 and
PR-G5 into one PR of roughly +5,400 lines. PR-G2 can stay separate: it is a
kernel API with its own tests. A second fallback, not yet checked against the
code, is to re-cut layerwise into user-reachable vertical slices:

1. the flag, the protocol and the per-layer wait, with an all-at-once copy;
2. the real per-layer copy.

**PR-G6 to PR-G8** only gain a production caller with the Aerospike
pipelined path (PR-A7 and PR-A11 in [aerospike_rdma.md](aerospike_rdma.md)).
Open them shortly before those, not on their own merits.

**Deferred:** the TTFT simulator (GF-6), after PR-G7, or rewritten for
PR-G5.

**Moved to other files:** GF-8 and GF-9 into PR-A9.
