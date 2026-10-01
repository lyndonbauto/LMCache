# D-14 fix: concurrent writes of one key

CPU only, 2026-10-01 19:40–20:42 UTC, worker `cpu-d14-fix`. No GPU, nothing
on ports 8000/6555, `aerospike-ce` (127.0.0.1:3000) never touched. Tests ran
in a new container `lmc-d` (image `lmcache-rocm:day1`, no GPU devices,
`/dev/infiniband/uverbs0`, host network) from a new tree
`/root/lmc-work/LMCache-d14` (rsynced from the local worktree, so the code
under test is byte-identical to `e9cd0689`), against:

- **E2:** a private Aerospike CE 8.2.0.0 node `aerospike-ce-d14`,
  127.0.0.1:3200–3202 (`scripts/aerospike-d14.conf`, namespaces `lmcache` and
  `lmcache_evict` as in item 2).
- **E4 storage:** the 3-node CE 8.2 cluster `aero-n1..n3`
  (127.0.0.1:3300/3310/3320, `functional/harness/cluster.sh`), driven by the
  control daemon (`scripts/start_ctl.sh`).
- **E1:** Soft-RoCE `rxe0`, GID 1; the kv-sink server on 127.0.0.1:3100 for
  the pipelined RDMA integration test (see CHANGES.md for the restart).

Fix: [`aerospike_concurrent_writes.md`](../../docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md)
(option 2, pipelined sub-option A). Paths below are relative to
`/root/lmc-work/functional/d14/` on the box.

## T-FLT-10 after the fix

Two writer processes released together on a fresh key each round, read by a
third adapter, which then deletes the key. 1,000 rounds per row
(`logs/cluster_it2.txt`):

| Object | RF | Before (stage 6) | After | Records left |
| --- | --- | --- | --- | --- |
| Sharded, 3 MiB in 4 segments | 1 | not run | 0 mixed (533 / 467 per writer) | 0 |
| Sharded, 3 MiB in 4 segments | 2 | 141 and 145 mixed | 0 mixed (509 / 491) | 0 |
| Inline, 64 KiB | 1 | not run | 0 mixed (911 / 89) | 0 |
| Inline, 64 KiB | 2 | 0 and 0 mixed | 0 mixed (929 / 71) | 0 |

"Records left" is the set's object count over all nodes after the last
round: 0 means every loser removed its segments. The first-version run
(`logs/cluster_it1.txt`) gave the same 0 mixed / 0 left.

## Read-path cost

| Path | Extra round trips | Measured (p50, `logs/read_cost_*.txt`) |
| --- | --- | --- |
| Plain get, exists, delete | 0 | whole 3 MiB load 1.0 ms (one node), 1.3 ms (cluster); unchanged path |
| Pipelined fetch | +1 batch read of the meta records | 1 / 4 / 16 / 64 keys: 25 / 47 / 63 / 129 µs on one node; 24 / 131 / 223 / 358 µs on the 3-node cluster |
| Store that loses a race | +1 meta select, ≤ `nseg` header reads, `nseg` deletes | not timed |

## Regression

| Suite | Before | After |
| --- | --- | --- |
| Storage ITs (`test_aerospike_{l2,record_layouts,storage_integrity}_integration.py`), E2 | 30/30 (item 2) | 36/36: the 30 plus 6 new D-14 tests (`logs/storage_it2.txt`) |
| Cluster file (`test_aerospike_cluster_integration.py`: T-STO-08, T-FLT-02/03/04, T-FLT-10, T-EVT-06), E4 | 6 passed, 2 failed (stage 6: T-FLT-10 sharded, RF 1 kill 3 misses after rejoin) | 10/10 (T-FLT-10 now 4 rows); T-FLT-04 RF 1: 70 exact, 0 miss after rejoin |
| E0 unit batch (day1fix batch1 set), CPU only | 2,720 passed, 5 failed, 12 errors (with GPU, lmc-c; failures are GPU/cache-server tests) | 2,402 passed, 181 skipped, 0 failed (`logs/units2/pytest.txt`); the GPU-only tests skip in `lmc-d` |
| RDMA suite on `rxe0` + `make test` | 17/17, all PASS (item 2) | 17/17, all PASS (`logs/rdma_suite.txt`, `logs/rdma_harness_direct.txt`) |
| `test_aerospike_pipelined_rdma_integration.py` vs kv-sink, warm | 3 passed, 1 skipped (cold-only test) | 3 passed, 1 skipped (`logs/pipelined_it_counted.txt`): the pipelined fetch lands every stored byte under per-write keys |

## What changed on the way

The first version deleted a damaged object in the reader (a load that finds a
named segment absent). The cluster run showed the cost: at RF 1 a segment is
absent only while its node is down, so T-FLT-04 went from 3 misses after the
node rejoined (stage 6, CE flush loss) to 13. Round 2 moved the self-heal to
the store: a store that loses to an object naming an absent segment removes
it (generation-guarded) and retries once; readers never delete. All results
above are round 2.

Six unit failures in the first batch were a test tap in
`test_three_track_retrieve.py` that did not forward `read_write_ids`; fixed in
the same commit.

## Not covered

- No GPU run: the GPU tree needs a rebuild (native and Python change) before
  any GPU stage exercises the fix.
- Orphans from writers killed between their segments and their meta record
  are reclaimed only by the TTL (as before); no sweeper.
