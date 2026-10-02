# Stage 4 harness: concurrency (runbook for the GPU worker)

`stage4/stage4.sh` runs T-PIPE-08, T-PIPE-10 and T-E2E-09
(functional-test-plan.md 5.5 and 5.6) on the Stage 3 setup. That setup is
Llama-3.1-8B, with LMCache's Aerospike RDMA adapter against the kv-sink
server (127.0.0.1:3100, RC on rxe0 GID 1), `--use-layerwise`, vLLM async
scheduling on (vLLM's default) and `VLLM_BATCH_INVARIANT=1`. Each section is
one or more `run_steps.sh` sessions in `lmc-c`, built from `stage3.sh`'s
helpers. Every group gets a freshly restarted and warmed kv-sink server.
The CPU dry run passed (results below). No vLLM has run yet.

```bash
# On the box, from the host, with the GPU free (Stage 3 helpers must be in the tree):
cd /root/lmc-work/functional/stage4
setsid nohup bash /root/lmc-work/LMCache/functional/stage4/stage4.sh > stage4.txt 2>&1 < /dev/null &
# or single sections:  ... stage4.sh precheck pipe08 pipe10
```

Progress goes to `stage4/progress.log`. Each section gets a directory with
the session outputs, the vLLM and LMCache logs (`vllm2_*` for the second
instance), `report_*.md` from `hit_report.py`, `agree_*.md` from
`logprob_agree.py`, and the kv-sink log of every group.

## On the new kv-sink stack (batch-read server, 127.0.0.1:3700)

```bash
bash /root/lmc-work/LMCache/functional/stage4/launch.sh s4 precheck pipe08s pipe08 pipe10 ref16 e2e09 idle
```

`launch.sh` sources `newstack/kvsink_bp_env.sh`, so `KVSINK_PORT=3700`
reaches the adapter specs and the precheck, and every group restarts
`aero-kvsink-bp` (no warm-up). Changes from the old-protocol harness:

- **T-PIPE-08's limit is the same.** The sink-fetch driver registers the
  window range once and runs each layer's batch read on a 16-thread pool,
  but concurrency between retrieves is still bounded by the windows:
  `RdmaWindowLeaser` grants one lease per window, the native
  `SinkFetchTable::begin` accepts one fetch per window, and a retrieve that
  finds no free window is `refused` and loads whole objects
  (`pipelined_loading.py`, `aerospike_rdma.md` "Leasing a window"). The
  16 threads are per-layer batches of the fetches already admitted, not
  extra retrieves.
- **`--pipelined-shared-keys` is unchanged** in 1a (`recompute` | `wait`,
  `--pipelined-shared-wait-seconds` 1.0).
- **pipe10 runs under `fail`** (`P10_POLICY`). A `shared_keys_busy`
  retrieve fails mid-forward, and recomputing that hits D-17
  (vllm#49250, wrong tokens). Under `fail` the busy request ends in a clean
  HTTP 500 and its partner must be exact. `P10_POLICY=recompute` records
  the D-17 case under a `_recompute` tag.
- **`STOP_GRACE=60`**: restarts SIGTERM LMCache and wait up to 60 s (a
  clean shutdown takes 14-17 s, D-18) instead of kill -9 after 5 s.
- **e2e09 plain** adds `--no-l1-use-lazy`: the adapter enables RDMA, which
  needs a fixed L1 slab.
- `TWO_VLLM_UTIL` (0.3) may drop to 0.25 if two instances don't fit.

## Why T-PIPE-08 and T-PIPE-10 need two vLLM instances

LMCache handles `RETRIEVE` as a blocking handler on its affinity thread pool.
The affinity key is the ZMQ client identity (`mq.py`: `hash(prefix_frames[0])`,
`affinity_pool.py`), so every retrieve from one vLLM at TP=1 runs on one
thread, one at a time. A pipelined fetch blocks that thread until every layer
has landed (`run_pipelined_retrieve`) and releases its window before the next
retrieve starts. With one vLLM, then:

- no retrieve ever finds every window leased, so `refused` cannot happen
  however many requests are in flight (T-PIPE-08);
- two fetches of the same key never overlap, so `shared_keys_busy` cannot
  happen and a busy key is never seen (T-PIPE-10).

Both sections therefore start a second vLLM on 127.0.0.1:8001
(`run_steps.sh vllm2`) against the same LMCache server, which runs with
`--max-gpu-workers 2` so each instance gets its own retrieve thread. Each
instance gets `--gpu-memory-utilization 0.3` (`TWO_VLLM_UTIL`). `pipe08s`
keeps the single-instance case on record. Its expected result is 0
`refused` with all outputs equal, and that is not a failure.

`run_steps.sh` changes for this (backward compatible): `vllm2 model=...`,
`vllm2_stop`, `send ... port=8001`. With both instances up, `restart` and
`server_up` wait for both to register again.

## Sections

| Section | Test | Setup | Pass criterion |
|---|---|---|---|
| pipe08s | T-PIPE-08 (record) | one vLLM, `window_count` 2, cap 4; the 8 prompts below stored, LMCache restarted, then sent at concurrency 8 (L2 hits) | outputs equal to the batch-1 baseline; outcome counts recorded (0 `refused` expected) |
| pipe08 | T-PIPE-08 | two vLLMs, `window_count` 1, cap 4, `--max-gpu-workers 2`; instance 1 sends P-exact-10..13, instance 2 sends P-exact-14 and P-ragged-08..10 (distinct keys, each at most 4 chunks, D-12), 4 + 4 at once, after a restart, 3 rounds | at least one `refused` retrieve, and every request equal to the baseline (the refused ones were served whole). Hit columns are not checked: both instances move LMCache's counters |
| pipe10 recompute | T-PIPE-10 | two vLLMs, `--pipelined-shared-keys recompute`, `window_count` 2; both instances send the same 4-chunk prompt at once, for P-exact-10..14, after a restart | at least one pair is `pipelined` / `shared_keys_busy`; every output equal (vLLM recomputes the busy one, under policy recompute) |
| pipe10 wait | T-PIPE-10 | the same with `wait` (`--pipelined-shared-wait-seconds` 1.0 default) | no `shared_keys_busy`; at least one pair with `reused`; every output equal |
| ref16 | oracle check for T-E2E-09 | vLLM without LMCache, batch invariant, no prefix caching, the 60 P-shared + P-multi prompts at concurrency 16, twice (`run_ref.sh`) | none (reference). `compare.py` against the batch-1 baseline: 60/60 exact on both runs means token equality is a valid oracle for T-E2E-09 |
| e2e09 plain / pipe | T-E2E-09 | one vLLM, 16 concurrent clients over P-shared + P-multi (60 prompts): `cold` (empty cache), `l1` (everything in L1), `l2` (after a restart); plain path (no `--pipelined-fetch`) and pipelined path (cap 4) | every request equal to the batch-1 baseline (`hit_report.py`); top-1 agreement >= 99.9% of tokens (`logprob_agree.py`); `l1`/`l2` batch hit totals equal the model; pipe: outcomes only `pipelined`/`not_deferred` |

Notes on reading the results:
- **pipe10 overlap.** The two instances' requests have to be in their
  retrieves at the same time. A pair that did not overlap shows `pipelined`
  and `pipelined`, or `pipelined` and `reused`. Five pairs make at least one
  overlap likely. If none overlapped, rerun the section; that is not a
  failure.
- **pipe10 wait and D-13.** Chunks loaded from L2 are evicted from L1 right
  after the retrieve (D-13). A waiting request can therefore find the key
  *absent* instead of readable, and then fetch it itself (`pipelined`
  instead of `reused`). That is correct behaviour. Record it and do not
  count it as a failure; only `shared_keys_busy` under `wait` fails.
- **Expected ERROR lines.** Under `recompute`, each `shared_keys_busy`
  retrieve logs `Cannot retrieve keys due to exception` with a traceback in
  the LMCache log. That is the designed path. The driver prints the count;
  compare it with the number of `shared_keys_busy` outcomes.
- **Server issue 7** (concurrent fetches on one region) may show up first in
  pipe08 and pipe10. This is the first time two LMCache threads fetch from
  kv-sink at once. Watch the kv-sink log for region errors, and the reports
  for `fell_back`.
- **e2e09 cold.** The hits of the cold send depend on which in-flight
  requests stored first, so its hit columns are not checked. Output
  equality is still checked.
- **P-shared under cap 4.** P-shared prompts hit 8 chunks, more than the
  cap, so they load whole (`not_deferred`). P-multi turns hit 1, 2, 4, 5 or
  7 chunks. Only the ones at or under 4 chunks are pipelined.

## T-E2E-09 oracle decision

- **Primary: token equality with the batch-1 baseline** (`day1/step4/bi_run1.json`,
  which covers P-shared and P-multi). This holds for Llama under
  `VLLM_BATCH_INVARIANT=1`: Stage 2b's 4/8/16-concurrent runs were 56/56
  token-equal to batch 1. Stage 2c found that gpt-oss is **not** batch
  invariant across batch sizes (79/130 at concurrency 8, G-14), so this
  oracle is for Llama only. `ref16` checks it with T-E2E-09's own arrival
  pattern and no LMCache. If ref16 is not 60/60, a T-E2E-09 mismatch is not
  S1 by itself. Compare that request with ref16a/ref16b, and rely on the
  agreement check and the byte oracle.
- **Secondary: top-1 agreement >= 99.9%** of tokens against the baseline
  (`logprob_agree.py`, strict reading: every position, including those after
  a divergence).
- **Byte oracle per request (plan section 2).** LMCache has no hook that
  checksums what one request received. `POST /cache/checksums` hashes GPU
  KV blocks by block id, but the harness does not know a request's block
  ids, and with 16 requests in flight those blocks are reused. Pipelined
  bytes go from the RDMA window straight to the GPU, so no L1 object is left
  to checksum afterwards. The smallest options, none done here (decision
  for humans):
  1. *Test-only, transport half.* Add `RDMA_ORACLE_SETS` to
     `tests/v1/distributed/test_aerospike_rdma_byte_oracle_integration.py`
     (today P-exact only), and run its `stored_by_vllm` test after `e2e09
     pipe` over the P-shared + P-multi records vLLM stored. That gives RDMA
     fetch == plain get for exactly the keys T-E2E-09 read. About 20 lines
     of test code, and it runs on the CPU.
  2. *Product debug hook, full per-request oracle.* Behind an environment
     variable, log the MD5 of each chunk's per-layer KV after the H2D copy
     (session id, object key) in the retrieve path. Then compare it with
     the MD5 of a plain get of the same key. This needs a product change
     and its own commit and test.
  3. *Default until decided.* Token equality (valid per ref16), the
     agreement check, and T-RDMA-06's 100/100 transport oracle as the proxy.

## Dry run (CPU, 2026-10-01, `stage4.sh dry`, box `stage4/dry4.txt`)

- Script syntax of `run_steps.sh` and `stage4.sh` checked.
- Adapter configs parse with `window_count` 1 and 2 (134217728-byte
  windows, which hold 4 Llama chunks).
- Prompt lengths: P-exact-10..14 are 4+0 chunks, P-ragged-08..10 are 4+1,
  4+128 and 4+255 (all at most 4 full chunks). T-E2E-09 has 60 prompts.
- kv-sink restarted and warmed; `lmcache server` started on the CPU against
  it in each Stage 4 configuration, every one with `rdma=RC` and no errors:
  `--max-gpu-workers 2` with `window_count` 1, `--pipelined-shared-keys
  recompute`, `--pipelined-shared-keys wait`, and the plain path.
- `logprob_agree.py` on Stage 2b's 16-concurrent runs (from L1 and from L2)
  against the batch-1 baseline: 1536/1536 positions agree, 32/32 exact, PASS.
- `--pipelined-shared-keys wait` adds its 1 s to the layer publish budget
  the server reports at registration. The worker's default per-layer wait
  (5 s) still exceeds it; registration checks this at run time.
- Not exercised without the GPU: two vLLM instances on one MI300X, the
  sends, and the outcome counts.
