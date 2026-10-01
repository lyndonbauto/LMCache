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

| Test ID | What | Result | Evidence (`logs/`) |
|---|---|---|---|
| T-STO-08 | RF 2, commit level all: a store returns only with its replica; reads survive one node down | **pass** | `sto08.txt`, `cluster_it_full.txt`, `sto08_sensitivity.txt` |
| T-FLT-02 | Kill a node mid-fetch, RF 1 | **partial: storage half pass**; end to end with vLLM in Stage 6 GPU | `flt02_03.txt`, `flt02_rf1_flushwait_{1,2,3}.txt` |
| T-FLT-03 | Same at RF 2: reads fail over to the replica | **partial: storage half pass** | `flt02_03.txt`, `cluster_it_full.txt` |
| T-FLT-04 | Node restart | **partial**: CE half pass; kv-sink half pass at the fanout level (registration dropped by the restarted node, re-registration on a fresh context); the adapter path can't run on 3 nodes (N1) | `flt02_rf1_flushwait_*.txt`, `flt04_kvsink_probe.txt`, `rdma05_adapter_probe.txt` |
| T-FLT-10 | Two engines store the same chunk at once, 1,000 times | **fail (S2, D-13)**: sharded chunks mixed in 141/1000 and 145/1000 rounds; inline chunks 0/1000 twice | `flt10.txt`, `cluster_it_full.txt` |
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
  2 × 1,000 rounds. See D-13.
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
| D-13 | S2 | Two writers storing the same sharded key at the same time leave a mixed object: the meta record says present and the segments come from both writers (about 14% of rounds). A reader during a rewrite can see the same | `do_single_set` writes segments under fixed keys `<key>\|s\|<i>` with `AS_POLICY_EXISTS_IGNORE` and then the meta record. Nothing ties a meta record to the segment set it describes. A create-only meta write (the plan's Track A blocker) would **not** fix this alone: the losing writer has already overwritten segments of the winner's committed object before its meta write fails |

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

1. D-13: Track A decides the fix (see the proposal in the work result). After
   it lands, rerun `-k racing` with 1,000 rounds.
2. Stage 6 GPU: T-FLT-02/03/04 and T-EVT-06 end to end with two vLLM +
   LMCache hosts on `cluster.sh`. Wait 2.5 s or more after the last store
   before a RF 1 kill (O-1).
3. T-RDMA-05 end to end and the kv-sink half of T-FLT-04 through the
   adapter: blocked on N1 (multi-node pipelined plans) and on the server
   team's answer about in-flight fetches after a restart.
