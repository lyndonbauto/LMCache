# Functional test ledger

One row for every test ID in section 5 of
[`functional-test-plan.md`](../docs/design/v1/layerwise/functional-test-plan.md).
Update the row whenever a test runs, and keep the plan's pass rules
(section 1) and oracle (section 2): token equality at temperature 0 with
`VLLM_BATCH_INVARIANT=1` on both the baseline and the LMCache run; for
gpt-oss-120b prefix hits, compare against vLLM's own prefix cache at block
size 16.

Gaps that need a later product, server or test change (and why they could
not be closed during this campaign) are in [`OPEN-GAPS.md`](OPEN-GAPS.md).

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
MI300X. E4 storage is the 3-node CE cluster from `functional/harness/cluster.sh`
(`127.0.0.1:3300/3310/3320`) driven through the adapter with no GPU; the
3-node kv-sink cluster is `kvsink_cluster.sh` (`3400/3410/3420`). Because the box is ROCm, every E3 run is also E6-equivalent on
CDNA 3. "Commit" is the LMCache commit the run used: `a3456e2c` is the Day 1
native build, `16d471e8` the Day 1 product code, `bb833c9c` the RDMA test
build fix, and `d41162b4` the five fixes after Day 1 (the tests ran on the
working tree just before those commits).

The pipelined RDMA path needs the Aerospike `kv-sink` server build. It is
built and smoke-tested on the box (`8.1.3.0-112-g512b0c207`, container
`aero-kvsink`, `127.0.0.1:3100`; see
[`stage3/KVSINK-SERVER-BUILD.md`](stage3/KVSINK-SERVER-BUILD.md)), so the
pipelined rows that waited for it are now **not run**. Two caveats apply to
all of them: this build fences, replying only after every write completes
(server issue 4), so `pipelined` outcomes show no overlap and the plan's
"no fence" entry criterion is not met; and the first fetch after each server
start falls back unless the server is warmed (issue 5).

## Ledger

### 5.1 Build and configuration

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-CFG-01 | Build without RDMA flags; adapter works with no libibverbs | P0 | E0 | pass | E0 | 2026-09-30 | `day1/step1/build_A_no_rdma.log`, `day1/step1/check_A.txt` | a3456e2c | Builds in 67 s; `lmcache_aerospike` does not link `libibverbs` and imports |
| T-CFG-02 | Build with `BUILD_WITH_AEROSPIKE_RDMA=1` (and EFA) | P0 | E0 | pass | E0 | 2026-09-30 | `day1/step1/build_B_rdma.log`, `day1/step1/check_B.txt` | a3456e2c | Verbs build links `libibverbs`, imports, RDMA symbols present. The EFA half needs `libefa`: N/A on this box |
| T-CFG-03 | Fetch timeout at or past the L1 write TTL refused | P0 | E0 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/cfg03_ttl5.txt`, `item2/logs/cfg03_ttl4.txt`; E0 `item2/logs/e0_units.junit.xml` (`test_rdma_registration.py::TestFetchTimeoutAgainstWriteTtl::test_timeout_at_or_beyond_the_ttl_is_rejected`) | b94cad49 | `StorageManager` startup with RDMA raises `ValueError` naming `fetch_timeout_seconds` and `write_ttl_seconds` (TTL 5 and 4 with timeout 5) |
| T-CFG-04 | RDMA with a growable L1 slab raises `ValueError` naming the hazard | P0 | E0 | pass | E0 | 2026-10-01 | `item2/logs/e0_units.junit.xml` (`test_rdma_registration.py::TestRdmaWindowPlan::test_growable_slab_is_rejected_naming_the_hazard`) | b94cad49 | Rerun |
| T-CFG-05 | Window too small for `--pipelined-max-chunks`: warn, not pipelined | P1 | E0 | pass | E0 | 2026-10-01 | `item2/logs/cfg05_registry.junit.xml` (`test_lmcache_driven_layout_registry.py::test_a_window_too_small_for_the_chunk_cap_warns_and_loads_whole_objects`, `test_pipelined_placer_access.py::test_a_window_too_small_for_the_chunk_cap_is_refused`) | c6980baf | Extend written (`c6980baf`): registration warns `Cannot fetch ... layer by layer` and the model is not pipelined |
| T-CFG-06 | Wrong GID index fails at startup naming the GID | P1 | E1 | fail | E1 (rxe0, kv-sink) | 2026-10-01 | `item2/logs/cfg06_gid0.txt`, control `item2/logs/cfg06_gid1.txt` | b94cad49 | GID index 0 on `lo`: startup succeeds; at layout registration a WARNING says `kv-sink-register failed: queue pair RTS failed` and retrieves load whole objects. Early, but the GID is never named (S3, D-11). Probe `item2/scripts/cfg_probe.py`, no pytest test |
| T-CFG-07 | Slab over `RLIMIT_MEMLOCK` fails naming memlock | P1 | E1 | pass | E1 (rxe0, kv-sink) | 2026-10-01 | `item2/logs/cfg07_memlock16m.txt`, control `item2/logs/cfg07_control.txt` | b94cad49 | `CAP_IPC_LOCK` dropped, memlock 16 MiB, windows 64 MiB: startup WARNING names `RLIMIT_MEMLOCK` and 67108864 bytes, then whole-object fallback (not a process exit). `lmc-c` has `CAP_IPC_LOCK`, so this cannot fail there without dropping it. Probe, no pytest test |
| T-CFG-08 | Registration logs `<model> fetches layer by layer from L2 adapter 0...` | P0 | E3 | not run | | | | | Needs the Aerospike adapter with RDMA windows against the kv-sink server. With CE, `day1fix/e2e_run.log` shows staging matching the plan for 32 layers, then `Cannot fetch ... layer by layer` (expected without RDMA) |

### 5.2 Storage: the plain (non-RDMA) path

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-STO-01 | Put, exists, get, delete round trip, single and sharded | P0 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` | a3456e2c | 14/14 integration tests; rerun 29/29 in `day1fix/batch1b/out.txt` |
| T-STO-02 | Plane-aligned sharding: no record straddles two layers | P0 | E0 | pass | E1 | 2026-09-30 | `day1/step2/rdma_suite.txt` (`test_shard_plan.py`, `test_slot_plan_parity.py`) | bb833c9c | Also in `day1fix/batch1/junit.xml` |
| T-STO-03 | Writer killed before metadata: nothing reported present | P0 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | `item2/logs/storage_it_final.junit.xml` (`test_aerospike_storage_integrity_integration.py::test_segments_without_their_meta_record_are_absent_and_unreadable`, `::test_a_writer_killed_mid_store_never_leaves_an_entry_reported_present`), `item2/logs/integrity_kill_1.txt` to `_3` | 87b84b6b | Writer subprocess SIGKILLed after a later object's first segment lands: no entry reported present, no load |
| T-STO-04 | Missing segment under intact metadata reads as a miss | P0 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | `item2/logs/storage_it_final.junit.xml` (`::test_a_missing_segment_fails_the_load`, `::test_the_storage_manager_treats_a_missing_segment_as_a_miss`) | 87b84b6b | Prefix stops at the damaged key; the other keys load byte-exact |
| T-STO-05 | Corrupt metadata fails as corrupt, never as a short read | P1 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | `item2/logs/storage_it_final.junit.xml` (`::test_a_corrupt_meta_record_fails_the_load_as_corrupt`, 5 cases; `test_aerospike_record_layouts_integration.py::test_a_corrupt_runs_bin_fails_the_read`) | 87b84b6b | Total size larger, smaller, inline smaller; runs garbage and too short: each fails naming the meta record, never as a short read |
| T-STO-06 | Records written before plane-aligned sharding still readable | P1 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` (`test_aerospike_record_layouts_integration.py`) | a3456e2c | |
| T-STO-07 | Record TTL equals `default_ttl_seconds` | P1 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | `item2/logs/storage_it_final.junit.xml` (`::test_every_record_carries_the_configured_ttl`, `::test_a_zero_ttl_takes_the_namespace_default`) | 87b84b6b | Meta, every segment and inline records carry 600 s; 0 takes the namespace default (7200) |
| T-STO-08 | Replication factor 2, commit level all | P1 | E4 | pass | E4 storage (3-node CE, CPU) | 2026-10-01 | `stage6/logs/sto08.txt`, `stage6/logs/cluster_it_full.txt`, `stage6/logs/sto08_sensitivity.txt` | 9367ef65 | After each of 40 stores (120 records) the cluster's replica count grew exactly as much as its master count; with n2 SIGKILLed all 60 keys load byte-exact, and again after it rejoins. The check is sensitive: plain-client commit level `master` left the replica behind in 97/2000 puts, `all` in 0/2000 |

### 5.3 Lookup and keys

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-LKP-01 | Batch exists over mixed hits and misses, in order | P0 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` | a3456e2c | |
| T-LKP-02 | Batch exists uses the Aerospike batch call (10,000 keys) | P0 | E2 | pass | E2 (CE 8.2) | 2026-09-30 | `day1/step3/integration.txt` | a3456e2c | Includes the 10,000-key batch exists |
| T-LKP-03 | Prefix semantics: only the leading run of hits is used | P0 | E2 | pass | E2 (CE 8.2); E3 (L2 = CE) | 2026-10-01 | `item2/logs/storage_it_final.junit.xml` (`::test_only_the_leading_run_of_hits_is_served`); `stage2/lkp03/` | b94cad49; 49f18d12 (product 00cd3eee) | E3, layerwise off and on: hits at 0, 1, 3, 5, 6 of 6 chunks exactly 0, 256, 768, 1280, 1536 tokens; an L2 gap (chunk 2's 65 records deleted, L1 empty after restart) gives a 512-token hit from L2, 2 chunks loaded, chunks 3-5 unused; 22/22 equal |
| T-LKP-04 | Tenant isolation: salt A never hits salt B (P-salt) | P0 | E3 | pass | E0; E3 (L2 = CE) | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_cache_salt_l2_eviction.py`, 6 passed); `stage2/lkp04/` | d41162b4; 49f18d12 (product 00cd3eee) | End to end, layerwise off and on: salt B's first request hits 0 tokens after A stored the prefix, from L1 and from L2 after a restart; A's repeat hits 2048 on 10/10 (L1, and L2 after restart); 120/120 equal |
| T-LKP-05 | No hit across models, or across TP=1 and TP=2 | P0 | E3 | partial | E3 (L2 = CE) | 2026-10-01 | `stage2/lkp05/`; `day1fix/batch1/junit.xml` | 49f18d12 (product 00cd3eee) | Model half passes, layerwise off and on: Llama-3.1-8B stores, then gpt-oss-120b on the same LMCache server and Aerospike set sends the same token IDs: 0 hits from L1 and from L2 (after restart), no errors; Llama re-served hits its own entries 30/30 equal. TP half N/A on this box (one GPU): unit level only, `test_object_key_parallel.py` passed (14); full TP half on an 8-GPU droplet |
| T-LKP-06 | Keys differing only in `object_group_id` stored separately | P1 | E0 | pass | E0, E2 (CE 8.2) | 2026-10-01 | `item2/logs/lkp06_e0.junit.xml` (`test_native_connector_l2_adapter.py::TestEndToEndWorkflow::test_keys_differing_only_in_object_group_are_stored_apart`), `item2/logs/storage_it_final.junit.xml` (`::test_keys_differing_only_in_object_group_are_stored_apart`) | 87b84b6b |  |

### 5.4 RDMA data path (whole object)

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-RDMA-01 | RDMA write lands byte-identical to the source | P0 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_suite.junit.xml` (`rdma/test_rdma_equivalence.py::test_rdma_write_is_byte_identical_to_the_normal_path`), `item2/logs/rdma_harness_direct.txt` | b94cad49 | Rerun; mock writer |
| T-RDMA-02 | Nothing lands outside the requested offsets | P0 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_harness_direct.txt` | b94cad49 | Rerun (428 ok, 0 FAIL) |
| T-RDMA-03 | Write past the registered window refused | P0 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_harness_direct.txt` | b94cad49 | Rerun |
| T-RDMA-04 | Region handle held per node | P0 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_harness_direct.txt` | b94cad49 | Rerun |
| T-RDMA-05 | Per-node `kv-sink-register` fanout on a real cluster | P0 | E4 | pass | E4 (3-node kv-sink, rxe0 GID 1) | 2026-10-01 | `stage6/logs/rdma05_fanout.txt`, `stage6/logs/rdma05_adapter_probe.txt` | 862e8289 | `kv_sink_fanout_probe` (production `RdmaContext` + `register_all_nodes`): 3/3 nodes registered, one node-scoped region each, separate server QPN per node, every QP to RTS, all deregistered. LMCache's adapter never calls the fanout on more than one node: startup logs `pipelined fetches need a single-node cluster, but the cluster has 3 nodes` and loads whole objects (N1, plan section 7) |
| T-RDMA-06 | Byte oracle: 100 P-exact keys, RDMA fetch vs plain get | P0 | E2 | partial | E1 (rxe0) + kv-sink (512b0c207, fencing) | 2026-10-01 | `stage3/SUMMARY.md` (CPU half); box `stage3/logs/rdma06/run1..4.txt` (`test_rdma_equals_plain_gets_for_100_p_exact_keys_in_fetches_of_4_chunks`, `test_pipelined_rdma_fetches_equal_plain_gets_for_100_p_exact_keys`); earlier `item2/logs/rdma06_try1.txt`, `_try2.txt`, `rdma06_threshold*.txt` | eeb0f402 | CPU half passes on a fencing server: the first 100 P-exact keys (production key derivation, synthetic payloads in the Llama-3.1-8B layout), fetched at most 4 chunks at a time, land byte-identical to plain gets and to the store, 4 out of 4 runs. Fetches over 4 chunks are blocked by D-12 (the 64-chunk fetch fell back in both runs that tried). GPU half (records vLLM stored) prepared: `stage3.sh rdma06gpu`. Rerun on a no-fence build and after the D-12 fix |

### 5.5 Pipelined retrieve

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-PIPE-01 | A layer is ready only when every piece has landed | P0 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_suite.junit.xml` (`rdma/test_rdma_pipeline.py::test_a_layer_is_consumable_before_later_layers_arrive`), `item2/logs/rdma_harness_direct.txt` | b94cad49 | Rerun; mock writer |
| T-PIPE-02 | Unsent layers' regions untouched while earlier ones are consumed | P0 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_suite.junit.xml` (`rdma/test_rdma_pipeline.py`), `item2/logs/rdma_harness_direct.txt` | b94cad49 | Rerun |
| T-PIPE-03 | Out-of-order arrival still handed to the GPU in order | P0 | E0 | pass | E0 | 2026-10-01 | `item2/logs/e0_units.junit.xml` (`test_layer_arrival_pump.py::test_pump_loads_layers_in_plan_order_when_arrivals_are_out_of_order`, `test_arrival_source_conformance.py::test_the_pump_loads_ascending_when_slots_land_backwards`) | b94cad49 | Rerun |
| T-PIPE-04 | Stale generation, unknown slot, duplicate immediate distinguished | P0 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_suite.junit.xml` (`rdma/test_rdma_pipeline.py`, `rdma/test_pipelined_fetch_session.py`), `item2/logs/rdma_harness_direct.txt` | b94cad49 | Rerun |
| T-PIPE-05 | Server declines one slot: `fell_back`, output correct | P0 | E3 | partial | E1 | 2026-09-30 | `day1/step2/rdma_suite.txt` (`test_pipelined_fetch.py`), `day1fix/batch1/junit.xml` (`test_pipelined_retrieve.py`) | bb833c9c | E0/E1 half passes. Against the kv-sink server (E2), a damaged record falls back to a whole reload: `stage3/smoke/pipelined_it_run2.txt` (`test_a_missing_record_falls_back_to_a_whole_reload`). End to end not run |
| T-PIPE-06 | Layer never arrives: deadline, quarantine, output correct | P0 | E3 | partial | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_pipelined_retrieve.py`, `test_rdma_window_leaser.py`), `day1fix/` fix-1 three-track run | d41162b4 | E0 half passes. End to end not run |
| T-PIPE-07 | Late write from an abandoned fetch not credited to the next | P0 | E1 | partial | E1 (rxe0) | 2026-09-30 | `day1/step2/rdma_suite.txt` (stale generation), `day1fix/batch1/junit.xml` (`test_rdma_window_leaser.py` quarantine) | bb833c9c | E1 half passes. E3 extension not run |
| T-PIPE-08 | More concurrent retrieves than `window_count`: extras `refused` | P0 | E3 | not run | | | | | New test; expect server issues 7 and 8. Harness ready: `stage4.sh pipe08` (two vLLMs, window_count 1; one vLLM serializes retrieves, G-16), `pipe08s` records the single-instance case |
| T-PIPE-09 | Plan over the slot cap or chunk cap refused before leasing | P0 | E0 | pass | E0 | 2026-10-01 | `item2/logs/e0_units.junit.xml` (`test_layer_arrival_pump.py::test_pump_passes_a_too_large_refusal_through_before_touching_the_sink`, `test_aerospike_layer_arrival_source.py::test_the_native_issuer_refuses_an_oversized_plan_before_sending`, `test_pipelined_retrieve.py::test_a_plan_past_the_slot_ceiling_is_refused_and_released_never_fetched`) | b94cad49 | Rerun |
| T-PIPE-10 | Two same-prefix requests at once, each shared-keys mode | P0 | E3 | partial | E0 | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_shared_keys.py`, 7 passed) | d41162b4 | E0 half passes. End to end not run; expect server issue 7 (concurrent fetches on one region). Harness ready: `stage4.sh pipe10` (two vLLMs, modes recompute and wait, G-16) |
| T-PIPE-11 | Hybrid model: sliding-window layers fetch only their window | P0 | E3 | partial | E0 + E3 staging | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_fetch_planner.py`), `day1fix/e2e/lmcache_gptoss_staging.log` | d41162b4 | E0 half passes; gpt-oss staging matches the plan for 36 layers. Pipelined end to end not run |
| T-PIPE-12 | Records under one `max_record_bytes`, read under another | P1 | E3 | not run | | | | | New test |

### 5.6 End to end with vLLM

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-E2E-01 | P-short: no L2 traffic, output equal | P0 | E3 | pass | E3 (L2 = CE) | 2026-10-01 | `stage2/e2e01v2/` (rerun); `stage2/e2e01/` (first run, P-short v1) | 49f18d12 (product 00cd3eee); corpus edc901fc | Rerun on P-short-v2 (48-208 tokens, prompt + 48 output tokens within one chunk; decision option 2): 80/80 equal, layerwise off and on; zero Aerospike reads, writes, batch and lookup traffic from cold start to warm end. The v1 run wrote decode-completed chunks (D-09) |
| T-E2E-02 | P-exact and P-ragged, second send hits L1, output equal | P0 | E3 | pass | E3 (L2 = CE) | 2026-10-01 | `stage2/e2e02/` | 00cd3eee (harness a8f06e06) | P-exact and P-ragged 80/80 equal per mode; every warm hit is exactly the full chunks (P-ragged) or the whole prompt minus one token (P-exact), all from L1; 40/40 `not_deferred` |
| T-E2E-03 | Same, LMCache restarted so the hit is L2, pipelined off | P0 | E3 | pass | E3 (L2 = CE) | 2026-10-01 | `stage2/e2e03/` | 00cd3eee (harness a8f06e06) | P-exact and P-ragged 80/80 equal per mode after an LMCache restart (re-registered in 4 s); 35,964 Aerospike reads in the warm send; hits 0 L1 / 141,312 L2 tokens; 40/40 `not_deferred` |
| T-E2E-04 | Same, pipelined on: `pipelined` on every eligible request | P0 | E3 | not run | | | | | Warm the kv-sink server first (issue 5); restarts with `kill -9` leak server regions (issue 9). Expect D-12 on Llama prompts over 4 chunks |
| T-E2E-05 | P-long at and one chunk over the cap | P0 | E3 | partial | E3 (L2 = CE) | 2026-10-01 | `stage2/e2e05/` | 00cd3eee (harness a8f06e06) | Plain-path half: 20/20 equal per mode at 64 and 65 chunks, every retrieve `not_deferred` (pipelined off). Pipelined half (outcome at the cap is `pipelined`) in Stage 3 |
| T-E2E-06 | P-shared: later requests hit the shared prefix | P0 | E3 | pass | E3 (L2 = CE) | 2026-10-01 | `stage2/e2e06/` | 00cd3eee (harness a8f06e06) | 20/20 equal per mode; cold send requests 2-10 each hit the 2048-token shared prefix (8 chunks), warm 10/10. Pipelined not run (Stage 3) |
| T-E2E-07 | P-multi: each turn hits the previous turns' chunks | P0 | E3 | pass | E3 (L2 = CE) | 2026-10-01 | `stage2/e2e07/` | 00cd3eee (harness a8f06e06) | 100/100 equal per mode; each turn hits exactly the previous turn's full chunks (cold), its own full chunks (warm). Pipelined not run (Stage 3) |
| T-E2E-08 | Hybrid model (gpt-oss-120b) through T-E2E-02 to 07 | P0 | E3 | partial | E3 | 2026-09-30 | `day1/step5_bi/`, `day1fix/e2e/compare_gptoss_staging_*` | 16d471e8 | P-exact 20/20 equal (cold, warm, layerwise off and on); P-shared 10/10 equal to vLLM's own prefix cache. P-ragged, P-multi, L2 restart and pipelined not run. Block size 16 under the connector (S3, see Defects) |
| T-E2E-09 | 16 concurrent clients, byte oracle and logprob agreement | P0 | E3 | not run | | | | | Batch invariance allows token equality here (Llama; checked by `stage4.sh ref16`). Harness ready: `stage4.sh e2e09` (plain and pipelined), top-1 agreement via `logprob_agree.py`; per-request byte oracle missing (G-15) |
| T-E2E-10 | Counts per `pipelined_outcome` match the setup | P1 | E3 | partial | E3 (L2 = CE) | 2026-10-01 | `stage2/metrics_table.md`, `stage2/metrics_table_2b.md`, `stage2/*/report_*.md` | 00cd3eee (harness a8f06e06); 49f18d12 (product 00cd3eee) | Plain path: every retrieve `not_deferred`, deferred-retrieve counter never incremented, vLLM external hit tokens equal the expected hit on 680/680 (Stage 2a) and 478/478 requests or concurrent batches (Stage 2b). `pipelined` / `fell_back` counts need Stage 3 |
| T-E2E-11 | Llama-3.3-70B, one pass of T-E2E-04 and 06 | P1 | E4 | not run | | | | | TP=1 on this box (plan section 3); model not downloaded; needs three kv-sink server processes |

### 5.7 Failure injection

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-FLT-01 | Load fails for present keys: leading run served, rest recomputed | P0 | E3 | partial | E0, E2 (CE 8.2) | 2026-10-01 | `day1fix/batch1/junit.xml` (`test_fault_inject_l2_adapter.py`), `item2/logs/storage_it_final.junit.xml` (`test_aerospike_storage_integrity_integration.py::test_the_storage_manager_treats_a_missing_segment_as_a_miss`) | 87b84b6b | Storage manager over Aerospike serves the leading run when a present key fails to load; recompute end to end (E3) not run |
| T-FLT-02 | Kill one Aerospike node mid-fetch, RF 1 | P0 | E4 | partial | E4 storage (3-node CE, CPU) | 2026-10-01 | `stage6/logs/flt02_03.txt`, `stage6/logs/flt02_rf1_flushwait_{1,2,3}.txt` | 9367ef65 | Storage half pass (5 runs): about 26k-42k loads racing a SIGKILL of n3 each ended byte-exact or a clean miss, never wrong bytes, a hang or an exception; with n3 down 33-44 of 60 keys miss (sharded objects span partitions), the rest load, new stores work. End to end with vLLM in Stage 6 GPU |
| T-FLT-03 | Same with RF 2: reads fail over | P1 | E4 | partial | E4 storage (3-node CE, CPU) | 2026-10-01 | `stage6/logs/flt02_03.txt`, `stage6/logs/cluster_it_full.txt` | 9367ef65 | Storage half pass (2 runs): about 10.6k loads in the 12 s between the SIGKILL of n1 and the settled 2-node cluster, 0 misses, all byte-exact; all keys byte-exact after settling and after the rejoin. End to end with vLLM in Stage 6 GPU |
| T-FLT-04 | Aerospike node restarts: registrations dropped, fall back | P0 | E4 | partial | E4 storage (CE) + 3-node kv-sink (rxe0) | 2026-10-01 | `stage6/logs/flt02_rf1_flushwait_*.txt`, `stage6/logs/flt04_kvsink_probe.txt`, `stage6/logs/rdma05_adapter_probe.txt` | 9367ef65; 862e8289 | CE: after a SIGKILLed RF 1 node restarts from its device file all 70 keys load byte-exact (writes in the last `flush-max-ms` = 1 s before a SIGKILL are lost on that node and read as clean misses; the test waits 2.5 s). kv-sink: the restarted node refuses deregistration (`no such region`), confirming it dropped the registration; re-registering on the same `RdmaContext` fails (`queue pair already exists`), a fresh context registers 3/3. The adapter's fall-back-until-re-registration path cannot run on 3 nodes (N1) and has no node-restart re-registration (design open item) |
| T-FLT-05 | Kill the LMCache server mid-retrieve, `recompute` and `fail` | P0 | E3 | not run | | | | | |
| T-FLT-06 | Restart the LMCache server between turns: L2 still found | P0 | E3 | partial | E3 (L2 = CE) | 2026-10-01 | `day1fix/e2e/compare_llama_restart0_*`, `compare_llama_restart_unseen_*`, `compare_llama_restart_between_pings_*` | d41162b4 | Restart between sends of P-exact: 20/20 equal, L2 found. Between P-multi turns not run. Requests before the next heartbeat recompute (S2, see Defects) |
| T-FLT-07 | Link down 2 s during a fetch: timeout, quarantine, reuse | P0 | E3 | not run | | | | | Expect server issues 8 (region disabled for good) and 10 (busy-spin). Approved veth/netns link probed 2026-10-01: does not work with the v6.11 rdma_rxe (init_net only); stand-in still used (G-13) |
| T-FLT-08 | 5% packet loss for 60 s | P1 | E5a | not run | | | | | Lowest env E5a; a Soft-RoCE `tc netem` approximation is possible here |
| T-FLT-09 | Writer killed mid-store: no partial entry present | P0 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | See T-STO-03 | 87b84b6b | Same as T-STO-03 |
| T-FLT-10 | Two engines store the same chunk 1,000 times | P1 | E4 | fail | E4 storage (3-node CE RF 2, CPU) | 2026-10-01 | `stage6/logs/flt10.txt`, `stage6/logs/cluster_it_full.txt` | 9367ef65 | Two writer processes released together on one key, then read by a third adapter: sharded (3 MiB, 4 segments) mixed in 141/1000 and 145/1000 rounds, always whole segments from each writer; inline (64 KiB) 0/1000 twice (S2, D-14). The current code does not prevent it, and a create-only meta write alone would not either |
| T-FLT-11 | Reply names a slot not asked for: violation reported | P1 | E1 | pass | E1 (rxe0) | 2026-10-01 | `item2/logs/rdma_suite.junit.xml` (`rdma/test_pipelined_fetch_session.py`: `a reply naming a slot it was not asked for is a violation`; `rdma_pipeline_test.cpp`, `kUnknownSlot`) | c6980baf | Reply-level check added (`c6980baf`). Against the kv-sink server: not possible, the server cannot be made to misreply |

### 5.8 Eviction and capacity

| Test ID | Short plain description | Priority | Lowest env | Status | Env run | Date | Evidence path | Commit | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-EVT-01 | Fill the namespace past eviction: only correct hits or misses | P0 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | `item2/logs/storage_it_final.junit.xml` (`::test_a_namespace_filled_past_eviction_serves_only_correct_hits_or_misses`) | b94cad49 | 512 MiB namespace, evict-used-pct 50: 101/168 stored (rest stop-writes), 140 records evicted, 73 served, all byte-exact |
| T-EVT-02 | Segments evicted, metadata survives: miss, no crash | P0 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | See T-STO-04 | 87b84b6b | Mechanism only: segment removed by hand under intact meta, not by eviction |
| T-EVT-03 | Records past TTL not found | P1 | E2 | pass | E2 (CE 8.2) | 2026-10-01 | `item2/logs/storage_it_final.junit.xml` (`::test_an_object_past_its_ttl_is_absent_and_unreadable`) | b94cad49 | TTL 2 s: absent and unreadable before nsup removes it |
| T-EVT-04 | Eviction during an in-flight pipelined fetch | P1 | E3 | not run | | | | | |
| T-EVT-05 | L1 pressure never evicts leased RDMA windows | P0 | E0 | pass | E0 | 2026-10-01 | `item2/logs/e0_units.junit.xml` (`test_l1_rdma_windows.py`, 35 passed) | b94cad49 | Rerun |
| T-EVT-06 | Client LRU off, two hosts: neither deletes the other's entries | P1 | E4 | partial | E4 storage (3-node CE RF 2, CPU) | 2026-10-01 | `stage6/logs/evt06b.txt`, `stage6/logs/cluster_it_full.txt` | 9367ef65 | Storage half pass: two `StorageManager` processes, 24 shared + 24 own keys each, no L2 capacity: all 72 present and byte-exact. Contrast with host A's L2 LRU on (8 MiB for 24 MiB): A deleted all 24 shared keys (which B relied on) and 18-19 of its own, none of B's own. Two vLLM hosts in Stage 6 GPU |

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
| T-ROC-02 | Layer progress across processes (event IPC on HIP) | P1 | E6 | pass | E0 + E3 (MI300X) | 2026-09-30 | `day1/step1/suite_1_events.txt` (`test_rocm_event_ipc.py`, `test_event_ipc.py`), `day1/step6_bi/` layerwise-on runs; `stage2/conc/` | a3456e2c; 49f18d12 (product 00cd3eee) | 40 event IPC tests; layerwise end to end serves 20/20 equal with timeline-semaphore events (`6eb51a66`). Deadlock regression (2026-10-01, E3, L2 = CE): 4, 8 and 16 concurrent cached requests, layerwise and async scheduling on, from L1 and from L2 after restart: no hang (each batch about 3 s), 56/56 token-equal to the baseline, batch hits as expected |
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
| D-07 | Info (server issue 4) | The kv-sink server under test (`512b0c207`) replies to `kv-sink-fetch-pipelined` only after every write completes. Correct data, no overlap; the plan's "no fence" entry criterion is not met | Every pipelined test; any timing | Aerospike server team | Open |
| D-08 | Info (server issue 5) | Seen in the Stage 3 smoke: the first pipelined fetch after a server start registers seven 2 GiB stripes inside the fetch (about 3.5 s), past the 1,000 ms info timeout, and falls back. Warm the server after every start | T-E2E-04 and any test after a server restart | Aerospike server team; harness warms | Open |
| D-09 | Info (test design) | The MP connector stores chunks completed by generated tokens, so P-short prompts of 209+ tokens (with `max_tokens` 48) write one chunk to L1 and L2. No reads or loads; output equal | T-E2E-01 pass rule ("no L2 traffic") | Test plan / corpus; decision in `stage2/SUMMARY.md` | Closed 2026-10-01: P-short-v2 (edc901fc) keeps prompt + output in one chunk; T-E2E-01 rerun passes |
| D-10 | Info | After an LMCache restart, chunks completed by generated tokens that are already in L2 are recomputed and written again (390 records in T-E2E-03's warm send); stores deduplicate against L1 only | L2 write amplification, not correctness | L2 store controller | Open |
| D-11 | S3 | A wrong RDMA GID index (0 on Soft-RoCE `lo`) does not fail startup and no message names the GID: layout registration logs `kv-sink-register failed: queue pair RTS failed` and retrieves load whole objects. `RdmaContext` accepts any GID `ibv_query_gid` returns (`fe80::200:ff:fe00:0` from `lo`'s all-zero MAC) | T-CFG-06; operators cannot tell a GID misconfiguration from a server fault | Track B (RDMA context, registration errors) | Open; see `item2/SUMMARY.md` |
| D-12 | S2 | kv-sink server: a pipelined fetch needing more than one `kv-sink-fetch-pipelined` command (over 256 slots, more than 4 Llama-3.1-8B chunks) intermittently logs `late completion for slot N` (N = first slot of a later command), then `region N in error state`; the retrieve falls back and the region stays disabled (server issue 8). `imm_reap_closed` stays set between one request's sequential commands, and the posting path's reap (`verbs_try_reap_sends`) treats the new command's own completions as late (and frees tokens still tracked). 12, 32 and 64 chunks fell back; 5, 8, 9, 16 pipelined | T-RDMA-06, T-E2E-04 and any pipelined Llama prompt over 4 chunks; output stays correct | Aerospike server team (new issue, related to issue 7) | Open |
| D-13 | Info | Chunks prefetched from L2 are evicted from L1 right after the retrieve, so a repeat request reads them from L2 again; L1 copies of later chunks then go unused, because the L1 lookup counts a leading run from chunk 0 (T-LKP-03 healed probe: 1536 tokens from L2, 0 from L1, with chunks 2-5 just stored to L1) | L1 locality after L2 hits; not correctness | L1 / prefetch controller | Open |
| D-14 | S2 | Two writers storing the same sharded key at once leave a mixed object: the meta record says present and the segments come from both writers (about 14% of 1,000 rounds); a reader during a rewrite can see the same. `do_single_set` writes segments under fixed keys `<key>\|s\|<i>` with `EXISTS_IGNORE`, then the meta record; nothing ties a meta record to its segment set. A create-only meta write alone would not fix it: the losing writer has already overwritten the winner's segments | T-FLT-10, T-SHR-02; payloads from two engines for one key are equal or near-equal, so output impact is small; no cross-tenant data | LMCache adapter (Track A); pipelined path Track C | Open; fix chosen 2026-10-01: option 2 (per-write segment keys, create-only meta, first writer wins), pipelined-path variant pending. See `stage6/SUMMARY.md` and [`aerospike_concurrent_writes.md`](../docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md) |
| D-15 | S2 | LMCache client teardown does not revoke RDMA remote access before L1 frees the slab: `AerospikePipelinedRdmaDriver::shutdown()` sends `kv-sink-deregister` but keeps the window MR and QPs until the C++ object is destroyed, and the server's deregister does not drain in-flight writes (proposed server issue 12). On a cold server, writes from a fetch abandoned at the 1 s timeout land 0.05-0.5 s after `close()` in freed slab memory; this is the kv-sink warm-up smoke crash (test L1 is `torch.empty` on the glibc heap) | Teardown only: production L1 is shm/mmap freed at exit, so a running server is not corrupted; S1 if L1 memory is ever recycled in-process. Test `0ebf98d0` (`test_no_server_write_reaches_l1_after_close`, fails 7/9 on a cold server) | Track B (`RdmaContext`, driver shutdown); server half Aerospike server team | Open; findings `functional/stage3/crash/FINDINGS.md` (`5b1fa9fe`); see [`OPEN-GAPS.md`](OPEN-GAPS.md) G-10, G-11 |
