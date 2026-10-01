# kv-sink warm-up crash: findings (2026-10-01)

**Verdict: (a), an LMCache client bug, severity S2 (teardown only). Confidence: high.**
The crash in the warm-up smoke is how that bug shows up in the test configuration.
It cannot corrupt memory in a running LMCache server.

## What happens

1. On a cold kv-sink server, the first `kv-sink-fetch-pipelined` registers 8
   stripes of 2 GiB inside the command, which takes 2 to 4 s (server issue 5).
   The client's info call gives up after 1 s and the retrieve falls back. The
   window is quarantined, as designed.
2. The test fails at once, and the fixture calls `StorageManager.close()`:
   - `AerospikeNativeConnector::close()` calls
     `AerospikePipelinedRdmaDriver::shutdown()`. That sends
     `kv-sink-deregister`, drops the pool and marks the driver closed.
   - It does **not** deregister the window memory region or stop the queue
     pairs: `RdmaContext` (MR, QPs and CQ) lives until the C++ object is
     destroyed.
   - `L1Manager.close()` then frees the slab.
3. The server's `kv-sink-deregister` only sets `teardown_pending` on the
   region, which is reference-counted, and replies. The abandoned fetch still
   holds its reference. When its stripe registration finishes, it posts the
   writes, and they land through the client's MR that is still live.
4. In the test, L1 is a 4 MiB slab with `shm_name=""` on `CpuDeviceOps`.
   That is `torch.empty`, so the slab comes from the glibc main heap
   (`[heap]` in `/proc/self/maps`). After `free()`, glibc hands the same pages
   to new allocations, and the late RDMA writes then overwrite live heap
   chunks. The result is the segfault in `malloc`, or an abort such as
   `corrupted size vs. prev_size` (this one came from `torch.empty`, which is
   what `alloc_pinned_ptr` calls).

## Evidence (box: `/root/lmc-work/functional/stage3/crash/`)

| Experiment | Result |
|---|---|
| `late_write_probe.py hold`, cold server: keep the slab alive after `close()`, fill the window with a sentinel, watch it | **4/4** runs: 16–28 KB of the *stored payload* written into the window 0.05–0.5 s **after** `close()` returned (`probe1.txt`, `probe_cold_*.txt`) |
| The same, on a warm server (the fetch pipelines) | 0 bytes after `close()` (`probe_warm.txt`) |
| `late_write_probe.py reuse`: after `close()`, `malloc(4000)` ×2048 filled with 0x5A, check after 5 s | 16 blocks land in the freed window in every run. 2 of 3 runs had 12–13 live blocks overwritten, and **1 aborted: `corrupted size vs. prev_size`, exit 134** (`probe_reuse_*.txt`) |
| `reuse` with `GLIBC_TUNABLES=glibc.malloc.mmap_threshold=65536` (slab in its own mmap) | 0 blocks reused, 0 overwritten (`probe_reuse_mmapthr.txt`) |
| One long-lived manager after a cold fallback | Late writes (21 KB, the last 1.45 s after the fallback) stay in the quarantined window. Nothing else touched (`probe_longlived.txt`) |
| pytest crash loop, cold server each time: 15 runs of the two tests, 10 of the full file, 3 of the full file with mmap_threshold | 0 crashes in 28. Test 1 fell back in every run (`loop_*/summary.txt`). The next 4 MiB slab usually reuses the freed chunk, so the late writes hit the 2nd manager's window instead of heap metadata. The crash needs small allocations carved from that chunk first; the smoke saw it in 2 of 7 runs |
| New test `test_no_server_write_reaches_l1_after_close` (L1 in a named shm segment the test also maps) | **Fails 7 of 9 on a cold server** (2–28 KB written after `close()`). It passes when the writes beat `close()`, and skips on a warm server |

The native stack in `smoke_crash/pipelined_it_segv.txt` shows `malloc+0xa4`,
called from logging, while the 2nd `StorageManager` is built.
`core_pattern` pipes to the host's apport. It was not changed, and gdb was not
needed.

## Why it is S2, not S1

- A real server frees L1 only in `StorageManager.close()` at shutdown
  (`engine_context.close`). There is no runtime path that removes an L2
  adapter.
- The production L1 (`--l1-size-gb 40`, default `shm_name=lmcache_l1_pool_<pid>`)
  is a shared-memory segment or an mmap; glibc always mmaps anything over
  32 MiB.
- Once that memory is unmapped, the late writes go to pages the MR still pins
  and nobody maps, so nothing is corrupted. A crash needs a heap-backed slab
  that the same process reuses, which is the pytest configuration.
- While the manager lives, quarantine contains the late writes, as designed.

The bug is still a real client contract violation: memory is freed while the
remote still holds an rkey for it. It becomes S1 if L1 memory is ever recycled
in-process, for example by a rebuilt StorageManager or a pinned pool that
reuses pages.

## Not (b), and not D-12

The server writes only inside the registered window, at the planned offsets,
with the stored bytes. That is in contract. Its contribution is a contract
gap: `kv-sink-deregister` replies without draining the region's in-flight
writes. Proposed new server issue 12, related to issues 4 and 5. The client
cannot rely on the server for this anyway; under verbs rules, the owner of the
memory revokes access before freeing it.

D-12 is the server freeing its *own* tokens on multi-command fetches (over 256
slots). This case is one command of 7 chunks, and the corrupted memory is on
the client side.

## Proposed fix (client, not applied)

1. `csrc/storage_backends/aerospike/rdma_context.{h,cpp}`: add
   `RdmaContext::revoke_remote_access()`. It moves every peer QP to
   `IBV_QPS_ERR` and `ibv_dereg_mr`s the window MR(s) in `impl_->mrs`. It
   keeps the PD, CQ and notification-scratch MR, so concurrent pollers stay
   valid, and leaves `registered_` set so re-registration is still refused.
   After `ibv_dereg_mr` returns, the device does not touch the range again,
   and any late write is NAKed with a remote-access error.
2. `RdmaContext::poll_notifications`: treat `IBV_WC_WR_FLUSH_ERR`
   completions as no event and do not repost them, instead of throwing.
   The flushes follow the QP's move to ERR.
3. `connector_pipelined_rdma.cpp`, `AerospikePipelinedRdmaDriver::shutdown()`:
   call `context_->revoke_remote_access()` after `deregister_all_nodes`,
   before `pool_.reset()`, under `mu_`.
4. Contract: the `L2AdapterInterface.close()` docstring should say "after
   return no remote peer can write into L1". `StorageManager.close()` already
   closes adapters before L1, which is the right order.

Rejected alternative: make `close()` wait out the quarantine
(`fetch_timeout_seconds`, 30 s). It slows shutdown and still depends on the
server's timing.

## Test

`tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py::test_no_server_write_reaches_l1_after_close`
(test-only). Run it first after `kvsink_server.sh start`. Expected after the
fix: it passes on a cold server in every run.

## Harness note

The smoke's warm-up retry (`kvsink_restart`) is still a valid mitigation.
Warming up in a separate process first avoids the crash entirely.
