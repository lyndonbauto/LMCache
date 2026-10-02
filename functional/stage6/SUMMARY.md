# Stage 6 prep: the E4 cluster and its storage-level tests

E4 (functional test plan section 3) needs three Aerospike nodes on this one
host. This stage builds that cluster, a matching 3-node kv-sink server
cluster, and runs the cluster tests that need no GPU through LMCache's
Aerospike adapter (no vLLM). The GPU half of Stage 6 (two vLLM + LMCache
hosts, the 70B pass) runs later on the same harness.

## Harness

```bash
# CE 8.2 cluster: aero-n1..3, 127.0.0.1:3300 / 3310 / 3320
functional/harness/cluster.sh start 2        # rf 1 or 2 (3 works too); waits for size 3 + no migrations
functional/harness/cluster.sh kill-node 2    # SIGKILL, waits for the 2-node cluster to settle
functional/harness/cluster.sh restart-node 2 # waits for size 3 + migrations done
functional/harness/cluster.sh status | stop | wipe

# kv-sink cluster: 3 asd in aero-kvsink, 127.0.0.1:3400 / 3410 / 3420
functional/harness/kvsink_cluster.sh start 1 # then warms every node (server issue 5)
functional/harness/kvsink_cluster.sh kill-node N | restart-node N | status | stop

# tests in lmc-c drive node kills through a host-side daemon
nohup setsid functional/harness/cluster_ctl_daemon.sh >> ctl.log 2>&1 &   # or scripts/start_ctl.sh
scripts/run_cluster_it.sh <log-name> [pytest -k expr] [race rounds]
```

"Settled" means every running node reports the expected `cluster_size` and
`migrate_partitions_remaining=0` on three polls in a row. A single good poll
can come before migrations have started. Configs are rendered from
`functional/configs/cluster/*.template`. Each CE node keeps a sparse 16 GiB
device file under `/root/lmc-work/aero-cluster/nN/`, because the 5 TiB scratch
disk is not mounted. Each kv-sink node has 2 GiB of memory, which keeps
stripe registration short.

## CPU half

All results are CPU only, run from `lmc-c` against the cluster through
`AerospikeL2AdapterConfig(hosts="127.0.0.1:3300,127.0.0.1:3310,127.0.0.1:3320")`.
The tests are `tests/v1/distributed/test_aerospike_cluster_integration.py`
(gated on `RUN_AEROSPIKE_CLUSTER_INTEGRATION=1`) and
`tests/v1/distributed/rdma/csrc/kv_sink_fanout_probe.cpp` (`make fanout-probe`).
Logs are under `logs/` on the box.

**Superseded (2026-10-02):** this table is the CPU half as run on
2026-10-01; the current status of each row is in the [ledger](../LEDGER.md).
T-FLT-10 passes after the D-14 fix (`e9cd0689`, [`d14/SUMMARY.md`](../d14/SUMMARY.md)),
and T-FLT-02/03/04 and T-EVT-06 pass with their GPU halves (section "GPU
half, part A" below). The CPU half's Defects and Next steps are superseded
the same way: D-14 is fixed in `e9cd0689`, and Stage 6 GPU has run.

| Test ID | What | Result | Evidence (`logs/`) |
|---|---|---|---|
| T-STO-08 | RF 2, commit level all: a store returns only with its replica; reads survive one node down | **pass** | `sto08.txt`, `cluster_it_full.txt`, `sto08_sensitivity.txt` |
| T-FLT-02 | Kill a node mid-fetch, RF 1 | **partial: storage half pass**; end to end with vLLM in Stage 6 GPU | `flt02_03.txt`, `flt02_rf1_flushwait_{1,2,3}.txt` |
| T-FLT-03 | Same at RF 2: reads fail over to the replica | **partial: storage half pass** | `flt02_03.txt`, `cluster_it_full.txt` |
| T-FLT-04 | Node restart | **partial**: CE half pass; kv-sink half pass at the fanout level (registration dropped by the restarted node, re-registration on a fresh context); the adapter path can't run on 3 nodes (N1) | `flt02_rf1_flushwait_*.txt`, `flt04_kvsink_probe.txt`, `rdma05_adapter_probe.txt` |
| T-FLT-10 | Two engines store the same chunk at once, 1,000 times | **fail (S2, D-14)**: sharded chunks mixed in 141/1000 and 145/1000 rounds; inline chunks 0/1000 twice | `flt10.txt`, `cluster_it_full.txt` |
| T-EVT-06 | Client LRU off, two hosts share the cluster | **partial: storage half pass** (two storage managers in two processes); vLLM hosts in Stage 6 GPU | `evt06b.txt`, `cluster_it_full.txt` |
| T-RDMA-05 | Per-node `kv-sink-register` fanout on a real cluster | **pass** at the fanout level: 3/3 nodes, one region per node, every QP to RTS. LMCache's adapter does not call the fanout on more than one node (N1) | `rdma05_fanout.txt`, `rdma05_adapter_probe.txt` |

Details:

- **T-STO-08.** After each of 40 stores (20 sharded, 20 inline: 120
  records), the cluster's replica count had grown exactly as much as its
  master count. The check is sensitive. With the plain client, commit level
  `master` left the replica count behind in 97 of 2,000 puts, and `all`
  (what `connector.cpp` sets) in 0 of 2,000. With n2 SIGKILLed, all 60 keys
  loaded byte-exact; after it rejoined and migrations finished, all 60 again.
- **T-FLT-02 (RF 1).** A reader thread loaded 60 keys in a loop while n3 was
  SIGKILLed. Over 5 runs, about 26k–42k loads per run all ended byte-exact
  or as a clean miss. There was never a wrong byte, a hang or an exception.
  With n3 down, 33–44 of 60 keys missed; a sharded object misses if any of
  its segments lived on n3, so more than a third miss. The rest loaded.
  Ten new keys stored and loaded fine with n3 down.
- **T-FLT-04, CE half.** After n3 restarted from its device file, all 70
  keys loaded byte-exact in all 3 runs with a 2.5 s pause after the last
  store, and in 1 of 2 runs without it. The other run without the pause
  lost the 3 keys stored last (56, 58, 59). Each was a clean miss; see O-1.
- **T-FLT-03 (RF 2).** While n1 was SIGKILLed, about 10.6k loads ran in the
  roughly 12 s window between the kill and the settled 2-node cluster:
  **0 misses**, all byte-exact (2 runs). All keys were byte-exact after
  settling and after the rejoin.
- **T-FLT-10.** Two writer processes, each with its own adapter, were
  released together on one key, with words `round<<21 | writer<<20 | i`.
  A third adapter read the key after both stores returned. Sharded (3 MiB:
  4 segments of 768 KiB plus a meta record): writer 1 won 456 and 371
  rounds, writer 2 won 403 and 484, and **141 and 145 rounds were mixed**,
  always whole segments from each writer (196,608 words = one segment) and
  never a word from neither. Inline (64 KiB, one record): 0 mixed in
  2 × 1,000 rounds. See D-14.
- **T-EVT-06.** Two processes each ran a `StorageManager`. Each stored 24
  shared keys and 24 keys of its own through `reserve_write`/`finish_write`,
  with no L2 capacity, so client LRU was off. Afterwards all 72 keys were
  present and byte-exact. Contrast run: host A's L2 LRU was on with 8 MiB
  for 24 MiB of data. A deleted **all 24 shared keys**, which B had also
  stored, and 18–19 of its own, but **none of B's own 24**. A host only
  deletes keys it stored itself, but for a shared prefix that includes
  entries another host relies on. This is why T-EVT-06 asks for client LRU
  off.
- **T-RDMA-05.** `kv_sink_fanout_probe` runs the production `RdmaContext`
  and `register_all_nodes()` against the kv-sink cluster on rxe0 GID 1. It
  got `registered=3 of 3, failures=0`, a node-scoped region per node (for
  example B1=5, B2=2, B3=5), a separate server QPN per node, and every queue
  pair reached RTS. Deregistration released all 3. Through the adapter
  (`rdma05_adapter_probe.txt`, item 2's `cfg_probe.py` pointed at 3400),
  startup succeeds and then logs `pipelined fetch unavailable, retrieves
  will load whole objects: … pipelined fetches need a single-node cluster,
  but the cluster has 3 nodes`. That is the documented N1 limitation
  (plan section 7), checked before any registration.
- **T-FLT-04, kv-sink half.** The probe registered on 3 nodes, then n2 was
  restarted and warmed. Deregistration failed only on B2 (`no such region`),
  which confirms the restarted node dropped its registration. Registering
  again on the same `RdmaContext` failed on every node with `queue pair
  already exists for node 'Bn'`. A fresh context and memory region
  registered 3/3 and reached RTS. LMCache has no node-restart
  re-registration today; that is intentionally open
  (`aerospike_rdma.md`, "Still open: registration lifecycle on node
  restart"). Any future path needs a new context, or per-node queue pair
  teardown.

## Defects

| ID | Severity | Defect | Cause |
|---|---|---|---|
| D-14 | S2 | Two writers storing the same sharded key at the same time leave a mixed object: the meta record says present and the segments come from both writers (about 14% of rounds). A reader during a rewrite can see the same | `do_single_set` writes segments under fixed keys `<key>\|s\|<i>` with `AS_POLICY_EXISTS_IGNORE` and then the meta record. Nothing ties a meta record to the segment set it describes. A create-only meta write (the plan's Track A blocker) would **not** fix this alone: the losing writer has already overwritten segments of the winner's committed object before its meta write fails |

Why S2 and not S1: two engines storing one key compute the same tokens on
the same model, so the two payloads are equal or close (differing only
without batch invariance). Each mixed segment is a whole segment of one
valid write. No cross-tenant data and no corruption inside a segment was
seen.

Observations (not defects):

- **O-1 (Info, Aerospike behavior).** CE with `commit-to-device false`
  buffers device writes for up to `flush-max-ms` (1000 ms). A SIGKILLed
  node at RF 1 loses up to its last second of writes; LMCache sees clean
  misses. The RF 1 test now waits 2.5 s after storing. At RF 2 the replica
  covers it.
- **O-2 (Info).** CE 8.2 ignores the `info` stanza ("info service is
  obsolete"), so 3303/3313/3323 never open. `aerospike-ce`'s 3003 is the
  same.
- **O-3 (expected, plan section 7).** Pipelined RDMA is refused on a cluster
  of more than one node (N1), so no pipelined E4 test can run end to end
  until N1 lands.

## Next steps

1. D-14: Track A decides the fix (see the proposal in the work result). After
   it lands, rerun `-k racing` with 1,000 rounds.
2. Stage 6 GPU: T-FLT-02/03/04 and T-EVT-06 end to end with two vLLM +
   LMCache hosts on `cluster.sh`. Wait 2.5 s or more after the last store
   before a RF 1 kill (O-1).
3. T-RDMA-05 end to end and the kv-sink half of T-FLT-04 through the
   adapter: blocked on N1 (multi-node pipelined plans) and on the server
   team's answer about in-flight fetches after a restart.

## GPU half, part A: two vLLM hosts on the new kv-sink stack

**Outcome: pass, with no wrong token in any run.** T-SHR-01/02/03, the GPU halves of
T-EVT-06 and T-FLT-02/03/04, and the kv-sink restart (`flt04k`) pass. T-EVT-04 passes
under `fail`; its recompute half (a mid-forward pipelined load failure) is blocked by
D-17. One new S3 finding (D-25: an L1 batch write is all or nothing, and refused chunks
never reach L2). Worker `gpu-stage6a`, 2026-10-02 05:22-07:00 UTC (about 1.6 GPU-hours).
T-E2E-11 (70B) and T-FLT-08 are separate items and were not run. Versions are in
[`VERSIONS.md`](VERSIONS.md) and host changes in [`CHANGES.md`](CHANGES.md). The
harness is [`stage6gpu.sh`](stage6gpu.sh) (run with `launch.sh`); see
[`HARNESS-GPU.md`](HARNESS-GPU.md) for what changed for the new stack.

Setup: "host B" is a second LMCache server (127.0.0.1:6556) and a second vLLM (8001)
on the same MI300X, both at `--gpu-memory-utilization 0.3`, which fit. L2 is either the
single-node batch-read kv-sink (`aero-kvsink-bp`, 3700, pipelined at LMCache's
default cap 64) or the 3-node CE 8.2 cluster (`aero-n1..3`, plain path).
Llama-3.1-8B, `VLLM_BATCH_INVARIANT=1`, layerwise on, async scheduling on. Evidence is
on the box under `/root/lmc-work/functional/stage6/gpu/<section>/`. The `run2_*` and
`run3_*` directories hold superseded attempts (see the notes).

| Test ID | What | L2 | Policy | Result | Evidence |
|---|---|---|---|---|---|
| T-SHR-01 | B (fresh L1) reads A's P-exact-10..14 and P-shared | kv-sink | fail | **pass**: 30/30 exact; B's 15 retrieves `pipelined`, full-prefix hits; B read 1,300 + 5,200 batch sub-records | `shr01/*_kvsink*` |
| T-SHR-01 | same | CE RF 1 | fail | **pass**: 30/30 exact; B's 15 retrieves full-prefix L2 hits (`not_deferred`) | `shr01/*_cluster*` |
| T-SHR-02 | Both hosts store P-exact-00..14 at once, 3 rounds, then both read back | kv-sink | fail | **pass**: `l2_integrity.py whole` PASS every round (35 objects, 2,275 records, 0 not whole, 0 orphans, 0 missing); 1 lost create-only race per round; read-back 30/30 exact, `pipelined` | `shr02/*_kvsink*` |
| T-SHR-02 | same | CE RF 2 | fail | **pass**: same integrity verdicts every round; 1 lost race per round; read-back 30/30 exact | `shr02/*_cluster*` |
| T-SHR-03 | A's LMCache SIGKILLed while B fetches A's entries | kv-sink | fail | **pass**: kill at 05:58:29.715 with B's first retrieve in flight; B 10/10 exact and `pipelined` (and 10/10 again later); both vLLMs alive; A re-registered and served 10/10 `pipelined` | `shr03/` |
| T-EVT-06 (GPU) | Client LRU off; A stores P-shared + P-exact-00..09, B P-shared + P-ragged-00..09; both re-read from L2; A reads B's | CE RF 2 | fail | **pass**: 90/90 exact; `integrity present` PASS after the stores and at the end; no eviction deletes (the only deletes are lost-race cleanups, O-4) | `evt06/*evt06_*` |
| (contrast) | Same with A's L2 LRU on at 0.5 GB | CE RF 2 | fail | as designed: 7-11 objects not whole, A's L2 hits 12/20, outputs 90/90 exact | `evt06/*evt06c*` |
| T-FLT-02 (GPU) | n3 SIGKILLed at the victim's lookup start (P-exact-15, 64 chunks) | CE RF 1 | recompute | **pass**: lookup ended 71 ms after the kill with `found_count=0`, victim recomputed and exact, engine alive; 12/12 later requests exact. They all missed (O-7) | `flt02/` |
| T-FLT-03 (GPU) | n2 SIGKILLed the same way | CE RF 2 | recompute | **pass**: the victim's lookup found 64/64 through the replica and was exact; after the 2-node cluster settled, 10/10 full hits, exact | `flt03/` |
| T-FLT-04 (GPU, CE) | n2 restarted (graceful) at the victim's lookup start | CE RF 1 | recompute | **pass**: victim exact; after the restart every entry is back: 10/10 full hits, exact | `flt04/` |
| T-FLT-04 (kv-sink) | kv-sink server restarted under a live LMCache (data and server-side registration gone) | kv-sink | fail | **pass**: the re-store after the restart and the 4 GB L1 fill worked; the reread (L1 empty for it) was `pipelined` and exact in 107 ms: the first per-layer batch reads failed (48 sub-read errors), the client registered a new region on the restarted server inside the same fetch, and no fallback was needed (O-5); after an LMCache restart, `pipelined` again | `flt04k/` |
| T-EVT-04 (L1) | L1 3 GB (plus the 4 GiB of windows), `--max-gpu-workers 2`; vLLM B stores P-long (64 chunks) while vLLM A reads L2 hits, 6 rounds | kv-sink | fail | **pass**: 18/18 victim retrieves `pipelined` and exact, 12/12 engine checks alive, no errors. In round 6 a pressure eviction (16 keys, 06:58:45.939) ran inside a 64-chunk pipelined fetch (06:58:45.753-47.253), which completed `pipelined`. The fill's L1 writes were refused (D-25) | `evt04/` |
| T-EVT-04 (L2) | Every segment of chunk 3 deleted at `MP retrieve start` (evictor pre-armed), 3 prompts | kv-sink | fail | **pass under fail**: each fetch failed on layer 1, 2 or 3, fell back to a whole load, which raised `LayerUnservableError` (the data is gone): clean HTTP 500, engine alive; later fetches `pipelined` and exact. **Recompute half blocked by D-17** (a mid-forward failure) | `evt04l2/` |

Hit counts (D-20 does not apply: the race sends ran at concurrency 1 per host):
T-SHR-02's race rounds had host B hitting A's fresh entries on 14/15 prompts per round
on both L2s (A first, B a moment behind). The only true collision per round is the one
`client_write_error`. All 99 listener checks passed (only loopback, plus the known
`udp:4791` of `rdma_rxe`). All 69 LMCache stops were clean (exit 143 within the 60 s
grace). There were no section-7 errors and no engine deaths, and the kv-sink logs have
no late completions, region errors or failed writes. The only LMCache tracebacks are
the 9 deliberate `LayerUnservableError`s in `evt04l2` (and 6 in `run2_evt04l2`).

### Defects and observations (GPU half)

| ID | Severity | Finding | Cause |
|---|---|---|---|
| D-25 (new) | S3 | Under L1 pressure a store batch is all or nothing: `Failed to batched allocate 32 memory blocks ... (short by 4 blocks)` refuses all 32 chunks (1 GiB of KV), and they never reach L2 (the L2 store goes through L1). Eviction runs only on the controller's 1 s tick above the 0.8 watermark, so a burst store bigger than the headroom is dropped while L1 sits below the watermark (0.78 in `run2_flt04k`). Seen in every `evt04` round and in `run2_flt04k` | Batched L1 allocation without eviction-on-demand or a partial grant. Hit-rate only; outputs exact |
| D-17 (known) | S1 | Blocks the recompute half of T-EVT-04 (L2) | vllm#49250 |

- **O-4 (Info).** After an L2 hit, a host sometimes stores a chunk that is already in L2
  (1-3 per send in `shr01`/`evt06`). It writes the chunk's 64 segment records, loses
  the create-only meta write (`client_write_error`) and deletes them again. That is
  D-14's designed loser path, and correct, but it costs a 32 MiB write per occurrence.
  T-EVT-06's "no L2 deletes" check counts these cleanups: the deletes equal 64 × the
  write errors in every send, and there were no eviction deletes.
- **O-5 (Info, better than the plan).** After a kv-sink restart, the plan expects a
  stale-registration read to fall back. On the batch-read stack the client registers
  its window on the restarted server inside the failing fetch, so the read stays
  `pipelined`. Suggest amending T-FLT-04's kv-sink expectation.
- **O-6 (known, Stage 5).** A failed pipelined fetch quarantines its window for
  `fetch_timeout_seconds` (30 s). With two windows, the next fetches within 30 s are
  `refused` (loaded whole, exact). `run2_evt04l2` hit this; the harness now waits 32 s.
- **O-7 (expected).** At RF 1 with one node of three down, almost every chunk misses:
  a 32 MiB chunk is about 64 segment records spread over every partition, so all but a
  vanishing fraction have one on the dead node. This matches the CPU half. Lookups
  stayed fast (about 1 ms, clean misses, no errors after the cluster settled).
- **Setup.** In `evt04` the salted fill (about 24 GiB in total) filled the 16 GiB kv-sink
  namespace, and from round 4 on its L2 stores failed with `AEROSPIKE_ERR_SERVER_FULL`.
  The victims' entries were stored first and were unaffected.

### Superseded attempts

- `run1_shr01`: P-shared (8 chunks) was `pipelined` at cap 64 where the old harness
  expected `not_deferred` (written for cap 4); outputs exact. Rerun with the expectation
  fixed.
- `run2_flt04k`: the reread was served from L1 (the fill stopped at 0.78 usage, below
  the eviction watermark: D-25), so the kv-sink path was not exercised. The fill now
  adds two 16-chunk prompts.
- `run2_evt04l2`: the third round and the later fetches were `refused` (O-6).
- `run2_evt04`, `run3_evt04`: every 4-chunk fetch (about 0.1 s) ended before the next
  1 s eviction tick, and run 3's 64-chunk rounds had too little fill to cross the
  watermark. Run 4 (the verdict) times the 64-chunk victim onto the fill's second store.

### Next steps (GPU half)

1. T-E2E-11 (70B) and T-FLT-08 are separate items. On this stack T-E2E-11 can use the
   default cap (D-12 is gone), so `CAP70` should be revisited.
2. D-25: decide whether a refused L1 batch should evict on demand or write through to
   L2. This is product work and needs an "Agent:" ask.
3. D-17 still blocks every recompute half that fails mid-forward (T-EVT-04 L2 here).
4. Amend T-FLT-04's kv-sink expectation (O-5): reads stay pipelined across a server
   restart.

## T-E2E-11 (Llama-3.3-70B, gpu-e2e11)

**Pass**, no wrong token. Results, records and plans at production size and D-26 are in
[`E2E11.md`](E2E11.md).
