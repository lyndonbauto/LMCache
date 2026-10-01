# Work item 2: Soft-RoCE (E1), storage (E2) and the remaining CPU rows

CPU only, 2026-10-01 16:18–17:15 UTC, worker `cpu-item2-storage`. No GPU,
nothing on ports 8000/6555, `aerospike-ce` (127.0.0.1:3000) never touched.
Tests ran in `lmc-c` from the clone `/root/lmc-work/LMCache-cpu`
(`prototype-stage1`; test commits in `VERSIONS.md`), against:

- **E2:** a private Aerospike CE 8.2.0.0 node, container `aerospike-ce-t2`,
  127.0.0.1:3200–3202 (`scripts/aerospike-t2.conf`): namespace `lmcache`
  (memory 8G, nsup 10 s, default-ttl 7200) and `lmcache_evict` (memory 512M,
  evict-used-pct 50, evict-tenths-pct 200, nsup 2 s).
- **E1:** Soft-RoCE `rxe0` on `lo`, GID index 1; the kv-sink server
  (`8.1.3.0-112-g512b0c207`, `aero-kvsink`, 127.0.0.1:3100) for T-CFG-06/07,
  warmed with `kvsink_smoke.sh` after the start (cold 2/3, warm 3/3: issue 5).

Paths below are relative to `/root/lmc-work/functional/item2/`.

## Results

| Test ID | What was checked | Result | Env | Evidence |
| --- | --- | --- | --- | --- |
| T-CFG-03 | `fetch_timeout_seconds` ≥ `write_ttl_seconds` refused at `StorageManager` startup, naming both | pass | E1 | `logs/cfg03_ttl5.txt`, `logs/cfg03_ttl4.txt`; E0 `logs/e0_units.junit.xml` (`test_rdma_registration.py::TestFetchTimeoutAgainstWriteTtl::test_timeout_at_or_beyond_the_ttl_is_rejected[600.0,601.0,10000.0]`) |
| T-CFG-04 | Growable L1 slab with RDMA: `ValueError` naming the hazard | pass (rerun) | E0 | `logs/e0_units.junit.xml` (`test_rdma_registration.py::TestRdmaWindowPlan::test_growable_slab_is_rejected_naming_the_hazard`) |
| T-CFG-05 | Window too small for `--pipelined-max-chunks`: placer refuses, registration warns, model not pipelined | pass | E0 | `logs/cfg05_registry.junit.xml` (`test_lmcache_driven_layout_registry.py::test_a_window_too_small_for_the_chunk_cap_warns_and_loads_whole_objects` (new), `test_pipelined_placer_access.py::test_a_window_too_small_for_the_chunk_cap_is_refused`) |
| T-CFG-06 | GID index 0 on Soft-RoCE `lo` | **fail (S3, D-11)** | E1 | `logs/cfg06_gid0.txt` vs `logs/cfg06_gid1.txt` |
| T-CFG-07 | RDMA window range (64 MiB) over `RLIMIT_MEMLOCK` (16 MiB), `CAP_IPC_LOCK` dropped | pass | E1 | `logs/cfg07_memlock16m.txt`, control `logs/cfg07_control.txt` |
| T-STO-03 = T-FLT-09 | Segments without meta record: absent, unreadable; real writer SIGKILLed between segments and meta | pass | E2 | `logs/storage_it_final.junit.xml` (`test_segments_without_their_meta_record_are_absent_and_unreadable`, `test_a_writer_killed_mid_store_never_leaves_an_entry_reported_present`); 3 extra kill runs `logs/integrity_kill_{1,2,3}.txt` |
| T-STO-04 | One segment deleted under intact meta: load fails; storage manager serves only intact keys, byte-exact | pass | E2 | `test_a_missing_segment_fails_the_load`, `test_the_storage_manager_treats_a_missing_segment_as_a_miss` |
| T-STO-05 | Wrong `tot_b` (larger, smaller, inline), garbage and short `runs`: fails as corrupt, never a size-mismatch read | pass | E2 | `test_a_corrupt_meta_record_fails_the_load_as_corrupt[5 cases]`, `test_aerospike_record_layouts_integration.py::test_a_corrupt_runs_bin_fails_the_read` |
| T-STO-07 | Meta and every segment carry `default_ttl_seconds` (600); 0 takes namespace `default-ttl` (7200) | pass | E2 | `test_every_record_carries_the_configured_ttl`, `test_a_zero_ttl_takes_the_namespace_default` |
| T-LKP-03 | Chunks 0, 2 stored, 1 missing: prefix prefetch serves only chunk 0 | pass | E2 | `test_only_the_leading_run_of_hits_is_served` |
| T-LKP-06 | Keys differing only in `object_group_id` stored, loaded, deleted apart | pass | E0 + E2 | `logs/lkp06_e0.junit.xml` (`test_native_connector_l2_adapter.py::TestEndToEndWorkflow::test_keys_differing_only_in_object_group_are_stored_apart`, new), E2 `test_keys_differing_only_in_object_group_are_stored_apart` |
| T-EVT-01 | Namespace filled past eviction: only byte-exact hits or misses | pass | E2 | `test_a_namespace_filled_past_eviction_serves_only_correct_hits_or_misses`: stored 101/168 (rest hit stop-writes), 140 records evicted, 73 served, all exact |
| T-EVT-02 | Segment gone, meta survives: miss, no crash | pass (mechanism) | E2 | Same tests as T-STO-04 (segment removed by hand, not by eviction) |
| T-EVT-03 | Object past its TTL (2 s): absent and unreadable before nsup | pass | E2 | `test_an_object_past_its_ttl_is_absent_and_unreadable` |
| T-EVT-05 | L1 pressure never evicts leased windows | pass (rerun) | E0 | `test_l1_rdma_windows.py` 35 passed (`::test_window_objects_are_not_evictable`) |
| T-PIPE-01, 02 | Layer ready only when every piece landed; unsent layers untouched | pass (rerun) | E1 | `logs/rdma_suite.junit.xml` (`rdma/test_rdma_pipeline.py::test_a_layer_is_consumable_before_later_layers_arrive`), `logs/rdma_harness_direct.txt` |
| T-PIPE-03 | Out-of-order arrival handed over ascending | pass (rerun) | E0 | `test_layer_arrival_pump.py::test_pump_loads_layers_in_plan_order_when_arrivals_are_out_of_order`, `test_arrival_source_conformance.py::test_the_pump_loads_ascending_when_slots_land_backwards[aerospike,scripted]` |
| T-PIPE-04 | Stale generation, unknown slot, duplicate immediate | pass (rerun) | E1 | `rdma/test_rdma_pipeline.py`, `rdma/test_pipelined_fetch_session.py` (`rdma_pipeline_test`, `pipelined_fetch_session_test` in `logs/rdma_harness_direct.txt`) |
| T-PIPE-09 | Plan over slot/chunk cap refused before leasing | pass (rerun) | E0 | `test_layer_arrival_pump.py::test_pump_passes_a_too_large_refusal_through_before_touching_the_sink`, `test_aerospike_layer_arrival_source.py::test_the_native_issuer_refuses_an_oversized_plan_before_sending`, `test_pipelined_retrieve.py::test_a_plan_past_the_slot_ceiling_is_refused_and_released_never_fetched` |
| T-FLT-11 | Reply names a slot not asked for / owned by another node: violation, no layer credited | pass | E1 | `rdma/test_pipelined_fetch_session.py` (new case `a reply naming a slot it was not asked for is a violation`), plus `kUnknownSlot` in `rdma_pipeline_test`. Against the kv-sink server: N/A, the server cannot be made to misreply |
| T-RDMA-01 to 04 | Byte-identical landing, nothing outside offsets, write past window refused, handle per node | pass (rerun) | E1 | `rdma/test_rdma_equivalence.py::test_rdma_write_is_byte_identical_to_the_normal_path`; 428 `ok`, 0 `FAIL` in `logs/rdma_harness_direct.txt` |
| T-FLT-01 | Load fails for present keys: leading run served | partial (E2 half added) | E2 | `test_the_storage_manager_treats_a_missing_segment_as_a_miss` (prefix `[T, F, F]`); recompute end to end needs E3 |
| T-RDMA-06 | First 100 P-exact keys (Llama-3.1-8B corpus token IDs, server key derivation, 32 MiB objects): pipelined RDMA fetch vs plain get vs stored bytes | **fail (S2, D-12)** | E1 + kv-sink | `logs/rdma06_try1.txt`, `logs/rdma06_try2.txt` (`test_aerospike_rdma_byte_oracle_integration.py::test_pipelined_rdma_fetches_equal_plain_gets_for_100_p_exact_keys`, new). The 15 prompts of 1, 2 and 4 chunks (35 keys) land byte-identical to plain gets and to the store. The 64-chunk prompt falls back both runs (server defect D-12), so 65 keys are not compared. Size sweep `logs/rdma06_threshold*.txt` |
| T-CFG-08 | Registration log line for a supported model | not run | | E3 (vLLM on the GPU) |

Suites: E2 30/30 (`logs/storage_it_final.txt`); RDMA on `rxe0` 17/17
(`logs/rdma_suite.txt`) and `make test` all `PASS`; E0 units 397/397
(`logs/e0_units.txt`); `test_native_connector_l2_adapter.py` 80/80;
registry + placer access 32/32.

## Defects

| ID | Severity | Finding | Cause |
| --- | --- | --- | --- |
| D-11 | S3 | GID index 0 on Soft-RoCE `lo` does not fail startup and the error never names the GID. `StorageManager` starts; at layout registration (same second) it logs `WARNING aerospike: pipelined fetch unavailable, retrieves will load whole objects: ... kv-sink-register failed: queue pair RTS failed from 127.0.0.1:3100`, and retrieves fall back to whole objects. Output stays correct. It is early (not a late `ENETUNREACH`), but an operator cannot tell it is the GID. | `AerospikePipelinedRdmaDriver::initialize` keeps init errors as "not ready" by design, and `RdmaContext` accepts any GID `ibv_query_gid` returns, including GID 0 on `lo` (`fe80::200:ff:fe00:0`, from an all-zero MAC). The server's QP fails RTS against that GID and replies only "queue pair RTS failed". The server logs nothing for the failed registration. |

| D-12 | S2 | On the kv-sink server, a pipelined fetch that needs more than one `kv-sink-fetch-pipelined` command (over 256 slots: more than 4 Llama-3.1-8B chunks) intermittently fails and then disables the client's region. Sweep, one fresh client each: 5, 8, 9, 16 chunks pipelined; 12, 32, 64 fell back; 64 fell back in both full runs. Server log: `late completion for slot N` with N the first slot of a later command (256, 512, 1024, 1280), then `region N in error state` for every remaining write. Output stays correct (whole-object reload), but every later fetch on that registration falls back until the client restarts (server issue 8). | Server, `kv_sink_verbs.c`: `verbs_imm_finish` sets `vr->imm_reap_closed = true` when a command finishes and clears it only when the next command's `verbs_imm_finish` starts, after all its writes are posted. While posting, `verbs_try_reap_sends` polls the completion queue; a completion of the new command's own first write seen then is treated as late, sets `xfer_failed` and frees the token that `tracked[]` still points to (use after free). Same state as server issue 7, but hit by one request's own sequential commands. |

Seen, not new: server issue 5 (cold kv-sink fetch falls back, D-08):
`logs/smoke1/` 2/3, `logs/smoke2/` 3/3.

## Notes

- `lmc-c` has `CAP_IPC_LOCK`, which bypasses `RLIMIT_MEMLOCK`; T-CFG-07 drops
  it with `setpriv --bounding-set -ipc_lock` (`scripts/run_cfg_probe.sh`).
  With the cap, the memlock check cannot fail in this container.
- Memlock is reported, like the GID, as a startup WARNING and a
  whole-object fallback, not a process exit. It names
  `RLIMIT_MEMLOCK` and the byte count, which is the plan's pass criterion.
- T-RDMA-06 keys use the model name vLLM sends (the snapshot path under
  `/work/hf`), world size 1, blake3 chunk hashes of 256 tokens. A read-only
  `exists` of 35 of them on `aerospike-ce` (set `kv_chunks`) found none,
  because Stage 2 truncates the set between runs, so the derivation is
  checked against the server code path, not against stored Stage 2 records.
- `rdma06_try2` logs `kv-sink-deregister failed:` (empty reason) twice on
  manager close, yet the server logs 16 registrations and 16
  deregistrations for that run; no region leaked.
- The Python client's `exists()` returns `(key, None)` for a missing record
  (it does not raise), which the tests rely on.

## Next steps

1. D-11: decide between a startup check that rejects a link-local GID
   derived from an all-zero MAC (or any GID with no route) naming
   `gid_index` and the GID, and adding `gid_index`/GID to the
   registration failure text. Either is a product change (not made here).
2. D-12: report to the Aerospike server team as a new issue (clear
   `imm_reap_closed` before posting, or keep reap state per command as issue
   7's fix proposes). Until then T-E2E-04 (pipelined end to end on Llama)
   will fall back on most prompts over 4 chunks and lose pipelining for the
   rest of the process. Rerun T-RDMA-06 unchanged once the server is fixed.
3. T-CFG-08, T-FLT-01 end to end: GPU worker (E3).
