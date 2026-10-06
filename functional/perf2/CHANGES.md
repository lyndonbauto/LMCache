# Perf rerun: changes to the box and the harness

No product-code change beyond `rdma.queue_pairs` (`81288120`, tested before the run on a
local Soft-RoCE VM). Host-level changes (the `rocm`/`caddy` stop, the out-of-tree
`rdma_rxe`, the scratch mount) are in `functional/HOST-CHANGES.md`. Every listener is on
127.0.0.1; only port 22 is public.

## Containers (new droplet)

Both created by `scripts/create_containers.sh`: host network, `/dev/infiniband/uverbs0`,
`memlock` unlimited, `CAP_IPC_LOCK`, `/root/lmc-work` bind-mounted.

| Container | Image | Added inside |
|---|---|---|
| `aero-kvsink-bp` | `ubuntu:24.04` | Server build dependencies (`scripts/build_server.sh`, apt); `python-is-python3`, because `functional/harness/kvsink_server.sh` probes the port with `python` and the server never counted as started without it |
| `lmc-c` | `vllm/vllm-openai-rocm:v0.27.1` | apt: `perftest`, `numactl`, `libuv1-dev` (for `kvlayers`). pip, all missing from the image: `cupy-rocm-7-0` 14.2.0 (`requirements/rocm_core.txt`; without it the MP server fails `register_kv_cache` and vLLM's engine times out), `sortedcontainers` 2.4.0, `aiofile` 3.12.3, `opentelemetry-exporter-prometheus` 0.61b0 (this moved `opentelemetry-sdk`/`-api` from 1.44 to 1.40; vLLM's tracing still imports, and tracing is off), Python `aerospike` 19.3.0 (the pipelined RDMA integration test's server probe), `pytest-cov`, `pytest-benchmark`. torch and vLLM unchanged (checked after each install). LMCache installed editable from `/work/LMCache` (`scripts/build_client_lmcache.sh`) |

The client and LMCache were built in `lmc-c`. `kvlayers` was built from a copy of the client
tree (`/work/kvlayers-build/client`, `make EVENT_LIB=libuv`), so the client LMCache links
against (no event library) was not touched.

## kv-sink server configs

- `functional/configs/aerospike-kvsink-bp-perf.conf.in` (unchanged): the device namespace
  of `functional/perf/` (`direct-files true`, `read-page-cache false`, `post-write-cache 0`,
  `max-write-cache 8G`), `filesize` 2.0x the KV.
- `functional/configs/aerospike-kvsink-bp-perf-mem.conf.in` (new, step 5): the same stanza
  with `storage-engine memory { data-size 16G }`.

Every session's server log, the generated config and `asd`'s disk `read_bytes` are kept in
the session directories on the box (`/root/lmc-work/functional/perf2/`).

## Harness changes (`functional/`)

| File | Change |
|---|---|
| `perf/perf.sh` | `PERF_OUT` (results directory; container path derived), `PERF_MODEL` passthrough, `CONF_TMPL` override, `STORE_IDS`; `vram()` falls back to sysfs `mem_info_vram_used`; `store_check` reads live namespace stats; new sections `qpstore:<L>`, `qplw:<L>:<qp>`, `timeline:<qp>` |
| `perf/perf_session.sh` | `PERF_MODEL`: weights to serve under the Llama name (`--served-model-name`); unused in the end (Meta's repo was available) |
| `perf/ibbw.sh` | `IBBW_OUT` output directory |
| `perf/aggregate.py` | Session names with a `_qp<n>` suffix become mode `lw_qp<n>` |
| `perf2/scripts/` | `create_containers.sh`, `build_server.sh`, `build_client_lmcache.sh`, `gate.sh` (step 3), `qpscan.sh` (step 4), `memscan.sh` (step 5), `sweep.sh` (step 6) |

The E3 print patch to `lmcache/v1/layerwise/pump.py` is box-only
(`/root/lmc-work/functional/perf2/e3_pump_patch.py`), applied and reverted by `qpscan.sh` and
`memscan.sh` around the timeline sessions (19:43-19:52Z and 20:18-20:22Z); `lmcache/` and
`csrc/` were clean after each. Never committed.

## Follow-up A and D (2026-10-05 22:04-22:47Z, `FOLLOWUP-A-D.md`)

| What | Change |
|---|---|
| Product code | `2eefa049`: the layer wait in `lmcache/v1/multiprocess/layer_progress.py` restarts its deadline on progress (A). On the box the file was copied over the tree before `scripts/fixa.sh` |
| `harness/kvsink_server.sh` | `KVSINK_EXTRA_ENV`: more `NAME=value` pairs for the server's environment |
| `perf/perf.sh` | `PERF_ASD` (another server binary) and `PERF_ASD_ENV` (its environment) |
| `perf2/scripts/` | `fixa.sh` (A rerun), `trace.sh` (D runs; builds the traced server), `trace_analyze.py` (trace summary) |
| Server (box-only) | Trace patch `/root/lmc-work/functional/perf2/trace_patch.py`, not committed (it quotes the private server source): per-op timestamps, `KV_SINK_TRACE`, `KV_SINK_REGION_INFLIGHT_KB`, `KV_SINK_GLOBAL_INFLIGHT_KB`. Traced binary `/root/lmc-work/asd-trace/asd` (md5 `172df35939cc7aa17bb86797241a86e3`); the source tree and the original binary were restored (`functional/HOST-CHANGES.md`) |
| Server (box-only) | Breakdown (`BREAKDOWN.md`): asd `314564cfb` (`sriram/kv-sink-batch-prio`) built in place from a tar of its four changed files (`/root/lmc-work/asd-314564cfb/src-314564cfb.tar`, not committed), binary `/root/lmc-work/asd-314564cfb/asd` (md5 `c934cad4f806ed88812b877162af6a67`); the `9c16972132` files and binary were restored (`functional/HOST-CHANGES.md`). Harness: `perf.sh` `PERF_ASD`/`PERF_ASD_ENV`, `kvsink_server.sh` `KVSINK_EXTRA_ENV`, `ibbw.sh` `IBBW_SET=sink_depths` |
| Harness (day 2) | `perf.sh`: `POINT_N` (prompts per qpstore/qplw point), `STORE_COUNT` and `CACHED2_LWWAIT` (cached2), `EXP_PART_POINTS` and `EXP_STORE_LENS` (exp2 partial hits). `breakdown.sh`: `BD_OUT`, steps `devp16/32/64`. New: `scripts/breakdown2.sh`, `scripts/breakdown2_report.py`, `scripts/lwaon.sh`, `scripts/lwaon_ctl.sh` and `scripts/lwaon_ctl2.sh` (controls), `scripts/lwaon_report.py`, `scripts/breakdown3.sh` and `scripts/breakdown3_report.py` (round 2, server `0c9703931`), `scripts/breakdown4.sh` and `scripts/breakdown4_report.py` (round 3, rxe task types, workqueue and sched latency), `scripts/breakdown5.sh` and `scripts/breakdown5_report.py` (round 4, Soft-RoCE counters and `rxe_requester` exit reasons) |
| Server and client (box-only, round 2) | asd `0c9703931` built in place from a tar of its four kv-sink files (`/root/lmc-work/asd-0c9703931/src-0c9703931.tar`, not committed), binary `/root/lmc-work/asd-0c9703931/asd` (md5 `73b677809610fef94ac32247f6b379e7`). `kvlayers` relinked against client `e8158149` (`as_sink_verbs.c` from a tar, not committed) in `/root/lmc-work/kvlayers-e8158149/client`. LMCache's client library stays on `5a24afdb`: LMCache allows at most 16 queue pairs |
| LMCache tree on the box | Before the breakdown, `/root/lmc-work/LMCache` had uncommitted copies of committed files (CRLF-only differences). Backed up to `/root/lmc-work/lmcache-tree-backup-20261005T225556Z/`, then reset to `b947831d` |

## Aborted attempts (kept on the box, not used)

- `gate_attempt1/`, `gate_attempt2/`: the gate before the container packages above were
  added (no `sortedcontainers`, no Python `aerospike`, no `cupy`, no `python` in the server
  container, `kvlayers` linked without libuv).
- `gate_attempt3/`: with `RUN_AEROSPIKE_SLOW_INTEGRATION=1`; the region-release test ran
  873 adapter lifetimes in 15 min without finishing and hit the gate's timeout. The VM check
  and the old d14 runs did not set it either; the gate ran without it.
- The first queue-pair scan start (19:23Z) used a 45G file (`FS_PCT` 140, about 71% full
  after the store, over `stop-writes-used-pct` 70); stopped during the store and restarted
  with `FS_PCT=200`.
- `trace/build_copy_attempt.txt`: the first traced-server build, from a copy of the server
  tree; failed on CMake caches that name the original tree.
