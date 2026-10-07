# Functional testing: open gaps

Gaps found while running the
[functional test plan](../docs/design/v1/layerwise/functional-test-plan.md)
that need a later product, server or test change. A gap is either something
a test could not check on this box, or a defect whose fix is out of scope for
the functional campaign. The [ledger](LEDGER.md) keeps each test's status; this
file says what is missing and what would close it.

How to read an entry:
- **Tests / defects**: ledger test IDs and `D-xx` rows it touches.
- **Missing**: what is not checked or not fixed today.
- **Why not now**: the blocker (missing knob, server bug, hardware, scope).
- **Proposed change**: the smallest change that would close the gap.
- **Owner**: Track A (Aerospike adapter), Track B (RDMA context and
  consumption), Track C (layerwise planner and pipelined path), the Aerospike
  server team, vLLM upstream, or "unassigned".
- **Evidence**: box paths are relative to `/root/lmc-work/functional/`;
  commits are on `prototype-stage1`.

D-14 (concurrent writers mix segments) has its own decision record:
[`aerospike_concurrent_writes.md`](../docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md).

| ID | Gap | Tests / defects | Owner |
| --- | --- | --- | --- |
| [G-01](#g-01-late-write-after-re-lease-cannot-be-forced-end-to-end) | Late write after re-lease cannot be forced end to end | T-PIPE-07 | Aerospike server team; Track B |
| [G-02](#g-02-no-log-of-the-pipelined-fetch-plan) | No log of the pipelined fetch plan | T-PIPE-11 | Track C |
| [G-03](#g-03-fault_inject-turns-pipelining-off) | `fault_inject` turns pipelining off | T-FLT-01, T-FLT-05 to 07, T-PIPE-05/06 | Unassigned |
| [G-04](#g-04-memlock-shortfall-warns-instead-of-failing-startup) | Memlock shortfall warns instead of failing startup | T-CFG-07 | Owner question |
| [G-05](#g-05-wrong-gid-index-is-not-named-at-startup) | Wrong GID index is not named at startup | T-CFG-06, D-11 | Track B |
| [G-06](#g-06-lmcache-refuses-multi-node-rdma) | LMCache refuses multi-node RDMA | T-RDMA-05, T-FLT-04, T-E2E-11 | Track A / B |
| [G-07](#g-07-no-re-registration-after-an-aerospike-node-restart) | No re-registration after an Aerospike node restart (multi-node only; single node re-registers on the new stack) | T-FLT-04 | Track B; Aerospike server team |
| [G-08](#g-08-l2-prefetched-chunks-are-evicted-from-l1-right-away) | L2-prefetched chunks are evicted from L1 right away | D-13, T-LKP-03 | L1 / prefetch controller |
| [G-09](#g-09-l2-lookup-reads-keys-after-a-gap) | L2 lookup reads keys after a gap | T-LKP-03 | Unassigned (L2 lookup) |
| [G-10](#g-10-client-teardown-leaves-l1-writable-by-the-server) | Client teardown leaves L1 writable by the server | D-15 | Track B |
| [G-11](#g-11-server-deregister-does-not-drain-in-flight-writes) | Server deregister does not drain in-flight writes | D-15 (proposed server issue 12) | Aerospike server team |
| [G-12](#g-12-multi-command-pipelined-fetch-breaks-the-region) | Multi-command pipelined fetch breaks the region | D-12, T-RDMA-06, T-E2E-04 | Aerospike server team |
| [G-13](#g-13-rdma-link-down-needs-a-dedicated-link) | RDMA link-down needs a dedicated link | T-FLT-07 | Harness; new host-change approval needed |
| [G-14](#g-14-gpt-oss-is-not-batch-invariant-across-batch-sizes) | gpt-oss is not batch invariant across batch sizes | T-E2E-09, D-06 | vLLM upstream; test plan |
| [G-15](#g-15-no-per-request-byte-oracle-for-concurrent-retrieves) | No per-request byte oracle for concurrent retrieves | T-E2E-09 | Test plan / harness; Track C |
| [G-16](#g-16-retrieves-of-distinct-keys-from-one-vllm-never-run-concurrently) | Retrieves of distinct keys from one vLLM never run concurrently | T-PIPE-08, T-PIPE-10, T-E2E-09 | Test plan |

## G-01 Late write after re-lease cannot be forced end to end

- **Tests / defects**: T-PIPE-07 (partial).
- **Missing**: an E3 run where a write from an abandoned fetch lands *after*
  its window is leased to the next fetch, with proof that it was not credited.
- **Why not now**: the race needs RDMA writes delayed while the TCP reply goes
  through. The kv-sink server has no per-slot delay, and the client drops a
  stale-generation immediate silently, so nothing shows that it happened.
- **Proposed change**: either a server debug knob that delays the writes of a
  chosen slot, or a client counter of discarded stale-generation immediates
  (the counter is cheaper and useful in production).
- **Owner**: Aerospike server team (knob) or Track B (counter).
- **Evidence**: E1 half in `day1/step2/rdma_suite.txt`, `day1fix/batch1/junit.xml`;
  plan for E3 in `functional/stage3/HARNESS.md` (`pipe07` partial).

## G-02 No log of the pipelined fetch plan

- **Tests / defects**: T-PIPE-11 (pass on the new stack; the ledger row
  passes on packet counts).
- **Status (2026-10-02, new stack)**: the packet count of the
  separate-object-groups pipe11 run matches the window-limited plan exactly
  (10,530 = 18 full layers × 4 chunks + 18 sliding layers × the 128-token
  window, ×1.016 acks), so T-PIPE-11 passes
  ([`stage3-newstack/SUMMARY.md`](stage3-newstack/SUMMARY.md), "Packet
  counts"). The gap stays open for what packet counts cannot show: which
  slots each layer fetched, and the 5 extra half-chunks in the one-group
  e2e08p run (217,620 packets).
- **Missing**: a direct check that sliding-window layers of gpt-oss fetch only
  the chunks inside their window.
- **Why not now**: the planner emits no plan. The Stage 3 harness infers it
  from `rxe0` packet counts, which shows volume but not which slots.
- **Proposed change**: a DEBUG log per layer of the planned `(chunk, slot)`
  set, or a counter of planned vs skipped slots per layer kind.
- **Owner**: Track C.
- **Evidence**: `day1fix/e2e/lmcache_gptoss_staging.log`; `stage3.sh pipe11`.

## G-03 `fault_inject` turns pipelining off

- **Tests / defects**: T-FLT-01, T-FLT-05, T-FLT-06, T-FLT-07 at E3;
  T-PIPE-05 and T-PIPE-06 end to end.
- **Missing**: failure injection on the pipelined path through the product
  adapter stack.
- **Why not now**: the `fault_inject` adapter does not forward the pipelined
  methods of the RDMA adapter it wraps, so the wrapped stack loads whole
  objects. E0 fakes cannot be enabled in a real server. Stage 5 injects
  faults by deleting records, SIGSTOP/SIGKILL of processes and
  `max_record_bytes` instead.
- **Proposed change**: forward the pipelined interface through `fault_inject`
  and add fault points on it (drop a slot, delay a layer).
- **Owner**: unassigned (`fault_inject` adapter).
- **Evidence**: `stage5/dry5.txt`; `functional/stage5/SUMMARY.md`.

## G-04 Memlock shortfall warns instead of failing startup

- **Tests / defects**: T-CFG-07 (pass under current behaviour).
- **Missing**: a decision. The plan says "startup error names memlock"; the
  product logs a WARNING naming `RLIMIT_MEMLOCK` and the byte count, then
  loads whole objects.
- **Why not now**: it is a policy choice, not a bug. A warning keeps a
  misconfigured host serving (slower); an error makes it visible at once.
- **Proposed change**: owner picks one. If "error", raise at startup; if
  "warning", amend the plan's pass rule.
- **Owner**: owner question (Track B proposes).
- **Evidence**: `item2/logs/cfg07_memlock16m.txt`, `item2/logs/cfg07_control.txt`.

## G-05 Wrong GID index is not named at startup

- **Tests / defects**: T-CFG-06 (fail), D-11 (S3).
- **Missing**: with GID index 0 on Soft-RoCE `lo`, startup succeeds and the
  only message is `kv-sink-register failed: queue pair RTS failed`; retrieves
  load whole objects.
- **Why not now**: fix not chosen; out of scope for the functional campaign.
- **Proposed change**: (a) reject at startup a link-local GID derived from an
  all-zero MAC (`fe80::200:ff:fe00:0`), naming `gid_index` and the GID; (b)
  add `gid_index` and the GID hex to the registration warning; or both.
- **Owner**: Track B.
- **Evidence**: `item2/logs/cfg06_gid0.txt`, control `item2/logs/cfg06_gid1.txt`.

## G-06 LMCache refuses multi-node RDMA

- **Tests / defects**: T-RDMA-05 (pass at fanout level), T-FLT-04, T-E2E-11.
- **Missing**: the adapter registering with and fetching from more than one
  kv-sink node.
- **Why not now**: by design (N1, plan section 7): startup logs
  `pipelined fetches need a single-node cluster, but the cluster has 3 nodes`
  and loads whole objects. The fanout was tested directly with
  `kv_sink_fanout_probe` (3/3 nodes, QPs to RTS).
- **Proposed change**: lift N1 in the adapter: call `register_all_nodes` and
  route each slot to its record's node.
- **Owner**: Track A / Track B.
- **Evidence**: `stage6/logs/rdma05_fanout.txt`, `stage6/logs/rdma05_adapter_probe.txt`;
  commit 862e8289.

## G-07 No re-registration after an Aerospike node restart

- **Tests / defects**: T-FLT-04 (pass in the ledger; this gap is the
  multi-node case only).
- **Status (2026-10-02, new stack)**: on a single-node kv-sink the gap is
  closed. In Stage 6 GPU (`flt04k`, O-5) the server restarted under a live
  LMCache and the reread stayed `pipelined` and exact: the client registered
  a new region on the restarted server inside the fetch, after 48 sub-read
  errors ([`stage6/SUMMARY.md`](stage6/SUMMARY.md), GPU half; ledger
  T-FLT-04). What follows still holds for a multi-node kv-sink cluster,
  which LMCache refuses for pipelined fetches anyway (N1, G-06), and was
  shown only on the old server (`kvsink_cluster.sh`).
- **Missing**: a path that notices a restarted node and registers L1 with it
  again. On the old 3-node kv-sink cluster the restarted node had dropped the
  region (deregister returns `no such region`) and the client never
  re-registered.
- **Why not now**: `register_all_nodes` cannot run twice on one `RdmaContext`
  (`queue pair already exists`); a fresh context registers 3/3. The design
  ("Still open: registration lifecycle on node restart" in
  `aerospike_rdma.md`) waits on the server team to say whether in-flight
  fetches against a stale region are dropped or completed.
- **Proposed change**: once the server answers, per-node QP teardown and
  re-registration triggered by a region error or a node generation change.
- **Owner**: Track B; Aerospike server team (protocol answer).
- **Evidence**: `stage6/logs/flt04_kvsink_probe.txt`.

## G-08 L2-prefetched chunks are evicted from L1 right away

- **Tests / defects**: D-13 (Info), T-LKP-03.
- **Missing**: L1 locality after an L2 hit. A repeat request reads the same
  chunks from L2 again, and L1 copies of later chunks go unused because the
  L1 lookup counts a leading run from chunk 0.
- **Why not now**: not a correctness issue; needs the L1/prefetch owner to say
  whether this is intended.
- **Proposed change**: keep L2-loaded chunks in L1 under normal LRU instead of
  freeing them after the retrieve.
- **Decision (2026-10-06)**: keep `default` as the default. Deployments that
  want L1 locality set `--l2-prefetch-policy retain`, documented in
  `docs/design/v1/distributed/l2_adapters/aerospike_rdma.md` ("Keeping
  fetched chunks in L1"). On the pipelined path a retained chunk lives only
  until its RDMA window is leased again.
- **Owner**: L1 / prefetch controller.
- **Evidence**: `stage2/lkp03/`.

## G-09 L2 lookup reads keys after a gap

- **Tests / defects**: T-LKP-03 (pass; not filed as a defect).
- **Missing**: the L2 lookup reads every key of a request, then uses only the
  leading run of hits. Each key after the first miss is a wasted read.
- **Why not now**: cost only, found in Stage 2b; no owner yet.
- **Proposed change**: stop at the first miss (sequential batches), or keep
  one batch but skip the reads of keys past the first miss when results
  arrive in order.
- **Decision (2026-10-06): won't fix.** The Aerospike lookup is one
  header-only batch read of every key (`do_batch_exists` in
  `connector.cpp`, no bins), so a key past the gap costs a metadata check
  inside the same round trip. Sequential batches would add round trips to
  every lookup. Stopping at the first miss would also break `SPARSE`
  (sliding-window) lookups and multi-adapter prefetch policies, which use
  hits after a gap.
- **Owner**: unassigned (L2 lookup in the storage manager).
- **Evidence**: `stage2/lkp03/` (chunk-2 gap: chunks 3-5 looked up, not used).

## G-10 Client teardown leaves L1 writable by the server

- **Tests / defects**: D-15 (S2, teardown only).
- **Missing**: `AerospikePipelinedRdmaDriver::shutdown()` sends
  `kv-sink-deregister` but keeps the window MR and QPs alive until the C++
  object is destroyed. `L1Manager.close()` then frees the slab, and a
  server write from an abandoned fetch can land in freed memory. On a cold
  server this is the kv-sink warm-up smoke crash.
- **Why not now**: product fix, out of scope for the campaign. Production L1
  is a shm segment freed only at exit, so a running server is not corrupted;
  it becomes S1 if L1 memory is ever recycled in-process.
- **Proposed change**: `RdmaContext::revoke_remote_access()` (QPs to
  `IBV_QPS_ERR`, `ibv_dereg_mr` of the window MRs, keep PD/CQ/scratch MR),
  called from `shutdown()` after `deregister_all_nodes`; skip
  `IBV_WC_WR_FLUSH_ERR` in `poll_notifications`; document in
  `L2AdapterInterface.close()` that no remote peer writes to L1 after it
  returns.
- **Owner**: Track B.
- **Evidence**: test `0ebf98d0` (fails 7 of 9 runs on a cold server);
  `functional/stage3/crash/FINDINGS.md` (`5b1fa9fe`); box `stage3/crash/`.

## G-11 Server deregister does not drain in-flight writes

- **Tests / defects**: D-15 (server half); proposed server issue 12, related
  to issues 4 and 5.
- **Missing**: `kv-sink-deregister` only sets `teardown_pending` on the
  reference-counted region and replies at once; an abandoned fetch still
  posts its writes afterwards.
- **Why not now**: server code; the server is being migrated, so the campaign
  does not dig further.
- **Proposed change**: deregister waits for (or cancels) every in-flight write
  on the region before replying, or the reply says writes may still land.
  The client fix in G-10 is needed either way.
- **Owner**: Aerospike server team.
- **Evidence**: as G-10.

## G-12 Multi-command pipelined fetch breaks the region

- **Tests / defects**: D-12 (S2), T-RDMA-06, T-E2E-04 (both pass on the new
  stack).
- **Status (2026-10-02)**: old server `512b0c207` only. D-12 does not
  reproduce on server `046e8558d`: cap-64 runs stayed `pipelined` with 0 late
  completions or region errors ([`stage3-newstack/SUMMARY.md`](stage3-newstack/SUMMARY.md);
  ledger D-12). Not fixed on the old server, by decision (server replaced).
- **Missing**: pipelined fetches over 256 slots (more than 4 Llama-3.1-8B
  chunks). The server splits them into several commands; it intermittently
  logs `late completion for slot N`, then `region N in error state`, and the
  region stays disabled (issue 8).
- **Why not now**: server bug in `kv_sink_verbs.c`: `imm_reap_closed` stays
  set between commands, so the posting path's reap treats the next command's
  completions as late and frees tokens still in use (use-after-free).
- **Proposed change**: clear the reap flag before posting, or keep reap state
  per command (as proposed for issue 7). Until then, long-prompt pipelined
  tests run once at the default cap (to record D-12) and once with
  `--pipelined-max-chunks 4`.
- **Owner**: Aerospike server team.
- **Evidence**: `item2/logs/rdma06_try1.txt`, `rdma06_try2.txt`,
  `rdma06_threshold*.txt`; `stage3/logs/rdma06/`.

## G-13 RDMA link-down needs a dedicated link

- **Tests / defects**: T-FLT-07.
- **Missing**: taking only the RDMA path down for 2 s during a fetch.
- **Why not now**: `rxe0` is bound to `lo` (GID 127.0.0.1); its packets bypass
  `lo` qdiscs and netfilter, and downing `lo` or `rxe0` breaks every other
  service.
- **History**: the default was a stand-in (kv-sink frozen 2 s, test marked
  partial), which exercises the same timeout, quarantine and reuse path
  through a server stall. Lyndon Bauto approved the host change on
  2026-10-01 18:32Z ("mitigation 1"): a veth pair with one end in its own
  netns and an rxe device on each end. **Probed the same day: it does not
  work on this box.** The v6.11 rdma_rxe opens its UDP 4791 socket and does
  its route lookups only in the initial netns (`rxe_net.c`: `init_net`). The
  rxe device inside the netns therefore gets nothing: rxe1 to rxe2
  `ibv_rc_pingpong` timed out, and the netns counted 65 `UdpNoPorts`. The
  probe was torn down; `stage5/CHANGES.md` has the log.
- **Still open (stand-in still needed)**: the working variants need a new
  approval:
  (1) both rxe devices in the host netns with the netns as a wire, plus a
  host routing-policy change (move the `local` fib rule after two `ip rule
  from/to` rules; set `accept_local` on the two veths);
  (2) a newer, netns-aware rdma_rxe (rmmod, rxe0 recreated between GPU work
  items).
  Either way, LMCache needs a container that sees `/dev/infiniband/uverbs1`
  (`lmc-c` maps only uverbs0).
- **Proposed change**: option 1 or 2 above if a human approves; otherwise keep
  the stand-in and run T-FLT-07 on a real fabric (E5a) by downing the port.
- **Owner**: harness (Stage 5); a human decides on the host change.
- **Evidence**: `functional/stage5/CHANGES.md`, `functional/stage5/SUMMARY.md`,
  `functional/stage5/flt07_netns_probe.sh`; box
  `stage5/flt07_probe/pingpong_netns.txt`.

## G-14 gpt-oss is not batch invariant across batch sizes

- **Tests / defects**: T-E2E-09 (for gpt-oss-120b), related to D-06.
- **Missing**: a token-equality oracle for concurrent gpt-oss runs. In Stage
  2c, with `VLLM_BATCH_INVARIANT=1` and no LMCache, the baseline at
  concurrency 8 matched batch size 1 on only 79 of 130 requests.
- **Why not now**: vLLM's batch-invariant kernels do not cover gpt-oss across
  batch sizes; nothing on the LMCache side can fix it.
- **Proposed change**: for gpt-oss, T-E2E-09 uses the plan's fallback oracle
  (byte oracle on the KV each request received, plus top-1 logprob agreement
  of at least 99.9% with the baseline), or compares against a baseline run at
  the same concurrency. Llama keeps token equality.
- **Owner**: vLLM upstream (kernels); test plan (oracle).
- **Evidence**: `stage2/gptoss_ref/session_base_b16.txt` (Stage 2c, in
  progress).

## G-15 No per-request byte oracle for concurrent retrieves

- **Tests / defects**: T-E2E-09 (plan section 2: "the KV bytes each request
  received must equal the bytes stored for its keys"); G-14.
- **Missing**: a check of the bytes one request actually received.
- **Why not now**: LMCache has no hook for it. `POST /cache/checksums` hashes
  GPU KV blocks by block id, but the harness cannot learn a request's block
  ids, and under concurrency those blocks are reused. Pipelined bytes go
  from the RDMA window straight to the GPU, so no L1 object is left to
  checksum afterwards. The T-RDMA-06 byte-oracle test covers P-exact keys
  only.
- **Proposed change**: (1) test-only, transport half: an `RDMA_ORACLE_SETS`
  option on `test_aerospike_rdma_byte_oracle_integration.py`, run after
  `stage4.sh e2e09` over the P-shared and P-multi records (about 20 lines);
  (2) full oracle: an env-gated debug log of each retrieved chunk's MD5
  after the H2D copy, compared with a plain get. Until then, Llama's
  T-E2E-09 uses token equality (batch invariant, checked by `stage4.sh
  ref16`) plus top-1 agreement.
- **Owner**: test plan / harness (1); Track C (2).
- **Evidence**: `functional/stage4/HARNESS.md` (oracle decision).

## G-16 Retrieves of distinct keys from one vLLM never run concurrently

- **Tests / defects**: T-PIPE-08 (pass), T-PIPE-10 (partial, for D-17), T-E2E-09.
- **Status (2026-10-02, new stack)**: only partly true; see the update at
  the end of this entry and [`stage4/SUMMARY.md`](stage4/SUMMARY.md)
  ("G-16 is only partly true"). Distinct keys serialize (`pipe08s`); a
  shared prefix does overlap within one vLLM (T-E2E-09: `reused`,
  `shared_keys_busy`). The ledger rows are run with two vLLMs as below.
- **Missing**: window exhaustion (`refused`) from a single vLLM instance.
- **Why not now**: for distinct keys this is by design. `RETRIEVE` is a blocking handler on
  the affinity pool, keyed by the ZMQ client identity, so one vLLM (TP=1)
  runs its retrieves one at a time. Each pipelined fetch holds and releases
  its window inside its own retrieve.
- **Proposed change**: none to the product. Stage 4 runs these two tests
  with two vLLM instances against one LMCache server started with
  `--max-gpu-workers 2` (`stage4.sh pipe08`, `pipe10`); `pipe08s` records
  the single-instance behaviour. Worth stating in `c9-wiring.md`: with one
  engine per server, `window_count` above 1 only helps when there are
  several engines or TP ranks.
- **Owner**: test plan (Track C for the doc note).
- **Evidence**: `functional/stage4/HARNESS.md`.
- **Update (Stage 4 GPU, 2026-10-02, new stack)**: true only for distinct
  keys (`pipe08s`: 0 `refused` at `window_count` 2). In T-E2E-09 one vLLM
  produced `reused` and `shared_keys_busy` when P-multi turns that share a
  prefix were in flight together, so same-prefix overlap does not need a
  second engine (`functional/stage4/SUMMARY.md`).
