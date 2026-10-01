# New kv-sink stack: integration and CPU verification on Soft-RoCE

**Outcome: pass.** `prototype-stage-1a` (`934052cf`, the kv-sink batch-read
fetch) is merged into `prototype-stage1` with D-14 intact, and the merged
tree passes every CPU suite against the new server `046e8558d` and client
`523d51ea` over RC on `rxe0`, with no failures. Worker `cpu-newstack`,
2026-10-01 22:14-23:10 UTC, containers `lmc-newstack` and `aero-kvsink-bp`
(no GPU). Versions in [`VERSIONS.md`](VERSIONS.md), host changes in
[`CHANGES.md`](CHANGES.md), scripts in [`scripts/`](scripts/).

## Merge

`git merge --no-ff origin/prototype-stage-1a` onto `origin/prototype-stage1`
(`d10a376f`). Two textual conflicts:

| File | Resolution |
| --- | --- |
| `docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md` | Cost table: kept D-14's implemented lost-race row and "+1 batch read" for the pipelined fetch; took 1a's "whole-object `kv-sink-fetch` removed". Pipelined section rewritten for the sink-fetch driver (slot record keys are the batch-read row keys) |
| `tests/v1/distributed/rdma/csrc/fabric_free_session_pybind.cpp` | Includes: kept `<map>` (D-14's `read_write_ids` on the fabric-free connector), dropped `<memory>` (1a removed its only use) |

**D-14 on the new path needed no code change.** D-14's pipelined half lives
in Python: `build_request_fetch` reads the write IDs once
(`read_write_ids`, one batch read of the meta records) and `RecordKeys`
names `<key>|s|<wid>|<i>` in every plan slot. 1a kept the planner and
`issue_pipelined_fetch_by_slots`; its `AerospikeSinkFetchDriver::read_rows`
sets each row's key to `slot.record_key` verbatim. So the per-layer batch
reads address the winning write's segments, and a segment that is gone
(winner replaced between the write-ID read and the fetch) answers not-found,
fails its slot and the retrieve loads whole objects. Covered by
`test_aerospike_pipelined_rdma_integration.py` (clean fetch lands every byte
`PIPELINED` from write-ID objects; deleting `<key>|s|<wid>|0` falls back) and
D-14's planner/request-fetch unit tests. A follow-up docs commit fixes
`aerospike_rdma.md`'s key examples and a stale info-command sentence in
`system-design.md` section 11.

## Test results (lmc-newstack, CPU only, server 127.0.0.1:3700)

| Run | Scope | Result |
| --- | --- | --- |
| `units` | D-14's unit set: `tests/v1/layerwise`, `multiprocess`, `distributed` (no servers), the MP adapter and re-registration tests | **2,398 passed, 182 skipped, 0 failed** (D-14 on the old stack: 2,402 / 181 / 0) |
| `it1` | `tests/v1/layerwise`, `tests/v1/distributed/rdma`, every `test_aerospike_*.py`, integration on | **478 passed, 14 skipped, 0 failed** |
| `wide1` | `tests/v1/layerwise` + all of `tests/v1/distributed`, integration on | **1,447 passed, 92 skipped, 0 failed** |
| `pipe1` | `test_aerospike_pipelined_rdma_integration.py` on a just-started server | 3 passed, 2 skipped; no warm-up needed |
| `slow1` | `test_closing_releases_the_servers_region` (`RUN_AEROSPIKE_SLOW_INTEGRATION=1`, server max regions + 1 lifetimes) | see the result line below |

Upstream reported 620 passed / 12 skipped on Soft-RoCE RC without naming
the file set, so the counts are not directly comparable; there are no
failures in any scope here. The `it1` skips: 10 cluster tests (no 3-node
cluster; not started, to stay off the old stack's containers), the
late-write-after-close test (only runs when a fetch falls back), the slow
region test (run separately), the vLLM-stored byte oracle (GPU half), and the
namespace-default TTL test (`default-ttl 0`, kept for Stage 3 parity).

Highlights:

- **Pipelined RDMA (A8):** 3/3; works on a cold server (old issue 5, lazy
  stripe registration, is gone: values go through a staging buffer
  registered at start-up).
- **Byte oracle, CPU half (T-RDMA-06):** both synthetic tests pass, including
  `test_pipelined_rdma_fetches_equal_plain_gets_for_100_p_exact_keys`, which
  asserts `PIPELINED` for whole prompts up to 64 Llama chunks. On the old
  server fetches over 4 chunks fell back (D-12); **D-12 does not reproduce**
  on the batch-read protocol (CPU half; the GPU half is for Stage 3).
- **D-14 storage ITs:** all pass on the new server: create-only conflict
  (sharded and inline), missing segment then re-store, delete removes named
  segments, old-layout objects readable, write-ID naming, writer killed
  mid-store, eviction.
- Server log: registrations/deregistrations only, no `write failed`, `post
  failed` or `dropped after a failed write` lines.

## Harness for the new stack

| Piece | State |
| --- | --- |
| `configs/aerospike-kvsink-bp.conf` | New: ports 3700-3703, memory namespace `lmcache` (8 GiB, `default-ttl 0`) plus `lmcache_evict`; device and GID from `KV_SINK_RDMA_DEVICE` / `KV_SINK_GID_INDEX` |
| `newstack/kvsink_bp_env.sh` | New: source on the host to select the new server for every script below (`KVSINK_CTR=aero-kvsink-bp`, binary, config, `KVSINK_PORT=3700`, `KVSINK_WARM=0`, `RXE_PKT=4096`) |
| `harness/kvsink_server.sh` | Updated: container, binary, data dir, port and the two `KV_SINK_*` variables come from the environment; refuses to start if the port is taken. Defaults are the old server's |
| `harness/host_actions.sh` | Updated: `KVSINK_CTR` and `KVSINK_CONF` from the environment; `KVSINK_WARM=0` skips the warm-up smoke |
| `stage3/stage3.sh` | Updated: `KVSINK_PORT` (adapter `hosts`, precheck, `L2_PORT`, `l2_segments.py`, the byte oracle) and `RXE_PKT` overridable; the per-session kv-sink line also counts the new server's failed writes and dropped regions |
| `harness/l2_segments.py` | No change: already names per-write keys; takes `--port` |
| `harness/kvsink_smoke.sh`, `kvsink_warm.py`, `kvsink_cluster.sh`, `configs/cluster/aerospike-kvsink-node.conf.template` | **Old stack only, not updated.** They drive the server's `kvsink_client` example (`kv-sink-fetch` info command) and the old binary. Not needed for Stage 3 on the new stack (no warm-up); a multi-node kv-sink cluster on the new server needs a new script |
| `stage3/dryrun_server.sh` (`stage3.sh dry`) | **Not updated:** hard-codes 3100 and `aero-kvsink` |
| `--pipelined-max-chunks` | **Unchanged** in 1a: it still sizes one RDMA window (`window_bytes` must hold the cap). Nothing replaces it |

## Switching lmc-c (GPU worker)

1. Stop Stage 3 sessions; confirm VRAM is back to about 285 MB.
2. Update the tree (from wherever that tree is normally updated):
   `git -C /root/lmc-work/LMCache pull --ff-only origin prototype-stage1`.
3. Rebuild in `lmc-c` against the prebuilt client (no client build or apt
   needed in `lmc-c`):

   ```bash
   docker exec -w /work/LMCache lmc-c bash -c '
     source /work/deps/aerospike-kvsink-install-523d51ea/env.sh
     export BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 MAX_JOBS=8
     export BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1
     nice -n 19 pip install -e . --no-build-isolation --no-deps --ignore-requires-python
     ldd lmcache/lmcache_aerospike*.so | grep -E "aerospike|verbs"   # must name aerospike-kvsink-install-523d51ea
     objdump -T /work/deps/aerospike-kvsink-install-523d51ea/lib/libaerospike.so | grep "IBVERBS_1.1.*ibv_reg_mr"'
   ```

   No `BUILD_WITH_AEROSPIKE_EFA`. After this, `lmc-c`'s pipelined path
   cannot use the old server on 3100 (1a removed the info-command client).
   Back out: rebuild with `/work/deps/aerospike-install/usr/{include,lib}`
   from a pre-merge tree.
4. Server and harness, on the host:

   ```bash
   cd /root/lmc-work/LMCache
   CLONE=/root/lmc-work/LMCache . functional/newstack/kvsink_bp_env.sh
   bash functional/stage3/stage3.sh precheck cfg08 e2e04 e2e05 pipe05 pipe06 pipe12 pipe11 rdma06gpu idle
   ```

   `stage3.sh`'s `group` restarts the server in `aero-kvsink-bp` on
   127.0.0.1:3700 before each group (no warm-up). Adapter spec becomes
   `"hosts":"127.0.0.1:3700"`; RDMA settings are unchanged (`RC`, `rxe0`,
   `gid_index 1`). Standalone start: `functional/harness/kvsink_server.sh
   start` with the same env. Stop `cpu-newstack`'s instance first if it is
   still running (`kvsink_server.sh stop`, same env).
5. `CAPS` can stay `"64 4"`: the cap-4 workaround for D-12 is no longer
   needed, and cap 64 is now the check that D-12 is gone on the GPU path.

## Stage 3 sections on the new protocol

| Section | Still meaningful | Notes |
| --- | --- | --- |
| cfg08 (T-CFG-08) | Yes | Registration line unchanged; packet count uses `RXE_PKT=4096` |
| e2e04 / e2e05 | Yes | Expect `pipelined` on every eligible request at caps 4 and 64 (D-12 retest) |
| pipe05 (T-PIPE-05) | Yes | Missing segment: not-found row, `fell_back`. Recompute half still blocked by D-17 (vLLM bug), so keep `FAULT_POLICY=fail` |
| pipe06 (T-PIPE-06, kv-sink frozen 2.5 s) | Yes | Now exercises the batch timeout and the server's per-write deadline instead of info-command timeouts; recompute half D-17 |
| T-PIPE-07 (late write after re-lease) | Yes, more important | Writes from an abandoned fetch can land until the fetch timeout; LMCache quarantines the window until then and the server drops queued writes at the deadline |
| pipe12 (T-PIPE-12) | Yes | Unchanged; the server now advertises a 2 MiB sink limit, the config caps records at 1 MiB |
| pipe11 (T-PIPE-11 packet counts) | Yes, with `RXE_PKT=4096` | One RDMA write per row; data packets = bytes / 4096 |
| rdma06gpu (T-RDMA-06 GPU half) | Yes | Uses `KVSINK_PORT` |
| Old kv-sink issue tests (region leak after `kill -9`, warm-up) | Changed | Regions of dead clients are reclaimed after `KV_SINK_IDLE_SEC` (600 s); restarting the server per group remains the simplest isolation |

## Open items and notes

- New server issue 9 (the poller never sleeps): the running `asd` in
  `aero-kvsink-bp` keeps one core busy while idle.
- A multi-node kv-sink cluster on the new server was not built or tested.
- Not run: anything on the GPU, the 3-node cluster ITs, plain-path tests
  against Aerospike CE 8.2 with the new client (CE is the old stack's).
