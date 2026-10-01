# Functional test ledger

One row for every test ID in section 5 of
[`functional-test-plan.md`](../docs/design/v1/layerwise/functional-test-plan.md).
Update the row whenever a test runs, and keep the plan's pass rules
(section 1) and oracle (section 2): token equality at temperature 0 with
`VLLM_BATCH_INVARIANT=1` on both the baseline and the LMCache run; for
gpt-oss-120b prefix hits, compare against vLLM's own prefix cache at block
size 16.

## Statuses

| Status | Meaning |
| --- | --- |
| pass | Ran in the environment named in "Env run" and met the plan's whole pass criterion |
| partial | Part of the criterion is shown (one prompt set, the E0 half, one mode); the note says what is missing |
| fail | Ran and missed the pass criterion; the Defects table has the entry |
| not run | Runnable on this box, not run yet |
| blocked | Cannot run until the named dependency exists |
| N/A on this box | Needs hardware this box does not have (one MI300X, Soft-RoCE only) |

## Where evidence lives

Every log, script and raw JSON is on the box under
`/root/lmc-work/functional/<stage>/` (symlinked from
`/root/RESULTS-functional`). "Evidence path" is relative to that folder:
`day1/` is Day 1, `day1fix/` is the five fixes after Day 1. Each stage
folder has `SUMMARY.md`, `VERSIONS.md` and `CHANGES.md`; the summaries are
committed under `functional/<stage>/`.

"Env run" names the plan's environment as reproduced on the box: E0 is the
unit suites in `lmc-c`, E1 adds Soft-RoCE (`rxe0` on `lo`, GID index 1), E2
adds Aerospike CE 8.2 on `127.0.0.1:3000`, E3 adds vLLM 0.27.1 on the
MI300X. Because the box is ROCm, every E3 run is also E6-equivalent on
CDNA 3. "Commit" is the LMCache commit the run used: `a3456e2c` is the Day 1
native build, `16d471e8` the Day 1 product code, `bb833c9c` the RDMA test
build fix, and `d41162b4` the five fixes after Day 1 (the tests ran on the
working tree just before those commits).

The pipelined RDMA path needs the Aerospike `kv-sink` server build
(`kv-sink-fetch-pipelined`, per-piece write-with-immediate, no fence, RC on
Soft-RoCE). Rows marked **blocked (kv-sink)** wait for it; Aerospike CE has
no `kv-sink` commands.

## Ledger

### 5.1 Build and configuration

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-CFG-01 | Build without RDMA flags; adapter works with no libibverbs | P0 | E0 | pass | E0 | 2026-09-30 | `day1/step1/build_A_no_rdma.log`, `day1/step1/check_A.txt` | a3456e2c | Builds in 67 s; `lmcache_aerospike` does not link `libibverbs` and imports |
| T-CFG-02 | Build with `BUILD_WITH_AEROSPIKE_RDMA=1` (and EFA) | P0 | E0 | pass | E0 | 2026-09-30 | `day1/step1/build_B_rdma.log`, `day1/step1/check_B.txt` | a3456e2c | Verbs build links `libibverbs`, imports, RDMA symbols present. The EFA half needs `libefa`: N/A on this box |
| T-CFG-03 | Fetch timeout at or past the L1 write TTL refused | P0 | E0 | pass | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_rdma_registration.py::test_timeout_at_or_beyond_the_ttl_is_rejected`) | d41162b4 | Config-level check; startup message not checked end to end |
| T-CFG-04 | RDMA with a growable L1 slab raises `ValueError` naming the hazard | P0 | E0 | pass | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_rdma_registration.py::test_growable_slab_is_rejected_naming_the_hazard`) | d41162b4 | |
| T-CFG-05 | Window too small for `--pipelined-max-chunks`: warn, not pipelined | P1 | E0 | partial | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_pipelined_placer_access.py::test_a_window_too_small_for_the_chunk_cap_is_refused`) | d41162b4 | Placer refuses; the "Extend" part (registration warns, model not pipelined) is not written |
| T-CFG-06 | Wrong GID index fails at startup naming the GID | P1 | E1 | not run | | | | | New test |
| T-CFG-07 | Slab over `RLIMIT_MEMLOCK` fails naming memlock | P1 | E1 | not run | | | | | New test |
| T-CFG-08 | Registration logs `<model> fetches layer by layer from L2 adapter 0...` | P0 | E3 | blocked (kv-sink) | | | | | Needs RDMA windows on the Aerospike adapter. With CE, `day1fix/e2e_run.log` shows staging matching the plan for 32 layers, then `Cannot fetch ... layer by layer` (expected without RDMA) |

### 5.2 Storage: the plain (non-RDMA) path

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-STO-01 | Put, exists, get, delete round trip, single and sharded | P0 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` | a3456e2c | 14/14 integration tests; rerun 29/29 in `day1fix/batch1b/out.txt` |
| T-STO-02 | Plane-aligned sharding: no record straddles two layers | P0 | E0 | pass | E1 | 2026-09-30 | `day1/step2/rdma_suite.txt` (`test_shard_plan.py`, `test_slot_plan_parity.py`) | bb833c9c | Also in `day1fix/batch1/junit.xml` |
| T-STO-03 | Writer killed before metadata: nothing reported present | P0 | E2 | not run | | | | | New test; same as T-FLT-09 |
| T-STO-04 | Missing segment under intact metadata reads as a miss | P0 | E2 | not run | | | | | New test |
| T-STO-05 | Corrupt metadata fails as corrupt, never as a short read | P1 | E2 | partial | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` (`test_a_corrupt_runs_bin_fails_the_read`) | a3456e2c | Bad runs string covered; wrong total size not yet (Extend) |
| T-STO-06 | Records written before plane-aligned sharding still readable | P1 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` (`test_aerospike_record_layouts_integration.py`) | a3456e2c | |
| T-STO-07 | Record TTL equals `default_ttl_seconds` | P1 | E2 | not run | | | | | New test |
| T-STO-08 | Replication factor 2, commit level all | P1 | E4 | not run | | | | | Needs the 3-node local cluster |

### 5.3 Lookup and keys

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-LKP-01 | Batch exists over mixed hits and misses, in order | P0 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` | a3456e2c | |
| T-LKP-02 | Batch exists uses the Aerospike batch call (10,000 keys) | P0 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` | a3456e2c | Includes the 10,000-key batch exists |
| T-LKP-03 | Prefix semantics: only the leading run of hits is used | P0 | E2 | not run | | | | | Extend |
| T-LKP-04 | Tenant isolation: salt A never hits salt B (P-salt) | P0 | E3 | partial | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_cache_salt_l2_eviction.py`, 6 passed) | d41162b4 | Eviction half only; end-to-end P-salt not run |
| T-LKP-05 | No hit across models, or across TP=1 and TP=2 | P0 | E3 | not run | | | | | Model half runnable here. TP half N/A on this box (one GPU): unit level only, `test_object_key_parallel.py` passed (14) in `day1fix/batch1/junit.xml`; full TP half on an 8-GPU droplet |
| T-LKP-06 | Keys differing only in `object_group_id` stored separately | P1 | E0 | not run | | | | | Extend; no test for this exact case confirmed in the logs |

### 5.4 RDMA data path (whole object)

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-RDMA-01 | RDMA write lands byte-identical to the source | P0 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_suite.txt`, `day1/step2/rdma_harness_direct.txt` | bb833c9c | Against the mock writer |
| T-RDMA-02 | Nothing lands outside the requested offsets | P0 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_harness_direct.txt` | bb833c9c | |
| T-RDMA-03 | Write past the registered window refused | P0 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_harness_direct.txt` | bb833c9c | |
| T-RDMA-04 | Region handle held per node | P0 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_harness_direct.txt` | bb833c9c | |
| T-RDMA-05 | Per-node `kv-sink-register` fanout on a real cluster | P0 | E4 | blocked (kv-sink) | | | | | Also needs the 3-node local cluster |
| T-RDMA-06 | Byte oracle: 100 P-exact keys, RDMA fetch vs plain get | P0 | E2 | blocked (kv-sink) | | | | | New test |

### 5.5 Pipelined retrieve

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-PIPE-01 | A layer is ready only when every piece has landed | P0 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_suite.txt` (`test_rdma_pipeline.py`) | bb833c9c | Against the mock writer |
| T-PIPE-02 | Unsent layers' regions untouched while earlier ones are consumed | P0 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_suite.txt` (`test_rdma_pipeline.py`) | bb833c9c | |
| T-PIPE-03 | Out-of-order arrival still handed to the GPU in order | P0 | E0 | pass | E0 | 2026-09-30 | `day1/step1/suite_2_layerwise.txt` (`test_layer_arrival_pump.py`) | a3456e2c | Also `day1fix/batch1/junit.xml` |
| T-PIPE-04 | Stale generation, unknown slot, duplicate immediate distinguished | P0 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_suite.txt` (`test_rdma_pipeline.py`, `rdma_pipeline_test.cpp`) | bb833c9c | |
| T-PIPE-05 | Server declines one slot: `fell_back`, output correct | P0 | E3 | partial | E1 | 2026-09-30 | `day1/step2/rdma_suite.txt` (`test_pipelined_fetch.py`), `day1fix/batch1/junit.xml` (`test_pipelined_retrieve.py`) | bb833c9c | E0/E1 half passes. End to end blocked (kv-sink) |
| T-PIPE-06 | Layer never arrives: deadline, quarantine, output correct | P0 | E3 | partial | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_pipelined_retrieve.py`, `test_rdma_window_leaser.py`), `day1fix/` fix-1 three-track run | d41162b4 | E0 half passes. End to end blocked (kv-sink) |
| T-PIPE-07 | Late write from an abandoned fetch not credited to the next | P0 | E1 | partial | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_suite.txt` (stale generation), `day1fix/batch1/junit.xml` (`test_rdma_window_leaser.py` quarantine) | bb833c9c | E1 half passes. E3 extension blocked (kv-sink) |
| T-PIPE-08 | More concurrent retrieves than `window_count`: extras `refused` | P0 | E3 | blocked (kv-sink) | | | | | New test |
| T-PIPE-09 | Plan over the slot cap or chunk cap refused before leasing | P0 | E0 | pass | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_layer_arrival_pump.py::test_pump_passes_a_too_large_refusal_through_before_touching_the_sink`, `test_aerospike_layer_arrival_source.py::test_the_native_issuer_refuses_an_oversized_plan_before_sending`) | d41162b4 | Includes the `PlanTooLargeError` fix (`93a77e56`) |
| T-PIPE-10 | Two same-prefix requests at once, each shared-keys mode | P0 | E3 | partial | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_shared_keys.py`, 7 passed) | d41162b4 | E0 half passes. End to end blocked (kv-sink) |
| T-PIPE-11 | Hybrid model: sliding-window layers fetch only their window | P0 | E3 | partial | E0 + E3 staging | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_fetch_planner.py`), `day1fix/e2e/lmcache_gptoss_staging.log` | d41162b4 | E0 half passes; gpt-oss staging matches the plan for 36 layers. Pipelined end to end blocked (kv-sink) |
| T-PIPE-12 | Records under one `max_record_bytes`, read under another | P1 | E3 | blocked (kv-sink) | | | | | New test |

### 5.6 End to end with vLLM

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-E2E-01 | P-short: no L2 traffic, output equal | P0 | E3 | partial | E3 (L1 only) | 2026-09-30 | `day1/step4/compare_bi_lmcache_lw_*_warm.json` | 16d471e8 | Output 20/20 equal, layerwise off and on. "No L2 traffic" not checked (that run had no L2) |
| T-E2E-02 | P-exact and P-ragged, second send hits L1, output equal | P0 | E3 | partial | E3 (L2 = CE) | 2026-09-30 | `day1/step6_bi/compare_e2e02_*`, `day1/step4/compare_bi_lmcache_lw_*_warm.json`, `day1fix/e2e/` | 16d471e8 | P-exact 20/20, layerwise off and on, `not_deferred`. P-ragged warm send 20/20 equal in the step-4 all-corpus run, but per-request L1 hits were not broken out; rerun P-ragged with hit counts |
| T-E2E-03 | Same, LMCache restarted so the hit is L2, pipelined off | P0 | E3 | partial | E3 (L2 = CE) | 2026-09-30 | `day1/step6_bi/compare_e2e03_*`, `day1fix/e2e/compare_llama_restart*` | 16d471e8 | P-exact 20/20 after restart (23,110 Aerospike reads), `not_deferred`. P-ragged not run |
| T-E2E-04 | Same, pipelined on: `pipelined` on every eligible request | P0 | E3 | blocked (kv-sink) | | | | | |
| T-E2E-05 | P-long at and one chunk over the cap | P0 | E3 | blocked (kv-sink) | | | | | Outcome at the cap is `pipelined` |
| T-E2E-06 | P-shared: later requests hit the shared prefix | P0 | E3 | partial | E3 (L1 only) | 2026-09-30 | `day1/step4/compare_bi_lmcache_lw_*_warm.json` | 16d471e8 | Llama output 10/10 equal; prefix hits per request not checked; L2 and pipelined not run |
| T-E2E-07 | P-multi: each turn hits the previous turns' chunks | P0 | E3 | partial | E3 (L1 only) | 2026-09-30 | `day1/step4/compare_bi_lmcache_lw_*_warm.json` | 16d471e8 | Llama output 50/50 equal; per-turn hits not checked; L2 and pipelined not run |
| T-E2E-08 | Hybrid model (gpt-oss-120b) through T-E2E-02 to 07 | P0 | E3 | partial | E3 | 2026-09-30 | `day1/step5_bi/`, `day1fix/e2e/compare_gptoss_staging_*` | 16d471e8 | P-exact 20/20 equal (cold, warm, layerwise off and on); P-shared 10/10 equal to vLLM's own prefix cache. P-ragged, P-multi, L2 restart and pipelined not run. Block size 16 under the connector (S3, see Defects) |
| T-E2E-09 | 16 concurrent clients, byte oracle and logprob agreement | P0 | E3 | not run | | | | | Batch invariance allows token equality here |
| T-E2E-10 | Counts per `pipelined_outcome` match the setup | P1 | E3 | not run | | | | | `not_deferred` counts can be checked now; `pipelined` and `fell_back` need kv-sink |
| T-E2E-11 | Llama-3.3-70B, one pass of T-E2E-04 and 06 | P1 | E4 | not run | | | | | TP=1 on this box (plan section 3); model not downloaded; T-E2E-04 half blocked (kv-sink) |

### 5.7 Failure injection

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-FLT-01 | Load fails for present keys: leading run served, rest recomputed | P0 | E3 | partial | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_fault_inject_l2_adapter.py`, 6 passed) | d41162b4 | Adapter tests only; with Aerospike inner, end to end, not run |
| T-FLT-02 | Kill one Aerospike node mid-fetch, RF 1 | P0 | E4 | not run | | | | | Needs the 3-node local cluster |
| T-FLT-03 | Same with RF 2: reads fail over | P1 | E4 | not run | | | | | |
| T-FLT-04 | Aerospike node restarts: registrations dropped, fall back | P0 | E4 | not run | | | | | Registration half needs kv-sink |
| T-FLT-05 | Kill the LMCache server mid-retrieve, `recompute` and `fail` | P0 | E3 | not run | | | | | |
| T-FLT-06 | Restart the LMCache server between turns: L2 still found | P0 | E3 | partial | E3 (L2 = CE) | 2026-10-01 | `day1fix/e2e/compare_llama_restart0_*`, `compare_llama_restart_unseen_*`, `compare_llama_restart_between_pings_*` | d41162b4 | Restart between sends of P-exact: 20/20 equal, L2 found. Between P-multi turns not run. Requests before the next heartbeat recompute (S2, see Defects) |
| T-FLT-07 | Link down 2 s during a fetch: timeout, quarantine, reuse | P0 | E3 | blocked (kv-sink) | | | | | Needs an RDMA fetch in flight |
| T-FLT-08 | 5% packet loss for 60 s | P1 | E5a | not run | | | | | Lowest env E5a; a Soft-RoCE `tc netem` approximation is possible here |
| T-FLT-09 | Writer killed mid-store: no partial entry present | P0 | E2 | not run | | | | | Same as T-STO-03 |
| T-FLT-10 | Two engines store the same chunk 1,000 times | P1 | E4 | not run | | | | | Tests the known interleaving gap |
| T-FLT-11 | Reply names a slot not asked for: violation reported | P1 | E1 | pass | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_suite.txt` (`rdma_pipeline_test.cpp`, `kUnknownSlot`) | bb833c9c | Mock writer; against the real server once kv-sink exists |

### 5.8 Eviction and capacity

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-EVT-01 | Fill the namespace past eviction: only correct hits or misses | P0 | E2 | not run | | | | | |
| T-EVT-02 | Segments evicted, metadata survives: miss, no crash | P0 | E2 | not run | | | | | Same mechanism as T-STO-04 |
| T-EVT-03 | Records past TTL not found | P1 | E2 | not run | | | | | |
| T-EVT-04 | Eviction during an in-flight pipelined fetch | P1 | E3 | blocked (kv-sink) | | | | | |
| T-EVT-05 | L1 pressure never evicts leased RDMA windows | P0 | E0 | pass | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_l1_rdma_windows.py`, 35 passed) | d41162b4 | |
| T-EVT-06 | Client LRU off, two hosts: neither deletes the other's entries | P1 | E4 | not run | | | | | |

### 5.9 Sharing between hosts

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-SHR-01 | Host B hits host A's P-shared entries | P0 | E4 | not run | | | | | Second LMCache + vLLM on the same GPU (plan section 3) |
| T-SHR-02 | Both hosts store the same prefix at once | P1 | E4 | not run | | | | | See T-FLT-10 |
| T-SHR-03 | Host A restarts while host B fetches A's entries | P1 | E4 | not run | | | | | |

### 5.10 Fabric-specific

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-FAB-01 | ConnectX RoCE: GID index auto-selection | P0 | E5a | N/A on this box | | | | | No ConnectX |
| T-FAB-02 | EFA: SRD QP, qkey, address handle | P0 | E5b | N/A on this box | | | | | No EFA |
| T-FAB-03 | EFA: unsolicited write receive capability | P0 | E5b | N/A on this box | | | | | No EFA |
| T-FAB-04 | EFA: out-of-order piece arrival | P0 | E5b | N/A on this box | | | | | No EFA; cannot be reproduced on Soft-RoCE |
| T-FAB-05 | More completions than receive slots under overload | P1 | E5a | N/A on this box | | | | | No ConnectX or EFA |

### 5.11 ROCm (E6)

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-ROC-01 | `BUILD_WITH_HIP=1` build with Aerospike and RDMA | P1 | E6 | pass | E0 (MI300X) | 2026-09-30 | `day1/step1/build_B_rdma.log` | a3456e2c | The T-CFG-02 build with `BUILD_WITH_HIP=1` |
| T-ROC-02 | Layer progress across processes (event IPC on HIP) | P1 | E6 | pass | E0 + E3 (MI300X) | 2026-09-30 | `day1/step1/suite_1_events.txt` (`test_rocm_event_ipc.py`, `test_event_ipc.py`), `day1/step6_bi/` layerwise-on runs | a3456e2c | 40 event IPC tests; layerwise end to end serves 20/20 equal with timeline-semaphore events (`6eb51a66`) |
| T-ROC-03 | T-E2E-02 to 07 on MI350P | P1 | E6 | N/A on this box | | | | | No MI350P. MI300X runs are E6-equivalent on CDNA 3, not CDNA 4 |

## Defects

Known open items. Record them; don't treat them as new defects.

| ID | Severity | Finding | Affects | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| D-01 | S2 | After an LMCache restart, requests sent before vLLM's next heartbeat miss and recompute (at most one heartbeat interval). The restart-shorter-than-heartbeat case is fixed (`717ec8e4`); this gap is by design for now | T-E2E-03, T-FLT-06; harness waits for "Registered KV cache" before sending | MP connector | Open |
| D-02 | S3 | gpt-oss-120b runs with KV block size 16 under the MP connector, where vLLM alone picks 64 | T-E2E-08; gpt-oss prefix oracle must use block size 16 | MP connector | Open |
| D-03 | Info (merge review 3) | The whole-object fallback re-runs `_objects_of` right after it failed; on success `objects_to_place` and `request_cache_keys` run two or three times | Fallback path cost, not correctness | Track C | Open |
| D-04 | Info (merge review 5) | Cleanups: test-only C++ `SlotPlanner` and `FetchPlanner.participating_chunks`; sliding-window rule written four times; duplicated defaults between server config and `PipelinedFetchConfig`; unreachable defensive code and three `Optional` fields in `lmcache_driven_transfer.retrieve`; per-layer staging's Python call count; test fakes imported by `lmcache.v1.layerwise`; duplicated shared-memory attach helper | Maintainability | Owners per item | Open |
| D-05 | Info (pre-existing) | Unit tests failing before Day 1, unrelated: `test_engine_driven_transfer.py` (ImportError of `TransferDirection`), `test_cache_server.py` and `test_mq.py` (expect registration to return `None`), `test_torch_ops.py` `cpu_py_ops` / `multi_layer_block_kv_transfer` | E0 suite totals (`day1fix/batch1/pytest.txt`: 14 + 1 failures in those two MP files) | Owners of those tests | Open |
| D-06 | Info | For gpt-oss-120b, a cached prefix changes the output in vLLM alone (prefix cache vs no cache: 7/10 exact on P-shared under batch invariance); LMCache matches vLLM's prefix cache exactly | Oracle for hybrid prefix hits | vLLM upstream | Open (oracle adjusted) |
