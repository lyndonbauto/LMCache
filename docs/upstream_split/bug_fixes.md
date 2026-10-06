# Bug fixes to existing LMCache code

Bugs we found and fixed on this fork in code that came from main LMCache.
**Upstream `dev` still has these bugs; only the fork has the fixes**
(checked against `8d7d2c47` on 2026-10-01). Each entry is a candidate for a
small standalone PR, unless noted otherwise.

Companion files:

- [general_features.md](general_features.md): new features that are not
  Aerospike-specific.
- [aerospike_rdma.md](aerospike_rdma.md): the Aerospike backend and the RDMA
  pipelined fetch.

**Baseline.** These files compare fork branch `prototype-stage1` /
`lmcache-pr` (`49f18d12`) with upstream `LMCache/LMCache` `dev` (`8d7d2c47`,
fetched 2026-10-01). The merge base is `caba24c2` (2026-09-17). The fork is
232 commits ahead (218 not counting merges) and 115 behind. To reproduce a
diff:

```bash
git remote add upstream https://github.com/LMCache/LMCache.git  # once
git fetch upstream dev
git diff $(git merge-base HEAD upstream/dev) HEAD -- <path>
```

**Update for the `prototype-stage-1a` PR (2026-10-01).** The PR into
`prototype-stage1` (6 commits, `62b384d6`..`61d39ffa`) replaces the RDMA
transport. `prototype-stage1` itself has also moved on to `0a339c63`, 34
commits past `49f18d12`. What that means for this file:

- **BF-1 to BF-7 are unaffected.** The PR touches none of their code.
- **New: BF-8**, the concurrent-writer fix (D-14, `e9cd0689`). It is on
  `prototype-stage1` and fixes upstream's own `do_single_set`.
- **Four fork-only fixes are now moot,** because the code they fixed is
  deleted. They are marked in the fork-only table.
- **New ledger entries.** D-13, D-16 and D-17 are added under "Found but not
  fixed". D-17 was first thought to be an LMCache bug, but has since been
  traced to vLLM ([vllm#49250](https://github.com/vllm-project/vllm/issues/49250)).

**PR isolation check (2026-10-01).** Every item was re-checked against
upstream `dev` for these rules:

- **Builds alone.** The code needs only upstream plus the PR's stated
  prerequisites.
- **Tests run alone.** The listed tests' imports and fixtures exist in that
  state.
- **Does something.** Its behavior can be reached through its own tests or
  through upstream code.
- **Cut cleanly.** The PR is cut from the final file state; hunks belonging
  to other items are excluded.

The [PR plan](#pr-plan) at the end gives the result: 7 PRs (PR-B1 to
PR-B7). Each item section starts with a **PR** line saying which PR it
ships in and what must change first.

**No commit cherry-picks cleanly.** Every PR here is re-cut or re-ported by
hand. Upstream has moved under several of them:

- `protocols/` was deleted (#5161);
- `torch_ops` became a package;
- the ROCm platform moved to `platform/devices/rocm/`;
- `reserve_write` lost its `mode` argument (#5247);
- prefetch v2 replaced `PrefetchRequestSpec`.

**How the fixes were found.** Commits rarely say "fix". The evidence comes
from four places:

- commit bodies;
- the defect tables in `functional/day1/SUMMARY.md` and `functional/LEDGER.md`;
- the merge review section of `functional/day1/SUMMARY.md`;
- the diffs of upstream files.

Fixes to code that exists only on this fork are listed separately at the end.
They are not upstream bugs and travel with their feature PRs.

**Sign-off.** Only 30 of the 218 commits carry a DCO `Signed-off-by`. Every PR
cut from them needs `git commit -s` when it is re-committed.

---

## Summary

| ID | Fix | Size (+/-) | Bug still in upstream `dev`? | Ships in |
| --- | --- | --- | --- | --- |
| BF-1 | Worker never re-registers after a fast LMCache restart | +400 / -36 | Yes | PR-B1 (alone) |
| BF-2 | A failed L2 store logs no reason | +215 / -2 (+ adapter lines) | Yes | PR-B2 (alone; tests re-ported) |
| BF-3 | ROCm: `torch_ops` loads the wrong HIP runtime and calls `cudaMemcpy` | +225 (+ torch_ops lines) | Yes | PR-B3 (alone; re-port) |
| BF-4 | ROCm: MP event IPC fails with more than one request per step | +1,286 | Yes | PR-B4, **after PR-B3** |
| BF-5 | Aerospike build: libyaml links wrongly when the dev symlink dangles | ~+60 | Yes | PR-B5, with BF-6's Aerospike half |
| BF-6 | `assert` used for runtime validation (2 sites) | tiny | Yes | **Split:** one site each into PR-B5 and PR-B6 |
| BF-7 | Order-dependent failure in `test_lmcache_driven_layout_registry` | test only | Yes (the file is upstream's, unchanged) | PR-B6, with BF-6's `object_group_transfer` half |
| BF-8 | Aerospike: two concurrent stores of one sharded key leave a mixed object (D-14) | +931 / -103 | Yes | **Split:** the native fix in PR-B7; `read_write_ids` and the planner half go with the planner PR (PR-A6 in [aerospike_rdma.md](aerospike_rdma.md)) |

The PR plan is at the end of this file.

---

## BF-1. The worker never re-registers if LMCache restarts within one heartbeat

> **PR: PR-B1, standalone.** It depends on nothing else: `ping.py` imports only
> `RequestClient`, and nothing touches `RegisterKvCacheResponse` or
> layerwise code. Take only commits `717ec8e4` and `d41162b4`.
>
> - **Exclude the layerwise hunks** of the test files: `614b04fa`, `061fb52a`
>   and `fa114939` in `test_worker_liveness.py`; `48288b94` and `1138782d`
>   in `test_vllm_mp_adapter.py`.
> - **`protocols/controller.py` no longer exists upstream** (#5161). The PING
>   contract goes in the `ManagementModule.ping` docstring, which still says
>   "Always True" (`management.py:115`).
> - The two `send_ping` monkeypatches have moved to
>   `test_vllm_mp_adapter.py:1351` and `:1478`.
> - **Upstream's new SGLang connector** (`unified_lmcache_mp_connector.py:587`)
>   pings and ignores the answer. BF-1 doesn't break it, but doesn't make it
>   re-register either. Say so in the PR.
> - **Tests:** the four listed files. Pure Python with mocks; no GPU.

**Symptom.** If the LMCache MP server restarts faster than vLLM's heartbeat
interval (10 s), vLLM never re-registers its KV cache. Every later lookup
misses with `No GPU context found`, and nothing recovers short of restarting
vLLM. Day 1 recorded it as severity S2 (`functional/day1/SUMMARY.md`,
"Defects and findings").

**Cause.** Upstream's `ManagementModule` answers PING with a constant `True`
("Always True"). The worker's `HeartbeatThread` re-registers only on a
transition from unhealthy to healthy, and that needs a PING to fail while the
server is down. A restart shorter than the interval never fails a PING. The
new server holds no registration, but every PING still says healthy.

**Fix.**

- **Server.** PING returns whether any liveness target holds a registration
  for the calling instance: `any(target.touch_instance(instance_id) ...)`.
  `InstanceLivenessTarget.touch_instance` now returns `bool`, and each PING is
  logged at debug level with the result.
- **Shared classification.** New `lmcache/v1/multiprocess/ping.py` adds
  `PingOutcome {REGISTERED, UNREGISTERED, UNREACHABLE}` and
  `probe_server(req_client, timeout, instance_id)`.
- **vLLM worker.** `HeartbeatThread` runs its recover callback (re-register)
  on `UNREGISTERED` as well as on recovery. The old `send_ping` is removed.
  Workers with no recover callback (the SGLang path) warn once, through
  `_can_recover_registration` and `_reported_unregistered`.
- **ATOM worker.** `_HeartbeatThread` follows the same logic.

**Where.**

- `lmcache/v1/multiprocess/ping.py` (new): `PingOutcome`, `probe_server`.
- `lmcache/v1/multiprocess/modules/management.py`: the PING handler.
- `lmcache/v1/multiprocess/engine_module.py`:
  `InstanceLivenessTarget.touch_instance -> bool`.
- Implementations of `touch_instance`:
  - `lmcache/v1/multiprocess/modules/engine_driven_transfer.py`
  - `lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py`
  - `lmcache/v1/multiprocess/modules/experimental/qstore.py`
- `lmcache/v1/multiprocess/protocols/controller.py`: the PING docstring.
- `lmcache/integration/vllm/vllm_multi_process_adapter.py`:
  `HeartbeatThread`; `send_ping` removed.
- `lmcache/integration/atom/multi_process_adapter.py`: `_HeartbeatThread`.
- `docs/design/v1/multiprocess/worker_liveness.md`.

**Commits.** `717ec8e4` (re-register), `d41162b4` (debug log of each PING).

**Tests.**

- `tests/v1/test_heartbeat_reregistration.py` (new).
- `tests/v1/multiprocess/test_worker_liveness.py`
- `tests/v1/test_atom_mp_adapter.py`
- `tests/v1/test_vllm_mp_adapter.py`
- End to end: the `RESTART_AFTER_PING` mode in the functional harness
  (`02d03f70`) restarts LMCache right after a heartbeat. Evidence is in
  `functional/day1/SUMMARY.md`, "Fixes after Day 1".

**Size.** About +400 / -36 over 10 files. No dependency on any other item.

**Upstream overlap.** Merging conflicts in `vllm_multi_process_adapter.py`,
`engine_module.py`-adjacent modules and `test_worker_liveness.py`. Upstream
has since touched heartbeats in #5233 ("start scheduler heartbeats for all
servers") and the connect path in #4671 ("bounded TCP connect").

**Likely pushback.**

- **The PING wire type is still `bool`, but its meaning changed.** An old
  worker talking to a new server reads `False` as "server unhealthy". It would
  sit in degraded mode, where before it silently missed every lookup. Both
  outcomes mean no cache, but say so in the PR.
- **Is re-registering from the heartbeat thread safe?** Reviewers will ask
  about this while the engine is running. The recover callback is the one
  upstream already uses after an outage.
- **A gap remains, by design.** Requests sent between the restart and the
  next heartbeat still recompute, up to one interval (`LEDGER.md` D-01).

---

## BF-2. A failed L2 store logs no reason

> **PR: PR-B2, standalone; its tests need re-porting.** The adapter change only
> passes through the `error` variable that upstream already has
> (`native_connector_l2_adapter.py:510`). There is no coupling to the RDMA
> code.
>
> - **`test_l2_store_failure_reason.py` uses fork-era APIs.** Rename
>   `AdapterDescriptor` to `L2AdapterDescriptor` (from
>   `storage_controllers/utils.py`). Replace `reserve_write(..., mode="new")`
>   with the `tag=` form, or drop the argument.
> - **The Aerospike integration case needs `import uuid`**, which came with
>   `f4ac45ec` (AS-1). Add the import in this PR.
> - **Docs.** The `EVENTS.md` row for `L2_STORE_COMPLETED` is now at line
>   84, and also has `key_count_per_salt`.
> - **Tests:**
>   - most cases are pure Python;
>   - the store-controller case needs a device;
>   - the integration case needs Aerospike CE, which upstream's
>     `aerospike_integration.yml` already runs.

**Symptom.** A failed store logged
`Store task N to adapter 0 failed for keys: [...]`: a full key list and no
cause. Day 1 recorded it as S3.

**Fix.**

- **The result carries the reason.** `L2StoreResult` (an `int` subclass) gains
  an optional failure reason that doesn't take part in its int value, read
  with `failure_reason()`. Existing comparisons are unaffected.
- **The native adapter fills it in.** `NativeConnectorL2Adapter` passes the
  backend's error string into the result.
- **The store controller uses it.** It records the reason on
  `InFlightStoreTask.l2_failure_reason` and logs
  `Store task %d to adapter %d (%s) failed for %d key(s): %s`. The key list
  moves to a debug log.
- **The event carries it.** The `L2_STORE_COMPLETED` event gains
  `failure_reason` metadata.

**Where.**

- `lmcache/v1/distributed/internal_api.py`: `L2StoreResult`.
- `lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py`: the
  store completion path.
- `lmcache/v1/distributed/storage_controllers/store_controller.py`.

**Commits.** `29ec44cf`.

**Tests.** `tests/v1/distributed/test_l2_store_failure_reason.py` (new), plus
a case in `tests/v1/distributed/test_aerospike_l2_integration.py`.

**Size.** About +215 lines, plus a few in the adapter and `internal_api.py`.
No dependencies.

**Upstream overlap.** Upstream `store_controller.py` still has the old message
(checked on `8d7d2c47`). `native_connector_l2_adapter.py` conflicts on merge,
because the RDMA work also touches it. Cut this PR from `dev`, not from the
fork.

**Likely pushback.**

- **The default log line changes.** Keys moved to debug level, and some
  operators may grep for them.
- **The event schema changes, but the docs don't say so yet.**
  `docs/design/v1/mp_observability/EVENTS.md` (line 74) lists the
  `L2_STORE_COMPLETED` fields without `failure_reason`. Add it in the PR.

---

## BF-3. ROCm: `torch_ops` loads the wrong HIP runtime and calls a CUDA symbol

> **PR: PR-B3, standalone; re-port onto the `torch_ops/` package.**
>
> - **Where the bug lives upstream:** `_tensor_from_ptr.py:26` and
>   `mem_kernels.py:603`. Upstream `test_torch_ops.py:83` still calls
>   `_get_copy_lib`.
> - **Exclude** the fork's `3a839927` edits to `torch_ops.py` (layerwise
>   `DeviceOps`, GF-1) and its "try `cuda_ops` first" hunk (GF-1).
> - **The test reaches private names:** `test_runtime_libs.py` uses
>   `_load_gpu_memcpy`, `_GpuMemcpy`, `_MEMCPY_DEFAULT` and `_get_gpu_memcpy`.
>   Either make the tested functions public or test through public behavior.
>   The coding standards discourage tests on private members.
> - **Tests:** mostly fakes, run anywhere; one GPU case. Proving it on ROCm
>   needs an MI300X; quote the Day 1 evidence.

**Symptom.** On ROCm, the Python `torch_ops` path either crashed or silently
did the wrong copy. Found on an MI300X in the first end-to-end layerwise
runs.

**Cause.** `torch_ops._get_copy_lib()` found the HIP runtime with
`ctypes.util.find_library`. That returned the system `libamdhip64.so.5`, while
PyTorch had loaded `.so.7`. It then bound `cudaMemcpy`, which HIP doesn't
export. As a result:

- the pointer-mode `lmcache_memcpy_async` fell back to a CPU byte copy of
  *device* pointers;
- `_tensor_from_ptr` raised.

**Fix.**

- New `lmcache/v1/platform/runtime_libs.py` with `load_runtime_library`. It
  prefers the library already mapped into the process, read from
  `/proc/self/maps`, over a search by name.
- In `torch_ops`: a `_GpuMemcpy` binding, `_load_gpu_memcpy(hip_version,
  cuda_version, load_library)` and `_get_gpu_memcpy()`. They bind `hipMemcpy`
  on ROCm and `cudaMemcpy` on CUDA, replacing `_get_copy_lib()`.

**Where.** `lmcache/v1/platform/runtime_libs.py` (new) and
`lmcache/v1/platform/torch_ops.py`.

**Commits.** `abd35b0c`.

**Tests.** `tests/v1/platform/test_runtime_libs.py` (new). In
`tests/v1/test_torch_ops.py`, one line now checks `_get_gpu_memcpy()` instead
of `_get_copy_lib()`.

**Size.** +225 in the new files, plus the `torch_ops` edits. No dependencies.

**Upstream overlap: needs a re-port, not a cherry-pick.** Upstream split
`torch_ops.py` into a package. The bug is still there, now in two places:

- `lmcache/v1/platform/torch_ops/_tensor_from_ptr.py`: `_get_copy_lib` with
  `find_library`;
- `lmcache/v1/platform/torch_ops/mem_kernels.py`: about line 603 calls
  `_get_copy_lib()` and uses the result as `libcudart`.

**Likely pushback.**

- **`/proc/self/maps` is Linux-only.** The fallback to a name search must
  stay, and the PR should say so.
- **Reviewers will want a ROCm CI run.** Upstream has limited ROCm CI. Quote
  the MI300X evidence: Day 1, Llama-3.1-8B, 130/130 token-exact.

---

## BF-4. ROCm: multiprocess event IPC fails with more than one request per step

> **PR: PR-B4, after PR-B3.** `event_ipc.py` imports `load_runtime_library` from
> BF-3's `runtime_libs.py`. It is functional without layerwise, because
> upstream's vLLM connector shares one event across every request in a step
> (`lmcache_mp_connector.py:896,977`), and the server imports it once per
> request (`lmcache_driven_transfer.py:648,915`). Not reproduced on upstream
> hardware.
>
> - **Ship only `6eb51a66`'s final state,** squashed with `7cd72356`. Exclude
>   the `track-b-acceptance.md` hunks.
> - **Move the code.** It goes into `platform/devices/rocm/`, and the design
>   doc to `docs/design/v1/platform/devices/rocm/`.
> - **It imports private helpers** `_raw_stream_handle` and
>   `_resolve_device_index` from `platform/devices/cuda/utils.py:133,157`.
>   Make them public or copy them; reviewers will flag private access.
> - **Tests:** `test_rocm_event_ipc.py`. Its 15 fake-based cases need only
>   Linux shared memory; 2 cases are marked `requires_rocm`.

**Symptom.**

- In MP mode on ROCm, any step that carries more than one request fails with
  `hipErrorInvalidValue` when the worker imports an IPC event handle.
- With per-layer events re-recorded on every retrieve (the layerwise path) and
  vLLM async scheduling, 4 or more concurrent cached requests deadlock.

**Cause.** On ROCm an interprocess event is a ROCr IPC signal, which differs
from CUDA in three ways (reproduced with two processes on an MI300X):

1. **A process can open a given handle only once,** even after dropping the
   first import. Several requests in one step carry the same event, so the
   second import fails. This affects plain MP mode, not just layerwise.
2. **Handle bytes repeat once the exporter frees an event,** so handles can't
   be cached or recycled safely.
3. **A queued wait blocks on the signal's live value,** not on the record
   that was current when the wait was enqueued, which is CUDA's rule. If the
   event is re-recorded first, step N waits for step N+1's copy, which is
   queued behind step N: a deadlock.

**Fix.** New `RocmEventIPCBackend` implements events as timeline semaphores
in shared memory instead of native HIP IPC events:

- **Storage.** Each process owns one POSIX shared-memory region,
  `lmcache_rocm_evt_<pid>_<random>`, registered with `hipHostRegister`
  (portable and mapped).
- **Record and wait.** Record enqueues `hipStreamWriteValue64(slot, seq)`;
  wait enqueues `hipStreamWaitValue64(slot >= seq)`, using the sequence
  published when the wait was queued. That restores CUDA's wait semantics.
- **Imports.** A process attaches each peer region once.
- **Selection.** `RocmDeviceSpec.event_ipc_backend` returns this backend
  unless isolated IPC is on.

`7cd72356` first fixed the "open once" problem alone; `6eb51a66` replaced it
with the timeline-semaphore design. Ship only the final design.

**Where.**

- `lmcache/v1/platform/rocm/event_ipc.py` (new, about 750 lines):
  `SemaphoreOps`, `HipSemaphoreOps`, `_SemaphoreRegion`, `RocmSemaphoreEvent`,
  `RocmEventIPCBackend`.
- `lmcache/v1/platform/rocm/__init__.py`: `RocmDeviceSpec.event_ipc_backend`.
- `docs/design/v1/platform/rocm/event_ipc.md`: the design and the three ROCr
  behaviours, with the probes.

**Commits.** `7cd72356` (superseded) and `6eb51a66`.

**Tests.** `tests/v1/platform/test_rocm_event_ipc.py` (new). End to end on
MI300X: Day 1 runs with async scheduling and 2 to 24 concurrent requests.

**Size.** About +1,286 including the design doc and tests. No functional
dependency. The deadlock half matters only to code that re-records an
exported event, which today means layerwise load (GF-2 / GF-3).

**Upstream overlap: needs a re-port.**

- **The ROCm platform package moved.** Upstream #4957 moved it from
  `lmcache/v1/platform/rocm/` to `lmcache/v1/platform/devices/rocm/`. Our
  `__init__.py` change hits a rename conflict, and `event_ipc.py` should land
  at `platform/devices/rocm/event_ipc.py`.
- **Upstream already has the same idea for CUDA.**
  `lmcache/v1/platform/devices/cuda/timeline_semaphore_event_ipc.py` (used for
  isolated IPC) records with `cuStreamWriteValue64` and waits with
  `cuStreamWaitValue64` on IPC memory handles.
- **Upstream #5280** retains exported IPC events so peers can import their
  handles. It touches the same lifetime question; re-check after rebasing.

**Likely pushback.**

- **"Why not generalize the existing timeline-semaphore backend to HIP?"**
  This is the main one. Be ready to show why shared host memory plus
  `hipHostRegister` was chosen over HIP IPC memory handles. Alternatively,
  factor the common slot and sequence logic out of both backends.
- **Lifecycle risk.** POSIX shared memory left behind by a crashed process
  needs cleanup.
- **The size of the PR (~750 lines)** for a platform reviewers can't easily
  test.

---

## BF-5. Aerospike build: libyaml links wrongly when the `.deps` dev symlink dangles

> **PR: PR-B5, standalone, together with BF-6's Aerospike half.** The bug is
> still upstream (`aerospike.py:62-71`).
>
> - **Re-port, not cherry-pick.** The hunk's context includes the fork-only
>   `is_rdma_requested` and `is_efa_requested`.
> - **Keep** the workflow's build-profile path trigger. **Exclude** the
>   `layerwise/**` and `native_connector_l2_adapter.py` triggers.
> - **Tests: written on `lmcache-pr`.** `tests/test_build_profiles_aerospike.py`
>   lays out fake `.deps` and system library trees and checks the libyaml
>   link inputs that `AerospikeStorageBackend.build` chooses:
>   - a real `.deps` `libyaml.so`;
>   - a dangling symlink (the regression);
>   - a runtime that only `find_library` finds;
>   - static-only.
>
>   It needs torch and POSIX, not Aerospike. Its one seam is a public
>   `SYSTEM_LIB_DIRS` constant, which replaces the hard-coded tuple in
>   `_system_yaml_soname`; port that with the fix. The test sets the RDMA
>   environment variables only through `monkeypatch.delenv`, so it runs on a
>   profile without them.

**Symptom.** The Aerospike extension built, then failed at import with an
undefined `yaml_parser_set_input_file`. CI extracted only the `libyaml-dev`
package, whose `libyaml.so` is a symlink into the runtime package. Without
the runtime package the symlink dangles, `ld` skips it and picks `libyaml.a`,
which cannot satisfy `libaerospike.so` (that library doesn't declare its
libyaml dependency).

**Fix.**

- **Build profile.** In `setup_extensions/storage_backend_profiles/aerospike.py`,
  `_system_yaml_soname()` finds the system runtime (for example
  `libyaml-0.so.2`). The profile links it by soname (`-l:libyaml-0.so.2`)
  whenever `.deps` has no real shared `libyaml.so`.
- **CI.** `.github/workflows/aerospike_integration.yml` also extracts
  `libyaml-0-2` and checks that the symlink resolves.

**Where.** Those two files. The same commits also add
`native_connector_l2_adapter.py`, `lmcache/v1/layerwise/**` and the build
profile to the workflow's path triggers, and add
`test_aerospike_record_layouts_integration.py` to the run. Leave both out of
a fix-only PR.

**Commits.** `99149ca0`.

**Tests.** The CI workflow itself. `docs/design/v1/layerwise/track-c-status.md`
mentions a build-profile test that fails on `dev`'s profile, prepared on
branch `fix/aerospike-libyaml-soname`. That branch is not in this clone,
probably only on the test box. It has been re-created as
`tests/test_build_profiles_aerospike.py`.

**Size.** About 60 lines. No dependencies. This is upstream's own Aerospike
backend (#3458), so it is a plain bug fix even though it is Aerospike-specific.

**Upstream overlap.** None seen. The profile also gains the RDMA build flags
(AS-R1 in [aerospike_rdma.md](aerospike_rdma.md)) and the `shard_plan.cpp`
source (AS-3). Split those hunks out.

**Likely pushback.** Little. Hard-coded library directories
(`/usr/lib/x86_64-linux-gnu`, `/usr/lib64`, `/usr/lib`) may draw a comment
about other architectures.

---

## BF-6. `assert` used for runtime validation

> **PR: split.** The Aerospike-adapter site (`aerospike_l2_adapter.py:192`
> upstream) goes in PR-B5. The `object_group_transfer.py:218` site goes in
> PR-B6; it does **not** need to wait for GF-3. Each PR has only one assert;
> there are no sibling checks.
>
> - **`object_group_transfer` site: test written on `lmcache-pr`.**
>   `tests/v1/multiprocess/test_downsample_block_ids.py` checks the docstring
>   example (a 64-token window keeps the last two blocks of each chunk) and
>   the `ValueError` for a partial chunk. It uses a fake cache context and
>   runs on CPU.
> - **Aerospike site: no test, on purpose.** The type check can't be reached
>   through the public API. `create_l2_adapter_from_registry` picks the
>   factory from the config's own type, so the Aerospike factory only ever
>   gets an `AerospikeL2AdapterConfig`. A test would have to import the
>   private `_create_aerospike_l2_adapter`, which the coding standard rules
>   out. It would also need the native extension, because the import comes
>   first. Ship it as a one-line `assert` → `raise` change and say so in the
>   PR.

The repo standard (`docs/coding_standards.md`) forbids `assert` for runtime
checks, because `python -O` strips it. Two upstream sites were converted to
`if ... raise ValueError`:

| Site | Commit |
| --- | --- |
| `lmcache/v1/distributed/l2_adapters/aerospike_l2_adapter.py`, `_create_aerospike_l2_adapter`: `assert isinstance(config, AerospikeL2AdapterConfig)` | `94e5df38` |
| `lmcache/v1/multiprocess/object_group_transfer.py`, `downsample_and_stage_block_ids`: `assert len(old_block_ids) % total_blocks_per_chunk == 0` (and its sibling checks) | `16d471e8` |

Both are mixed into larger commits; take only these hunks. Fold them into
BF-5 or any small PR.

---

## BF-7. Order-dependent failure in `test_lmcache_driven_layout_registry`

> **PR: PR-B6, standalone, together with BF-6's `object_group_transfer`
> half.**
>
> - **It is an upstream fix.** The test file exists upstream, unchanged since
>   `caba24c2`, with the same fixture at lines 52-73.
>   `lmcache_driven_transfer.py:48` and `object_group_transfer.py:41` bind
>   `lmcache_native` at import, so a module first imported under the stub
>   keeps it.
> - **Unverified:** whether upstream CI's test order triggers it.
> - **Take only the fixture hunk.** Exclude the `layer_indices` change in
>   `87a8925b` and the file's other fork hunks (`a7af296c`, `fa114939`,
>   `7b292537`, `ae529b58`, `b88ec0ff`, `c6980baf`).

**Symptom.** The `stub_lmcache_native` fixture replaced `lmcache.lmcache_native`
with a stub that has no `TransferDirection`. Whether a later import failed
depended on which tests ran first.

**Fix.** Install the stub only when the real extension is missing.

**Where.** `tests/v1/multiprocess/test_lmcache_driven_layout_registry.py`, the
`stub_lmcache_native` fixture. Take only that hunk. The same file also gains
large fakes for pipelined staging (`_FakeGPUContext.staging`,
`_ShiftedStagingContext`), which belong to the RDMA work.

**Commits.** `87a8925b`, mixed with an Aerospike `layer_indices` change that
upstream `dev` doesn't need.

**Size.** Under 20 lines. Ships in PR-B6.

---

## BF-8. Aerospike: concurrent stores of one sharded key leave a mixed object (D-14)

> **PR: split. The native fix ships in PR-B7, re-ported by hand.** The pipelined
> half goes in PR-A6 (the planner, [aerospike_rdma.md](aerospike_rdma.md)).
>
> **The fork's code is tangled with other items:**
>
> - `segment_range` comes from AS-3 (`a63bd1d9`); upstream slices by `seg_b`
>   offsets;
> - the `runs.empty()` term in the meta bin count comes from `a7af296c`;
> - `conn.lookup_policy` comes from AS-2; use `read_policy`.
>
> The fix itself doesn't need sharding. Write IDs, the create-only meta put,
> `remove_segments`, `remove_damaged_object`, and get/delete reading `wid`
> map onto upstream's `do_single_set` (lines 244-262, still
> `AS_POLICY_EXISTS_IGNORE` at line 161).
>
> **Not in PR-B7:**
>
> - `read_write_ids`, which needs AS-1's `kMaxBatchExistsKeys`;
> - its pybind entry. That entry's context lines (`record_node`,
>   `max_record_bytes`) are also fork-only, so PR-B7 needs no pybind change
>   at all.
>
> **None of the listed test files exist upstream:**
>
> | File | Origin | Can BF-8's cases ship with PR-B7? |
> | --- | --- | --- |
> | `test_aerospike_storage_integrity_integration.py` | AS-5 | **Yes.** The create-only conflict, missing-segment re-store and delete-named-segments cases, plus the `_segment_key` / `_set_objects` helpers, use only upstream APIs. PR-B7 creates the file with these cases; AS-5 (PR-A3) adds the rest, whichever lands first |
> | `test_aerospike_record_layouts_integration.py` | AS-3 | **No.** T-STO-06 and the write-ID test import `RecordKeys`, `ModelLayout` and `read_write_ids`. Write a new old-layout test instead (load and delete only) |
> | `test_aerospike_cluster_integration.py` | AS-5 cluster | **No.** It needs the 3-node harness. T-FLT-10 ships with PR-A4 |
>
> **Unverified:** `SHARDED_RECORDS = 5` assumes 3 MiB splits into 4 segments
> under upstream's `plan()`.
>
> **Needs** Aerospike CE, and a workflow line so CI runs the new file. Open
> it after PR-B5 to avoid a workflow-file conflict.

**Symptom.** Two engines storing the same sharded key at once could leave an
object whose meta record says "present" but whose segments come from both
writers. A reader then loads a mix. T-FLT-10 (two writer processes, 1,000
rounds) found 141 and 145 mixed objects in 1,000 at RF 1 and RF 2. A reader
during a rewrite can see the same. Ledger severity S2. The output impact is
small, because two engines' payloads for one key are equal or nearly so.
There is no cross-tenant leak.

**Cause, in upstream code.** Upstream's `do_single_set`
(`csrc/storage_backends/aerospike/connector.cpp`) writes segments under the
fixed keys `<key>|s|<i>` with `AS_POLICY_EXISTS_IGNORE`, then writes the meta
record. Nothing ties a meta record to its segment set. A create-only meta
write alone would not fix it: by then the losing writer has already
overwritten the winner's segments.

**Fix** (option 2 of the decision record):

- **Per-write keys.** Each store draws a 64-bit write ID and writes
  `<key>|s|<wid>|<i>`. The meta record, still written last, carries the ID in
  a new `wid` bin.
- **Create-only meta.** The meta record is put with `AS_POLICY_EXISTS_CREATE`.
  The first store wins. A loser deletes its own segments and reports success.
  A failed (not in-doubt) meta put also removes this store's segments.
- **Damaged holders are replaced.** If a store loses to an object that names
  a segment the cluster reports absent, it removes that object
  (generation-guarded) and retries the create once. Without this, a damaged
  object would block re-stores until its TTL. Loads that find a segment
  missing stay a miss and delete nothing.
- **Old layout stays readable.** Get and delete read `wid`. A meta record
  without one names `<key>|s|<i>`, as before.
- **Pipelined half (fork-only).** A native `read_write_ids` batch-reads the
  meta records' `state` and `wid` bins.
  - `LayerArrivalSource.read_write_ids` exposes it.
  - `build_request_fetch` reads it once per request.
  - `RecordKeys` names `<key>|s|<wid>|<i>`.
  - An object without a meta record, or a failed read, refuses the
    pipelined retrieve.
  - Measured cost: 25-130 us on 1 node and 24-358 us on 3 nodes, for 1-64
    keys.

**Where.**

- Upstream half:
  - `csrc/storage_backends/aerospike/connector.{h,cpp}`: `do_single_set`,
    get and delete;
  - `pybind.cpp`.
- Fork-only half:
  - `lmcache/v1/layerwise/contract.py`, `planner.py`, `request_fetch.py`,
    `pipelined_retrieve.py` and `fakes.py`;
  - `lmcache/v1/distributed/l2_adapters/layerwise_source.py`.
- Decision record:
  `docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md`
  (`4cf34d8e`, 296 lines). It conflicts when `prototype-stage-1a` merges.

**Commits.** `e9cd0689`. Design record `4cf34d8e`. Results are in
`functional/d14/SUMMARY.md`.

**Tests on the fork** (where they live today; see the PR line above for
what PR-B7 can take):

- `test_aerospike_storage_integrity_integration.py`:
  - the create-only conflict;
  - a missing segment, then a re-store;
  - delete removes the named segments.
- `test_aerospike_record_layouts_integration.py`:
  `test_an_object_written_before_write_ids_existed_is_still_readable`
  (T-STO-06). It drives the planner, so PR-B7 needs a new load-and-delete
  version.
- `test_aerospike_cluster_integration.py`:
  `test_two_writers_racing_on_one_{sharded,inline}_chunk_never_mix[1,2]`
  (T-FLT-10). The rerun mixed 0 of 1,000 rounds, sharded and inline, at RF 1
  and RF 2, and left 0 records behind. It ships with PR-A4.
- Pipelined half: `test_fetch_planner.py`, `test_request_fetch.py` and
  `test_aerospike_layer_arrival_source.py`. These ship with PR-A6 and PR-A10.

**Size.** +931 / -103 over 24 files on the fork. PR-B7 is roughly the native
`do_single_set`/get/delete changes, without `read_write_ids`: under +300.
Add the three storage-integrity cases with their helpers and a new
old-layout test, about +200. The size is an estimate until the re-port
exists.

**Dependencies.** None for PR-B7. Re-port it onto upstream's segment layout,
then rebase AS-3 (PR-A5) on top of it.

**Likely pushback.**

- **The record layout changes.** Mixed-version clusters matter here: an old
  reader can't find `<key>|s|<wid>|<i>` segments, so it misses (it does not
  read wrong data). Say so in the PR.
- **Losers report success while their data is discarded.** That is correct
  for a cache, but reviewers will ask.
- **Orphaned segments.** A writer that crashes between its segments and its
  meta put leaves segments that only the TTL removes. That was already true
  upstream.

---

## Regression to fix before any protocol PR (mislabeled as pre-existing)

`functional/LEDGER.md` D-05 and the Day 1 summary call these failures
"pre-existing, unrelated". Two of them are **caused by this fork**:

| Test | Real cause |
| --- | --- |
| `tests/v1/multiprocess/test_cache_server.py` (`registered_instance`, `test_register_unregister_kv_cache`) | Asserts `REGISTER_KV_CACHE` returns `None` ("Register should return None"). On upstream that is true. The fork changed the response to `RegisterKvCacheResponse` (GF-4 in [general_features.md](general_features.md)). The fork added an extra `[]` argument to the calls (removed again in `36fbc279` with the request field) but never updated the assertions. These tests need cross-process CUDA IPC, so they don't run under WSL. |
| `tests/v1/multiprocess/test_mq.py` (`test_mq_register_kv_cache`) | Same: its docstring and handler helper expect `None`. |

Fix them inside the PR that changes the protocol: PR-G5, the MP layerwise
feature PR in [general_features.md](general_features.md). Otherwise that PR
breaks upstream CI.

- `test_cache_server.py:375` still asserts `result is None`.
- `register_kv_cache_handler` in `test_mq_handler_helpers.py` is annotated
  `-> None`, so `_inspect_handler_signature` rejects the new return type.
- Upstream has rewritten `test_mq.py` since, so redo the fix on its version.
- Both tests need CUDA.

Still unverified: `tests/v1/multiprocess/test_engine_driven_transfer.py`
(ImportError of `TransferDirection`) and the `cpu_py_ops` /
`multi_layer_block_kv_transfer` case in `tests/v1/test_torch_ops.py`. Commit
`abd35b0c` says the latter fails identically on the original file. Run both
on a clean `upstream/dev` checkout before calling them pre-existing.

---

## Found but not fixed: upstream issues to report or track

These came out of functional testing. None is fixed on the fork.

| Ledger | Issue | Owner |
| --- | --- | --- |
| D-01 | After an LMCache restart, requests sent before vLLM's next heartbeat recompute (at most one interval). By design after BF-1 | MP connector |
| D-02 | gpt-oss-120b runs with KV block size 16 under the MP connector; vLLM alone picks 64 for its ROCm attention backend | MP connector |
| D-06 | For gpt-oss-120b, a cached prefix changes output even in vLLM alone (prefix cache vs none: 7/10 exact under batch invariance). LMCache matches vLLM's prefix cache exactly | vLLM |
| D-09 | The MP connector stores chunks completed by generated tokens, so short prompts near a chunk boundary write to L2. Expected behavior; affects test pass rules only | Test plan |
| D-10 | After a restart, generated-token chunks already in L2 are recomputed and written again (390 records in one warm send): stores deduplicate against L1 only. L2 write amplification, not a correctness bug | L2 store controller |
| D-13 | Chunks prefetched from L2 are evicted from L1 right after the retrieve, so a repeat request reads them from L2 again. L1 copies of later chunks then go unused, because the L1 lookup counts a leading run from chunk 0 (T-LKP-03: 1,536 tokens from L2, 0 from L1). L1 locality, not correctness | L1 / prefetch controller |
| D-16 | gpt-oss-120b is not batch invariant across batch sizes under `VLLM_BATCH_INVARIANT=1` (79/130 prompts matched at concurrency 8 vs batch size 1, no cache). Affects test pass rules only | vLLM (ROCm) |
| D-17 (S1) | After a synchronous KV-load failure under `kv_load_failure_policy=recompute`, output is wrong. The cause is [vllm#49250](https://github.com/vllm-project/vllm/issues/49250): the V2 runner doesn't rewind `num_computed_tokens` on the GPU side, and async scheduling doesn't roll back `num_output_placeholders`. Its fixes are unmerged. Late LMCache copies are ruled out. LMCache hits it because layerwise loads are synchronous. It needs an LMCache startup guard, which goes in PR-G5 | vLLM (fix); MP connector (guard) |

D-10 is a reasonable upstream issue to file: the store path could check L2
existence for chunks it didn't load. That is a design discussion, not a bug
PR. D-13 belongs in the same discussion, since both are about L1/L2 tiering
policy.

---

## Fixes to fork-only code (not upstream bugs)

These fixed code that exists only on this fork. They need no separate PR;
they ride with the feature PR that introduces the code. They are listed so
nobody files them as upstream fixes by mistake.

| Fix | Commit | Belongs to |
| --- | --- | --- |
| `lmcache.mp.use_layerwise` parsed two ways: a CLI string `"false"` turned layerwise on in the connector and off in the worker (silent KV corruption). Now one `is_layerwise_enabled` | `16d471e8` | GF-5 (PR-G5) |
| `LMCacheMPConnector.get_finished` reported recving ids in layerwise mode; vLLM asserts on that for a running request | `e34fa420` | GF-5 (PR-G5) |
| Whole-object layerwise staging reused one batch's staging for every later layer; with more than one batch per object group, cached requests produced garbage | `e34fa420` | GF-3 (PR-G4) |
| A failed retrieve could overwrite a newer retrieve's progress record or reset the watermark; now `LayerProgressRecord.fail_retrieve(generation)` | `5e3d5443` | GF-2 (PR-G3) |
| The layer-progress shared-memory segment's lifetime vs Python's resource tracker | `061fb52a`, `b795e69f` (see `track-b-acceptance.md` Step 7) | GF-2 (PR-G3) |
| The daemon's per-layer waits (pump, fallback, shared-key wait) summed past the worker's 5 s wait, so a worker timeout stopped the engine; now `layer_publish_budget_seconds` and a registration check | `b88ec0ff` | AS-M5 (PR-A11) |
| Two different rules picked "the pipelined adapter" in `StorageManager`; now `_first_ready_pipelined_adapter` | `16d471e8` | AS-P5 (PR-A10) |
| `shard_plan.cpp` compiled only into RDMA builds, so non-RDMA builds failed to link | `10c30f03` | AS-3 (PR-A5) |
| ~~`find_info_field` parsed our own echoed info command, connecting our QP to itself~~ | `03c1ad6d` | **Moot after the PR:** AS-R3 (the kv-sink control plane) is deleted |
| ~~CQ size clamped by `max_cq` instead of `max_cqe` (`ibv_create_cq` EINVAL)~~ | `03c1ad6d` | **Moot after the PR:** AS-R2 (`RdmaContext`) is deleted; the client fork creates the CQ |
| ~~kv-sink regions never deregistered; the 17th client start was refused~~ | `03c1ad6d` | **Moot after the PR:** the client fork owns regions. The region-release integration case (`c09dee44`) still checks the behavior |
| `RdmaWindowPlacer` release called `finish_write` on fetched objects, so the store controller queued every fetched object for a store back to L2 (F1) | `8e18aabd` | AS-P3 (PR-A10) |
| Connector pybind chain broken outside `#ifdef LMCACHE_AEROSPIKE_RDMA`; ~~verbs helpers in the wrong namespace~~ | `b3d67843`, ~~`a74e828c`~~ | AS-R5 (PR-A8). **After the PR** only the pybind half applies; the verbs helpers are deleted |
| "Too many slots" raised `ValueError` where `PlanTooLargeError` is documented | `93a77e56` | AS-L1 (PR-A6) |
| The C++ layout conversion parsed dtype names and sized FP8, int8 and float64 as 4 bytes, so an FP8 model's records were sized wrong and could fail the window-fit check; Python now sends `dtype.itemsize` | `7c4b2278` | AS-R5 (PR-A8) |
| `fetch_deferred_objects` logged a misleading "refused" warning when the keys did not match the model, then failed listing them again (D-03, partly) | `49971250` | AS-M3 (PR-A11) |
| D-15: client teardown did not revoke RDMA access before L1 freed the slab. **Not fixed**; test `0ebf98d0` | — | AS-R5 (PR-A8). **After the PR** the cause (`AerospikePipelinedRdmaDriver::shutdown()`) is deleted; re-run the test against the new driver. Any remaining risk is server issue 2 |

---

## PR plan

Each PR below builds, passes its own tests, and fixes a bug a user can hit
on upstream `dev`. The only prerequisite is PR-B3 before PR-B4. All seven
are independent of the feature work in the other two files, so they can be
opened now.

| PR | Title | Contents | Prerequisites | How it is tested | Size |
| --- | --- | --- | --- | --- | --- |
| PR-B1 | MP: re-register when the server lost the worker | BF-1 | none | 4 test files, pure Python | ~+400 / -36 |
| PR-B2 | L2: report why a store failed | BF-2, tests re-ported | none | `test_l2_store_failure_reason.py` (CPU + one device case); Aerospike case on CE in CI | ~+215 |
| PR-B3 | ROCm: bind the runtime PyTorch loaded | BF-3, re-ported onto `torch_ops/` | none | `test_runtime_libs.py` (fakes); MI300X evidence | ~+225 |
| PR-B4 | ROCm: event IPC with CUDA wait semantics | BF-4, moved to `platform/devices/rocm/` | PR-B3 | `test_rocm_event_ipc.py` (15 fake cases on Linux, 2 ROCm) | ~+1,290 |
| PR-B5 | Aerospike: libyaml soname and config validation | BF-5, plus BF-6's `aerospike_l2_adapter` site | none | `test_build_profiles_aerospike.py` (written), and the Aerospike CI workflow. The adapter's type check has no test: it can't be reached through the public API | ~+120 |
| PR-B6 | MP: fix stub leak in layout-registry test; validate block ids | BF-7, plus BF-6's `object_group_transfer` site | none | The fixed test file itself, plus `test_downsample_block_ids.py` (written) | ~+60 |
| PR-B7 | Aerospike: first writer wins for concurrent stores | BF-8 native half, re-ported; trimmed `aerospike_concurrent_writes.md` | none (after PR-B5 only to avoid a workflow conflict) | 3 storage-integrity cases plus a **new** old-layout case, on CE in CI | ~+500 (estimate) |

**Pieces that are not standalone PRs:**

- **BF-8's pipelined half** (`read_write_ids`, `RecordKeys(write_ids)`) ships
  in PR-A6.
- **Its cluster regression test** (T-FLT-10) ships in PR-A4.
- **The `test_cache_server` / `test_mq` regression** ships in PR-G5.
- **The fork-only fixes above** ride with the PRs that introduce their code.

Coordinate PR-B4 with the owner of upstream's CUDA timeline-semaphore
backend before opening it. "Why not generalize that backend to HIP?" is
the expected first question.
