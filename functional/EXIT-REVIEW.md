# Functional test exit review (draft, T-E2E-11 pending)

Review of the Aerospike KV cache tier functional campaign against section 9
("Exit criteria") of
[`functional-test-plan.md`](../docs/design/v1/layerwise/functional-test-plan.md).
Drafted 2026-10-02 from `prototype-stage1` at `4534ecce`, while T-E2E-11
(Llama-3.3-70B) was still running on the box.

Sources: [`LEDGER.md`](LEDGER.md) (authoritative per-test status and D-01 to
D-25), [`OPEN-GAPS.md`](OPEN-GAPS.md), the stage summaries
([`day1`](day1/SUMMARY.md), [`item2`](item2/SUMMARY.md),
[`stage2`](stage2/SUMMARY.md), [`stage3`](stage3/SUMMARY.md),
[`stage3-newstack`](stage3-newstack/SUMMARY.md), [`stage4`](stage4/SUMMARY.md),
[`stage5`](stage5/SUMMARY.md), [`stage6`](stage6/SUMMARY.md),
[`newstack`](newstack/SUMMARY.md), [`d14`](d14/SUMMARY.md)), the D-14 design
record
[`aerospike_concurrent_writes.md`](../docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md),
and the control tower's decision notes (`decisions/*.txt`, `state.json`
notes and thread list, worker result files). Evidence paths in backticks
without a link are box paths relative to `/root/lmc-work/functional/`, as
in the ledger.

**Verdict: the functional phase does not meet its exit criteria.** Two of
the five section-9 criteria are not met on this box by construction (real
fabric, and "every P0 in E4"), and two are not met on findings: one S1
(D-17, a vLLM bug) and at least four S2 defects are open, and T-FLT-05
leaves vLLM wedged (D-21) or a request hung (D-22). No test on the current
stack returned a wrong token; the only wrong tokens in the campaign came
from D-17 under `recompute` (Stage 3, old stack), and fault tests have run
under `fail` since. (gpt-oss differences against vLLM's prefix cache at
block 16 are vLLM's own, D-06; the block-256 oracle is exact.)

## 1. Section 9 verdicts

| # | Exit criterion | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Every P0 test passes in its lowest environment and in E4 | **Not met** | 44 of 55 P0 tests pass in their lowest env (section 2). Not passing: T-LKP-05, T-PIPE-05/06/07/10, T-FLT-07 (partial), T-FLT-05 (fail), T-FAB-01..04 (N/A). "In E4" holds only for the tests whose lowest env is E4 (T-RDMA-05, T-FLT-02, T-FLT-04, T-SHR-01; P1: T-STO-08, T-FLT-03/10, T-EVT-06, T-SHR-02/03). The other P0 tests were not rerun in E4, and E4 here is one host (plan section 3). Pipelined fetch cannot run on a multi-node cluster (N1, plan section 7; `stage6/logs/rdma05_adapter_probe.txt`), so no pipelined P0 test can pass in a true E4 |
| 2 | Every P0 test in 5.4, 5.5 and 5.6 passes on a real fabric (E5a or E5b) | **Not met** (not testable here) | Every RDMA run is Soft-RoCE `rxe0` on `lo`. The box has no ConnectX or EFA (plan section 3: "E5a and E5b are out of scope"); T-FAB-01..05 are N/A on this box in the ledger |
| 3 | Zero S1 and zero S2 defects open | **Not met** | Open S1: D-17 (vLLM upstream, [vllm#49250](https://github.com/vllm-project/vllm/issues/49250)). Open S2: D-01, D-15, D-21 (S2 proposed, S1 arguable), D-22. D-12 (S2) is not reproduced on the new server but is not fixed (section 3) |
| 4 | Every fault test ends in correct output or a clean request error, engine alive, apart from section 7 | **Not met** | T-FLT-05 (`stage5/flt05/`): 2 of 7 in-retrieve kills wedged vLLM's engine for good (D-21, `/health` 200, no error); lookup-time kills left the request waiting until the client gave up (D-22). D-17: `recompute` after a mid-forward layerwise failure gives wrong tokens (`stage3/d17/`, [`stage3/D17-NARROWING.md`](stage3/D17-NARROWING.md)), so every recompute half that fails mid-forward is blocked and ran under `fail`. Met everywhere else: T-FLT-01/02/03/04/06, T-PIPE-05/06 (`fail`), T-EVT-04, T-SHR-03 ended exact or in a clean HTTP 500 with the engine alive. Section-7 errors outside deliberate faults: 0 (`stage5/count7_final.txt`) |
| 5 | Results, Aerospike server commit and LMCache commit recorded together | **Met with exceptions** | Ledger "Commit" and "Env run" columns, and a `VERSIONS.md` per stage folder (`functional/<stage>/VERSIONS.md`). Exceptions: (a) the kv-sink C client `523d51ea` (`sriram/kv-sink-batch-prio`) is private and never pushed ([`stage6/VERSIONS.md`](stage6/VERSIONS.md)), so a benchmark can only reproduce it from the box copy; (b) the ledger mixes two stacks (section 4), so a benchmark must use the new-stack rows; (c) T-E2E-11 is not recorded yet |

Stack the current results ran on (from
[`stage6/VERSIONS.md`](stage6/VERSIONS.md),
[`stage3-newstack/SUMMARY.md`](stage3-newstack/SUMMARY.md)):

| Part | Version |
| --- | --- |
| LMCache | `prototype-stage1`, product code `b6b0caae` (prototype-stage-1a `934052cf` merged in `e4701b9a`, plus the D-14 fix `e9cd0689`) |
| kv-sink server (pipelined path) | `8.1.3.0-111-g046e8558d` (`sriram/kv-sink-batch-prio`), single node, `aero-kvsink-bp`, 127.0.0.1:3700 |
| Aerospike C client | `523d51ea` (private) |
| Plain-path server | Aerospike CE 8.2.0.0 (one node on 3000; 3-node cluster on 3300/3310/3320) |
| vLLM / ROCm | `0.27.1.dev5+gf46a9dfe2.d20260827` (V2 runner, async scheduling on) / ROCm 10.0.0 on one MI300X |

## 2. Test status summary

Counts from the ledger (79 test IDs; T-E2E-11 counted as "not run" until
the GPU worker fills it in):

| Status | All tests | P0 only |
| --- | --- | --- |
| pass | 61 | 44 |
| partial | 8 | 6 |
| fail | 2 | 1 |
| not run | 2 (T-E2E-11 running, T-FLT-08) | 0 |
| N/A on this box | 6 | 4 |
| **Total** | **79** | **55** |

Passing test IDs by plan section:

| Section | Pass |
| --- | --- |
| 5.1 Build and configuration | T-CFG-01, 02 (EFA half N/A), 03, 04, 05, 07, 08 |
| 5.2 Storage, plain path | T-STO-01 to 08 |
| 5.3 Lookup and keys | T-LKP-01, 02, 03, 04, 06 |
| 5.4 RDMA data path | T-RDMA-01 to 06 |
| 5.5 Pipelined retrieve | T-PIPE-01, 02, 03, 04, 08, 09, 11, 12 |
| 5.6 End to end | T-E2E-01 to 09 |
| 5.7 Failure injection | T-FLT-01, 02, 03, 04, 06, 09, 10, 11 |
| 5.8 Eviction and capacity | T-EVT-01, 02, 03, 05, 06 |
| 5.9 Sharing between hosts | T-SHR-01, 02, 03 |
| 5.11 ROCm | T-ROC-01, 02 |

Every test that does not pass:

| Test ID | Pri | Status | Reason (one line) |
| --- | --- | --- | --- |
| T-CFG-06 | P1 | fail | Wrong GID index: startup succeeds and no message names the GID (D-11, S3); old stack only |
| T-LKP-05 | P0 | partial | Model half passes; the TP=1 vs TP=2 half needs more than one GPU (unit level only) |
| T-PIPE-05 | P0 | partial | `fail` half passes (clean 500, engine alive); `recompute` half blocked by D-17. `fell_back` with correct output is unreachable by record deletion |
| T-PIPE-06 | P0 | partial | `fail` half passes, window reused after the 30 s quarantine; `recompute` half blocked by D-17 |
| T-PIPE-07 | P0 | partial | E1 half passes; E3 cannot delay RDMA writes alone (no kv-sink per-slot delay hook, G-01) |
| T-PIPE-10 | P0 | partial | Busy request under `recompute` shared-keys mode can only fail cleanly; its recompute is blocked by D-17. `wait` mode passes |
| T-E2E-10 | P1 | partial | Plain path only; the row was never updated with pipelined outcome counts (see discrepancies) |
| **T-E2E-11** | P1 | **running — to be filled in** | **Placeholder: GPU worker `gpu-e2e11` running Llama-3.3-70B, one pass of T-E2E-04 and T-E2E-06. Control tower fills in status, evidence path and commit** |
| T-FLT-05 | P0 | fail | SIGKILL of LMCache during a layerwise retrieve: section-7 engine stop or permanent wedge (D-21); during a lookup: request never finishes (D-22). No wrong tokens |
| T-FLT-07 | P0 | partial | Stand-in only (kv-sink SIGSTOP 2 s); a real link-down is not possible with rxe0 on `lo` (G-13) |
| T-FLT-08 | P1 | not run | 5% packet loss; posted default is "N/A on this box" (lowest env E5a) |
| T-EVT-04 | P1 | partial | L1-pressure half and L2 half under `fail` pass; L2 recompute half blocked by D-17 |
| T-FAB-01 to 05 | P0 (05: P1) | N/A on this box | No ConnectX or EFA |
| T-ROC-03 | P1 | N/A on this box | No MI350P (CDNA 4) |

## 3. Defects

| ID | Sev | Status | Description |
| --- | --- | --- | --- |
| D-01 | S2 | **Open** (by design for now; restart-shorter-than-heartbeat case fixed in `717ec8e4`) | After an LMCache restart, requests before vLLM's next heartbeat miss and recompute |
| D-02 | S3 | Open | gpt-oss-120b runs with KV block 16 under the MP connector (vLLM alone picks 64) |
| D-03 | Info | Open | Whole-object fallback re-runs `_objects_of` and key planning two or three times |
| D-04 | Info | Open | Merge-review cleanups (test-only planners, duplicated defaults, `Optional` fields) |
| D-05 | Info | Open (pre-existing) | Unit tests failing before Day 1, unrelated to this work |
| D-06 | Info | Open (oracle adjusted) | gpt-oss output changes with vLLM's own prefix cache; LMCache matches the prefix cache |
| D-07 | Info | Open (old server `512b0c207`) | Old kv-sink server replies only after every write (fences); the plan's "no fence" entry criterion is not met |
| D-08 | Info | Open in ledger (old server) | First pipelined fetch after a server start fell back unless warmed (see discrepancies) |
| D-09 | Info | Closed (corpus `edc901fc`) | P-short prompts of 209+ tokens stored decode-completed chunks; P-short-v2 fixed the test |
| D-10 | Info | Open | After a restart, decode-completed chunks already in L2 are written again |
| D-11 | S3 | Open | Wrong RDMA GID index is not named at startup (T-CFG-06) |
| D-12 | S2 | Not reproduced on new server `046e8558d`; open on old server, not fixed by decision (server replaced) | Old server: multi-command pipelined fetch logs a late completion and disables the region; retrieves fall back |
| D-13 | Info | Open | L2-prefetched chunks are evicted from L1 right after the retrieve |
| D-14 | S2 | **Fixed in `e9cd0689`** | Two writers of one sharded key left a mixed object (about 14% of rounds); now per-write segment keys, create-only meta |
| D-15 | S2 | **Open** (not reproduced at E3 on the new driver; code finding stands) | Client teardown does not revoke remote RDMA access before L1 frees the slab |
| D-16 | Info | Open (vLLM upstream) | gpt-oss is not batch invariant across batch sizes under `VLLM_BATCH_INVARIANT=1` |
| D-17 | S1 | **Open, upstream [vllm#49250](https://github.com/vllm-project/vllm/issues/49250)** | `recompute` after a mid-forward layerwise load failure gives wrong tokens (V2 runner `num_computed_tokens`, async placeholders) |
| D-18 | Info | Open (worked around: `STOP_GRACE=60`) | Usage telemetry to `stats.lmcache.ai:8080` makes a clean shutdown take 14-17 s |
| D-19 | Security | Fixed in harness `adf9aa5f` | vLLM's TCPStore master listened on all interfaces; remaining host item `udp 0.0.0.0:4791` (rdma_rxe) |
| D-20 | S3 | Open, record-only (posted default) | Concurrent L2-only lookups of a shared prefix miss for all but one request; outputs exact |
| D-21 | S2 proposed (S1 arguable) | **Open**, record-only (posted default) | SIGKILL of LMCache during a layerwise retrieve can wedge vLLM's engine for good (GPU wait on a dead process's IPC event) |
| D-22 | S2 | **Open** | A lookup/prefetch in flight when LMCache dies is never failed; the request waits until the client gives up |
| D-23 | S3 | Open (harness waits 130 s) | After an engine stop, LMCache keeps the dead engine's KV memory mapped until the 120 s worker reap |
| D-24 | Info | Open (docs) | `vllm-load-failure.md` and plan section 7 omit that a generation timeout also stops the engine |
| D-25 | S3 | Open | Under L1 pressure an L1 store batch is all or nothing; refused chunks never reach L2 |

**S1/S2 open at exit:** D-17 (S1, vLLM), D-21 (S2 proposed, S1 arguable),
D-22 (S2), D-15 (S2, not reproduced on the new driver but unfixed in code),
D-01 (S2, accepted as by design for now). D-12 (S2) is open only against the
old server, which the new stack replaces.

## 4. Old stack vs new stack

- **Old stack:** kv-sink server `8.1.3.0-112-g512b0c207` (fencing), stock
  Aerospike C client 7.3.0, LMCache product `00cd3eee` (Stage 2/3) then
  `e9cd0689` (D-14 build, Stage 3b) ([`stage3/VERSIONS.md`](stage3/VERSIONS.md),
  [`stage2/VERSIONS.md`](stage2/VERSIONS.md)).
- **New stack:** kv-sink server `8.1.3.0-111-g046e8558d`, client
  `523d51ea`, LMCache `b6b0caae` (1a merge `e4701b9a`). Switched in on
  Lyndon's 22:00Z instruction (`state.json` note "NEW STACK (Lyndon 22:00Z)");
  the run book asks to "retest anything the switch affects, and mark it in
  the ledger" (control-tower `prompt.md`).

| Group | Tests |
| --- | --- |
| Run on the new stack, old result kept in the row's Notes | T-CFG-08, T-RDMA-06, T-PIPE-05, 06, 07, 11, 12, T-E2E-04, 05, 08 (pipelined half) |
| Rechecked on the new stack (plain path through client `523d51ea`) | T-E2E-01, 02, 03 (`stage3-newstack/e2e123/`) |
| First run on the new stack | T-PIPE-08, 10, T-E2E-09 (Stage 4); T-FLT-01, 05, 06, 07 (Stage 5); T-FLT-02, 03, 04 GPU halves, T-EVT-04, T-EVT-06 GPU half, T-SHR-01, 02, 03 (Stage 6 GPU) |
| Suites rerun on the merged tree, ledger rows still cite old evidence | E0/E1/E2 rows: `newstack` units 2,398 passed / 0 failed, `it1` 478 passed / 0 failed (every `test_aerospike_*` IT and the RDMA suite against server 3700), repeated with the GPU visible in `stage3-newstack/its/` ([`newstack/SUMMARY.md`](newstack/SUMMARY.md)) |
| Old stack only, affected by the switch, **not retested** | T-CFG-06 and T-CFG-07 (registration path changed with the new client); T-RDMA-05 and the kv-sink half of T-FLT-04's storage probe (no multi-node kv-sink on the new server; `kvsink_cluster.sh` is old-stack only); the 10 cluster ITs (skipped in `it1`: T-STO-08, T-FLT-10, T-FLT-02/03 and T-EVT-06 storage halves) |
| Old client only, plain path, not rerun | T-LKP-03, 04, 05, T-E2E-06, 07, T-E2E-08 plain half, T-E2E-10, T-ROC-02 E3 half (Stage 2, product `00cd3eee`). The plain path was exercised through the new client elsewhere (e2e123, T-FLT-01/06, Stage 6 CE runs) |
| Never run on the pipelined path on either stack | T-E2E-06, T-E2E-07 as rows ("Pipelined not run (Stage 3)"). T-SHR-01 did read P-shared `pipelined` at cap 64 on the new stack |

## 5. Gaps not testable on this box

| Gap | Tests | What closes it |
| --- | --- | --- |
| TP above 1 | T-LKP-05 TP half; T-E2E-11 runs at TP=1 | 8-GPU droplet (`gpu-mi300x8-1536gb`, plan section 3) |
| Real RDMA fabric | T-FAB-01, 05 (ConnectX); T-FAB-02, 03, 04 (EFA); T-CFG-02 EFA half; section-9 criterion 2 for all of 5.4/5.5/5.6 | E5a: hosts with ConnectX RoCE; E5b: AWS with EFA (SRD) |
| CDNA 4 | T-ROC-03 | MI350P (AMD partnership, plan section 8) |
| Packet loss | T-FLT-08 | E5a (netem on a real RoCE link); or the Soft-RoCE `tc netem` approximation with host-change approval (`flt08_netem.sh`, `cpu-prep-s6` result) |
| Real link-down | T-FLT-07 | E5a (down the port); here only with a new host-change approval: host-netns hairpin with a routing-policy change, or a netns-aware rdma_rxe ([`stage5/SUMMARY.md`](stage5/SUMMARY.md), G-13) |
| True multi-host E4 | T-SHR-*, T-EVT-06, T-FLT-02/03/04 ran as two LMCache + two vLLM on one MI300X | Two GPU hosts on a network; the pipelined half also needs N1 lifted (G-06), a product change, not hardware |
| Late write after re-lease | T-PIPE-07 E3 half | Not hardware: a kv-sink per-slot delay knob or a client counter of discarded stale-generation immediates (G-01) |

## 6. Open decisions for humans

"Posted default, no reply" means the control tower posted a default in
Slack and no reply is recorded in its notes. "Options only" means a worker
listed options in its result and no posted default is recorded.

| Decision | Options / default | State | Source |
| --- | --- | --- | --- |
| D-21 severity | Default opt 1: S2, record only. Alternative: S1 (engine failure not in section 7), which would require a fix before exit | Posted default, no reply (03:58Z) | `decisions/d21.txt`; thread "D-21 possible S1 engine hang" |
| T-FLT-05 expectation (layerwise on) | (1) accept the section-7 engine stop and amend the plan row; (2) connector reports failed blocks when the daemon is known dead; (3) also fix D-22 | Options only; Stage 5 end summary posted, no decision recorded | `gpu-stage5` result; [`stage5/SUMMARY.md`](stage5/SUMMARY.md) |
| T-EVT-04 status | (1) keep partial until D-17 is fixed (current ledger); (2) accept pass under `fail`, as for T-PIPE-05/06 | Options only; Stage 6a end summary posted, no decision recorded | `gpu-stage6a` result |
| D-25 | (1) record only (S3); (2) "Agent:" ask to evict on demand or grant partial L1 batches | Options only | `gpu-stage6a` result; [`stage6/SUMMARY.md`](stage6/SUMMARY.md) |
| D-18 telemetry | Set `LMCACHE_TRACK_USAGE=false` / `DO_NOT_TRACK=1` in the harness, or keep the 60 s grace (current) | Posted ("offered Agent: disable telemetry", 00:27Z), no reply | `state.json` notes; thread "UDP 4791 ... + telemetry ask" |
| UDP 4791 (rdma_rxe on 0.0.0.0) | Default: accept. Alternatives: `ufw deny 4791/udp`; unload rdma_rxe between sessions | Posted default, no reply | `state.json` thread list; [`stage3-newstack/SUMMARY.md`](stage3-newstack/SUMMARY.md) |
| gpt-oss prefix-hit oracle | Default: vLLM prefix cache at block 256 decides; block 16 reported | Posted default, no reply (20:00Z) | `decisions/gptoss-oracle.txt` |
| T-E2E-09 Llama only | Default: Llama only (gpt-oss not batch invariant, D-16) | Posted default, no reply (20:00Z) | `decisions/gptoss-oracle.txt` |
| T-E2E-09 verdict mode, D-20 | Default: `--pipelined-shared-keys wait` is the verdict; D-20 record only | Posted default, no reply | `state.json` notes "Stage4 defaults"; thread "Stage 4 summary + defaults" |
| T-FLT-07 stand-in | Mitigation 1 (veth/netns) **approved by Lyndon Bauto** 18:32Z, failed (rxe is init_net only); default opt 3, keep the stand-in (partial) | Stand-in: posted default, no reply (19:26Z); options 1/2 need a new approval | `decisions/flt07.txt` |
| T-FLT-08 | Default opt 3: N/A on this box. Opt 1: RoCE-only netem on an idle box; opt 2: all of `lo` | Posted default, no reply (21:18Z) | `decisions/flt08.txt` |
| D-17 fault-test policy | No fix for D-17/D-12 **approved by Lyndon** (21:00Z); running fault tests under `fail` is opt 1 | `fail` policy: posted default, no reply (21:44Z) | `decisions/d17.txt` |
| T-CFG-07 memlock | Warning + whole-object fallback (current) vs startup error (plan wording) | Asked in the D-12 thread, no reply recorded | `state.json` thread list; G-04 |

Already decided by a human (for reference): D-14 fix option 2 and pipelined
sub-option 1 (Lyndon, `decisions/d14.txt`); T-E2E-01 option 2 (Lyndon 17:19Z,
`decisions/e2e01.txt`); Stage 3 on a fencing server (Lyndon, `state.json`
notes); the new stack (Lyndon 22:00Z); vLLM loopback binding (Lyndon 23:00Z).

## 7. Recommended next steps

1. Fill in T-E2E-11 (section 2 placeholder, ledger row, section 4 group).
2. Get a human call on D-21 severity and the T-FLT-05 expectation; these
   two decide whether the LMCache side has an S1 to fix before exit.
3. D-17: backport vllm#53298 (or both parts of #49252) into the ROCm vLLM,
   then rerun the blocked recompute halves (T-PIPE-05/06/10, T-EVT-04 L2) and
   T-FLT-05 under `recompute`.
4. Product fixes for the open S2s: D-22 (fail in-flight lookups on degraded
   mode, or a lookup timeout), D-21 (bounded wait before queuing a GPU wait
   on an IPC event), D-15 (revoke remote access in driver shutdown).
5. Retest on the new stack what the switch affected (section 4): T-CFG-06/07
   probes and the 10 cluster ITs (CPU only), and record T-E2E-10's pipelined
   counts from Stage 3-newstack/Stage 4.
6. Book hardware for what this box cannot close: an 8-GPU host (TP), E5a
   (T-FAB-01/05, T-FLT-07/08, criterion 2), E5b (T-FAB-02..04), MI350P
   (T-ROC-03).
7. Confirm in writing whether server `046e8558d` meets the plan's "no fence"
   entry criterion (D-07 is recorded only against the old server).

## Discrepancies found

The ledger is taken as authoritative above; these are the places where
another source disagrees or is stale.

1. **Ledger intro vs rows.** The paragraph on the old kv-sink build says the
   pipelined rows "are now **not run**" and that the first fetch needs a
   warm-up; the rows carry new-stack results and the next paragraph says no
   warm-up is needed.
2. **D-08.** Ledger status Open; [`newstack/SUMMARY.md`](newstack/SUMMARY.md)
   says old issue 5 (lazy stripe registration) "is gone" on the new server.
3. **T-EVT-04.** Ledger: partial. [`stage6/SUMMARY.md`](stage6/SUMMARY.md):
   "T-EVT-04 passes under `fail`", with both halves marked pass in its table.
4. **T-FLT-04 / G-07.** [`OPEN-GAPS.md`](OPEN-GAPS.md) G-07 says T-FLT-04 is
   partial and "the client never re-registers"; the ledger says pass, and
   Stage 6 GPU (O-5) shows the client registering a new region on a restarted
   single-node kv-sink inside the fetch. G-07 still holds for multi-node.
5. **T-PIPE-11 / G-02.** G-02 says T-PIPE-11 is partial; the ledger says pass
   on the new stack (packet count matches the window-limited plan). The
   e2e08p "+5 half-chunks" remain unexplained.
6. **G-16.** OPEN-GAPS says retrieves from one vLLM never run concurrently;
   [`stage4/SUMMARY.md`](stage4/SUMMARY.md) says this is "only partly true"
   (one vLLM produced `reused` and `shared_keys_busy` in T-E2E-09).
7. **T-E2E-11 row notes.** Ledger: "model not downloaded; needs three kv-sink
   server processes". The `cpu-prep-s3s5` result says the 70B is downloaded
   (132 GB), and `cpu-prep-s6` says it uses one 64 GiB kv-sink node.
8. **T-E2E-10.** Ledger partial, "`pipelined` / `fell_back` counts need
   Stage 3"; Stage 3-newstack and Stage 4 recorded those counts, but the row
   was never re-judged.
9. **Stage 6 CPU-half table** still shows T-FLT-10 fail and T-FLT-02/03/04,
   T-EVT-06 partial without marking them superseded by `d14/` and the
   Stage 6 GPU half; the ledger has the current status.
10. **D-14 sub-option timing.** The design record says sub-option A was chosen
    at 19:30Z; `decisions/d14.txt` says 19:43Z. No effect on results.
