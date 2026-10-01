# Stage 3 (pipelined path on Soft-RoCE): summary

Server under test: the kv-sink server `512b0c207`. It is a fencing build
(issue 4), so every pipelined row is "pass on a fencing server" and is rerun
when a no-fence build lands. Runbook: [HARNESS.md](HARNESS.md).

The table in "Stage 3 final table" supersedes the partial one from the first
GPU run, which is kept below under "History".

## Stage 3 final table (gpu-stage3b, 2026-10-01 21:47-23:39Z)

**Outcome.** Every Stage 3 test has now run. No request anywhere in Stage 3
returned wrong tokens except the D-17 rows of the first run, which used the
`recompute` policy (a known vLLM bug). This run: Llama 160/160 (e2e04), 8/8
(e2e05), 2/2 (pipe12), the 4-chunk gpt-oss hit, gpt-oss 360/360 (e2e08p) plus
120/120 (its L2 tail), and every fault request either matched or failed with a
clean HTTP 500. The failing criterion is the same everywhere: on the kv-sink
server, D-12's `late completion for slot N` puts the client region in error
after 7 to 24 fetches, and every later fetch falls back to whole objects or is
refused. Those rows fail (S2, D-12) even though their outputs are exact.

Tree: box `/root/lmc-work/LMCache` at `a3504150` at start. That build already
contains the D-14 fix `e9cd0689` (rebuilt by gpu-d17-narrow); the commits
after it are `functional/` only, so there was no rebuild. Harness commits used:
`d10a376f`, `b6b9d76f`, `4c7a3df3`. During this run, origin received the
newstack product merge (`e4701b9a`..`e6d35a0c`). The box tree was held at
`d10a376f` product code so that every row in this table ran the same build;
see VERSIONS.md.

Decisions in force (humans, recorded here):
- Lyndon (Slack, 2026-10-01 21:00Z): no fixes for D-17 or D-12. The RDMA
  server and the LMCache client code will be replaced. Every relevant test
  runs without the fixes, and a known defect is recorded, not retried or
  worked around.
- Fencing (mitigation 3): the kv-sink build still fences, so passing rows are
  "pass on a fencing server".
- D-12, default 2+4: run the default cap (64) and `--pipelined-max-chunks 4`.
  Judge output correctness, and record the pipelined, refused and fell_back
  counts. D-12 is also known to fail single-command fetches on a reused region
  after 6 to 8 fetches.
- D-17 (vLLM bug [vllm#49250](https://github.com/vllm-project/vllm/issues/49250)):
  recompute-after-mid-forward-failure tests run under the `fail` policy
  (expected: a clean error and no wrong tokens). The `recompute` half is
  recorded as "blocked by D-17 (vLLM)". The scratch vLLM was not used.
- gpt-oss oracle: prefix hits are judged against vLLM's prefix cache at block
  256 (`pc256_*`), and block 16 (`pc16_r1_*`) is reported. Sends run at
  concurrency 1 (D-16). The T-E2E-08 pipelined half uses Stage 2c's server
  flags, and T-PIPE-11 uses `--separate-object-groups`.
- kv-sink hygiene: the server is restarted and warmed (`kvsink_smoke.sh`)
  before every group.
- Harness fixes allowed: `vllm_check`'s engine-death pattern (it counted
  LMCache's "load failed" warnings), and the rxe0 packet math (packets are
  about 1 KiB).

| Test | What | Result | Evidence (box `/root/lmc-work/functional/stage3/`) |
|---|---|---|---|
| T-CFG-08 | Registration line; one 4-chunk pure L2 hit, pipelined | **pass on a fencing server** (gpu-d17-narrow rerun on the D-14 build: line at both registrations, hit `pipelined`, exact). This run also logged the line at every Llama and gpt-oss registration (`... fetches layer by layer from L2 adapter 0, reading records of at most 983040 bytes`) | `cfg08/`, `stage3b_*.txt` (`-- registration:`) |
| T-E2E-04 (cap 4) | P-exact + P-ragged cold, LMCache restart, warm from L2 | **fail (S2, D-12)**: outputs 80/80 exact. Over the cap: 14/14 `not_deferred`. Eligible: 24 of 26 `pipelined`. The 25th fetch on the region (P-ragged, 4 chunks, one command) got `late completion for slot 0`, then 2 `fell_back`, 0 `refused`. First run (before D-14): 7/26 pipelined, 2 fell_back, 17 refused, error at the 8th fetch | `e2e04/report_e2e04_c4_*`; first run `e2e04_r1/` |
| T-E2E-04 (cap 64) | Same at the default cap | D-12 reproduction (not judged): outputs 80/80 exact. short: 9 `pipelined`, 4 `fell_back`, 22 `refused` (first run: 5/4/26). long1/long2 (64+ chunks): all fell back or were refused, in both runs. One late completion per group | `e2e04/report_e2e04_c64_*`; `e2e04_r1/` |
| T-E2E-05 (cap 4) | At the cap and one chunk over | **pass on a fencing server**, same in both runs: P-exact-10 (4 chunks) `pipelined` and exact; P-multi-03 (5 chunks) `not_deferred` and exact | `e2e05/report_e2e05_c4_all.md`; `e2e05_r1/` |
| T-E2E-05 (cap 64) | P-long-00 at 64 chunks, P-long-05 over | D-12 reproduction, same in both runs: at the cap `fell_back` (late completion for slot 1536), exact; over the cap `not_deferred`, exact | `e2e05/report_e2e05_c64_all.md`; `e2e05_r1/` |
| T-PIPE-05 | Segment 5 of chunk 1 of P-exact-10 deleted (meta kept) | **partial**. `fail` half passes: probe and repeat were both `failed` (layer 2 never arrives, and the whole-object fallback cannot load the deleted record). vLLM logged `Failing 1 request(s) due to KV load failure`, returned HTTP 500 twice, and stayed alive (0 engine deaths, 0 generation timeouts). No wrong tokens. The `recompute` half is **blocked by D-17 (vLLM)**. `fell_back` with a correct output is out of reach with this fault: the record is gone, so the fallback cannot succeed either | `pipe05/`; recompute run `pipe05_r1_recompute/`, `pipe05_noasync/` |
| T-PIPE-06 | kv-sink SIGSTOP 2.5 s at lookup end, 3 times; then after13/after14 | **partial**. `fail` half: stall10/11 hit the layer-0 deadline (`failed`) and returned clean HTTP 500s; vLLM stayed alive. stall12 was `refused` (both windows quarantined), loaded whole, exact. After the 30 s quarantine, the window **was leased again**: after13 started a pipelined fetch. But kv-sink logged `late completion for slot 0` (D-12 on a reused region), so after13/14 `fell_back`, exact. `recompute` half: **blocked by D-17**. The first attempt in this run (`pipe06_r2_fail_nowait/`) sent after13 5 s after the stalls, inside the quarantine. That gave `refused`, as in the first run: a harness timing gap, now fixed (`4c7a3df3`) | `pipe06/`; `pipe06_r2_fail_nowait/`, `pipe06_r1_recompute/` |
| T-PIPE-07 | Late write from an abandoned fetch | partial, as decided. In `pipe06/`, the first fetch after the abandoned ones got a server late completion and fell back, with exact output, so nothing late was credited to it. E3 cannot delay RDMA alone (HARNESS.md) | `pipe06/kvsink_pipe06/asd-kvsink.log` |
| T-PIPE-12 | Records under one `max_record_bytes`, read under another | **pass on a fencing server**: stored at 256 KiB and read with discovery (1 MiB), and the reverse. Both reads `fell_back`, exact, no error | `pipe12/report_pipe12_*` |
| T-PIPE-11 | gpt-oss sliding-window layers fetch only their window (`--separate-object-groups`) | **partial**. The 4-chunk pure L2 hit was `pipelined` and exact; staging matches the plan for all 36 layers. At the object level the window limit holds: 8 keys looked up, 5 retained (4 full-attention + 1 sliding-window chunk), `retrieved_count=5`. The rxe0 byte check does not match: 100,360 packets received, against 46,080 (window-limited) or 73,728 (full) expected at 1 KiB per packet. With one object group (e2e08p, below), the count matches the per-layer window-limited plan. Unexplained; needs the per-layer fetch plan logged (HARNESS.md missing hook 3) | `pipe11/` |
| T-E2E-08 pipelined half | gpt-oss through T-E2E-02..07, `--pipelined-fetch` cap 4, Stage 2c flags | **fail (S2, D-12)**; outputs pass. 360/360 exact against `pc256` (block 16: 352/360; the 8 are the P-ragged prompts where vLLM alone differs at block 16 and 256, as in Stage 2c). L2 send within the cap: 7 of 26 `pipelined`, then a late completion at the 8th fetch, then 8 `fell_back` and 11 `refused`. The full sequence stores about 17 GB, more than kv-sink's 16 GiB `data-size`, so the last P-shared/P-multi stores got `AEROSPIKE_ERR_SERVER_FULL` and l2sh/l2mu found nothing in L2 (harness capacity, not judged). The tail rerun on a fresh server (`e2e08pl2`) was 120/120 exact, every hit the expected length from L2. l2mu: 19 `pipelined`, then a late completion, then 8 `fell_back`, 3 `refused`. l2sh is over the cap (8+ chunks), so `not_deferred`. rxe0 during the within-cap send: 150,800 packets, which matches the per-layer window-limited plan for 5×1 + 2×2 pipelined chunks (147,456 data packets plus about 2% acks), not the full size (165,888) | `gpt_e2e08p/report_e2e08p_pc256.md`, `_pc16_r1.md`; `gpt_e2e08pl2/` |
| T-RDMA-06 GPU half | vLLM stores P-exact-00..16; the byte oracle reads 100 production records by RDMA and by plain get | **pass on a fencing server**: `test_rdma_equals_plain_gets_for_100_keys_stored_by_vllm` 1 passed; no late completions | `rdma06gpu/rdma06gpu.txt`, `rdma06gpu.junit.xml` |

### D-12 in this run (S2, server; not fixed by decision)

Each kv-sink group logged exactly one `late completion` at most, then the
region stayed in error until LMCache restarted:

| Group | Fetch that hit it | After it |
|---|---|---|
| e2e04 c4 short | 25th (slot 0) | 2 fell_back |
| e2e04 c64 short / long1 / long2 | slot 0 / 2048 / 1024 | fell_back, refused |
| e2e05 c64 | P-long-00 (slot 1536) | fell_back |
| gpt_e2e08p | 8th (slot 0) | 8 fell_back, 11 refused |
| gpt_e2e08pl2 | about the 20th (slot 0) | 8 fell_back, 3 refused |
| pipe06 | first fetch after the abandoned ones (slot 0) | 2 fell_back |
| e2e04 c4 long1/long2, e2e05 c4, pipe05, pipe12, pipe11, rdma06gpu | none (at most 2 fetches each) | |

At cap 4 the error came later than in the first run (25th fetch, against the
8th). With one run of each, that difference is not attributable to the D-14
fix: the gpt-oss groups on the same build hit it at the 8th fetch.

### Other findings

- **kv-sink capacity (Info, harness).** The full T-E2E-08 sequence fills the
  kv-sink namespace (16 GiB; `breached stop-writes limit`). LMCache logged
  `Store task N ... failed: AEROSPIKE_ERR_SERVER_FULL` warnings and carried on.
  No crash, and outputs stayed exact. `stage3_gpt.sh e2e08pl2` reruns the tail
  on a fresh server.
- **pipe11 byte count (S3, open).** See the T-PIPE-11 row. The one-group
  control is consistent with per-layer window limiting. The separate-groups
  count is 2.2 times the expected size, and nothing logged explains it.
- **Harness fixes** (`d10a376f`, `b6b9d76f`, `4c7a3df3`):
  - pipe05/pipe06 run under `FAULT_POLICY` (default `fail`), with `errors=1`
    and `--allow-error`/`--no-hit-check` on the faulted sends;
  - rxe0 packet math uses 1 KiB packets;
  - `vllm_check` no longer counts LMCache's own `(EngineCore pid=N) ... load
    failed` warnings as engine deaths (all `engine_dead_errors` were 0 in this
    run);
  - `e2e08pl2` reruns the P-shared/P-multi L2 tail;
  - pipe06 waits 32 s (`PIPE06_REUSE_WAIT`) for the window quarantine
    (`fetch_timeout_seconds`, 30 s) before after13/after14.

### Next steps

1. D-12: a server fix (or the replacement server), then rerun e2e04, e2e05
   cap 64, e2e08p, pipe06 and the cap-64 rows. The harness is ready
   (`stage3.sh`, `stage3_gpt.sh e2e08p e2e08pl2`).
2. D-17: once the ROCm vLLM carries the vllm#49250 fix, rerun pipe05 and
   pipe06 with `FAULT_POLICY=recompute`.
3. T-PIPE-11: log the per-layer fetch plan (slots and bytes), or count kv-sink
   write bytes, so the byte check can be closed under
   `--separate-object-groups`.
4. On the newstack (batch-read server `046e8558d`), the same drivers apply via
   `newstack/kvsink_bp_env.sh`. The box tree must be pulled and rebuilt first.

## History: first GPU run (gpu-stage3, 2026-10-01 20:02-20:48Z), stopped on S1

Superseded by the final table above: e2e04/e2e05 were rerun on the D-14
build, and pipe05/pipe06 were rerun under `fail`. This run's directories were
renamed `e2e04_r1/`, `e2e05_r1/`, `pipe05_r1_recompute/` and
`pipe06_r1_recompute/`; the paths below refer to them by their old names.

**Outcome.** The stage stopped after pipe06 because of an **S1, D-17**. When a
layerwise pipelined load fails partway through the forward pass under
`kv_load_failure_policy=recompute`, vLLM logs "Recovered from KV load
failure: 1 request(s) rescheduled" and the output is still wrong. It
reproduced 4 times out of 4 failed loads (pipe05 twice, pipe06 twice) and again
with async scheduling off. pipe12, pipe11, rdma06gpu and the gpt-oss
T-E2E-08 pipelined half did not run. Every
completed request that did *not* fail mid-load matched its oracle:
cfg08 2/2, e2e04 160/160 (both caps), e2e05 8/8, pipe06 non-failed 8/8.

Decisions applied (humans): fencing server, mitigation 3 (rows are "on a
fencing server"); D-12 default 2+4 (caps 64 and 4; correctness judged at cap
4); kv-sink restarted and warmed with `kvsink_smoke.sh` before every group;
T-PIPE-07 partial; T-PIPE-11 by rxe0 packet counts; E3 faults via record
deletion / SIGSTOP / `max_record_bytes`. Layerwise was on in every session
and async scheduling was on (vLLM's default; nothing disables it), except in
the one S1 diagnostic.

| Test | What | Result | Evidence (box `/root/lmc-work/functional/stage3/`) |
|---|---|---|---|
| T-CFG-08 | Registration line for the pipelined path; one 4-chunk pure L2 hit | **pass on a fencing server**: line logged at both registrations (`... fetches layer by layer from L2 adapter 0, reading records of at most 983040 bytes`); the hit was `pipelined`, exact, deferred counter `outcome="pipelined"` 1 | `cfg08/` |
| T-E2E-04 (cap 4) | P-exact + P-ragged cold, restart, warm from L2 | **fail (S2, D-12 broadened)**: outputs 80/80 exact (short: 35 cold + 26 eligible + 9 over the cap; long1/long2: 10/10, all over the cap). Over the cap: `not_deferred`, as required. Eligible: only 7 of 26 were `pipelined`. The 8th fetch on the region (P-exact-07, 2 chunks, one server command) lost layer 16. kv-sink logged `late completion for slot 0 on region 23`, put the region in error state, and every later fetch fell back (2) or was refused (17) | `e2e04/report_e2e04_c4_short_all.md`, `e2e04/kvsink_e2e04_c4_short/asd-kvsink.log` |
| T-E2E-04 (cap 64) | Same at the default cap | D-12 reproduction (not judged): outputs 80/80 exact. short: 5 pipelined, then P-exact-05 (2 chunks) fell back with `late completion for slot 0`, and 26 were refused. long1/long2 (64+ chunks): all fell back or were refused | `e2e04/report_e2e04_c64_*` |
| T-E2E-05 (cap 4) | At the cap and one chunk over | **pass on a fencing server**: at the cap (P-exact, 4 chunks): `pipelined`, exact. Over (P-multi, 5 chunks): `not_deferred`, exact | `e2e05/report_e2e05_c4_all.md` |
| T-E2E-05 (cap 64) | P-long-00 at 64 chunks, P-long-05 over | D-12 reproduction: at the cap it `fell_back` (late completion, region error), exact. Over: `not_deferred`, exact | `e2e05/report_e2e05_c64_all.md` |
| T-PIPE-05 | Segment 5 of chunk 1 of P-exact-10 deleted, meta kept; recompute | **fail (S1, D-17)**: probe and repeat send were both `failed` (layer 2 never arrives, then the whole-object fallback cannot load the missing object). vLLM rescheduled 1024 tokens, and both outputs are wrong: `' New York the 5th of May…'` and `' New 0 0 0…'`, where the store send gave `' 2677, the school in Cairo…'`. The plan expects `fell_back` with a correct output | `pipe05/`, `pipe05/report_pipe05_all.md` |
| T-PIPE-06 | kv-sink SIGSTOP 2.5 s at lookup end, 3 times; recompute | **fail (S1, D-17)**: stall10 and stall11 timed out on layer 0 (`failed`), vLLM rescheduled them, and both outputs were wrong (first token 269 `' or'`, where 220 is correct). stall12 was `refused` and exact. The window was not leased again: after13 and after14 were `refused`, though exact. vLLM stayed alive, with 0 generation-timeout errors | `pipe06/`, `pipe06/report_pipe06_all.md`, `pipe06/HOSTFAULT_*` |
| T-PIPE-07 | Late write after re-lease | partial, as decided (E3 cannot delay RDMA alone); nothing new, since pipe06 never re-leased a window | n/a |
| T-PIPE-12 | Records under one `max_record_bytes`, read under another | not run (stage stopped on S1) | |
| T-PIPE-11 | gpt-oss sliding-window layers fetch only their window | not run (stage stopped on S1). Prepared: `stage3_gpt.sh pipe11` | |
| T-E2E-08 (pipelined half) | gpt-oss through T-E2E-02..07 with pipelining | not run (stage stopped on S1). Prepared: `stage3_gpt.sh e2e08p` | |
| T-RDMA-06 GPU half | vLLM-stored records, RDMA vs plain get | not run (stage stopped on S1) | |

### D-17 (S1): wrong output after a failed layerwise load under recompute

> **Narrowed (gpu-d17-narrow, 2026-10-01): the cause is a vLLM bug, not
> LMCache.** It is upstream [vllm#49250](https://github.com/vllm-project/vllm/issues/49250).
> The V2 runner does not rewind its GPU `num_computed_tokens` for a recompute, and
> async scheduling does not roll back output placeholders. With vLLM fixed (V1
> runner, or V2 with a one-change scratch patch, async off), the same fault gives
> exact output. The hypothesis below about late copies is ruled out. See
> [D17-NARROWING.md](D17-NARROWING.md).

- **Trigger.** Any pipelined retrieve that fails after the forward pass has
  started waiting on layers. In pipe05 a segment record was deleted, so layer
  2 never arrives and the whole-object fallback raises "1 deferred object(s)
  could not be loaded whole". In pipe06 kv-sink was frozen, so layer 0 timed
  out (`will never arrive`). Both end with `pipelined_outcome=failed`, and
  `retrieved_count=0`.
- **What vLLM does.** The connector logs `Layerwise KV load failed at layer
  ...; reporting 64 blocks as load errors so vLLM recomputes them`. The
  scheduler then logs `Recovered from KV load failure: 1 request(s)
  rescheduled (1024 tokens affected)`. The output is still wrong.
- **Not the oracle.** The same prompt on the same server matched the
  baseline in the store send. stall12 and after13/14 (`refused`, loaded
  whole) matched. The outputs are degenerate (`' 1 the 1 the …'`).
- **Async scheduling is not the cause.** `s1_diag.sh` repeats pipe05 with
  `--no-async-scheduling` (`pipe05_noasync/`; the vLLM args show
  `async_scheduling: False`). It is still wrong in 2 of 2 sends: the first
  token is now correct (220), but the rest is garbage (`' 1.5.1.1.1…'`), and
  vLLM again logs "rescheduled (1023 tokens affected)". With async scheduling
  on, the first token is wrong and the text after it is coherent. So the
  timing changes the symptom, but the KV the recompute should rebuild stays
  corrupt either way.
- **Leading hypothesis (not proven).** Copies from the failed load (pipelined
  layers, or the whole-object fallback) land in vLLM's blocks after the
  failure was published, which overwrites the recomputed KV. That would
  break the assumption in `LMCacheMPConnector.wait_for_layer_load`'s
  docstring: "the daemon publishes a failure only after every copy the
  retrieve queued has landed". Alternative: the rescheduled request reuses
  the partly written blocks. Untested so far: whether the plain path
  (layerwise off), or layerwise with `--pipelined-fetch` off, recomputes
  correctly after the same deletion.
- **Not covered before.** Earlier coverage of this path is E0 only: T-FLT-01
  ran on the plain path, and T-PIPE-05/06 passed at E0/E1. This is the first
  E3 run of a mid-forward failure under recompute.

### Other findings

- **D-12 is broader than recorded (S2, server).** It also hits
  single-command fetches (at most 4 chunks, at most 256 slots) once the
  client region has served a few fetches: the 8th fetch at cap 4, the 6th at
  cap 64. Each time there was one `late completion for slot 0`, then the
  region stayed in error state (server issue 8) until LMCache restarted.
  Every following pipelined fetch fell back or was refused. The CPU half of
  T-RDMA-06 did not see this because it registers a fresh window per fetch.
  In production one region (two windows, 256 MiB at cap 4) serves every fetch.
  Outputs stayed correct.
- **Harness: RDMA needs `--no-l1-use-lazy` on the GPU** (fixed in `30734468`).
  Lazy L1 is the default whenever pinned memory is supported. The CPU dry
  run had it off implicitly, so the first attempt's LMCache refused to start
  (`RDMA windows need a fixed-size L1 slab`). That attempt's logs are in
  `attempt1_lazy/`.
- **rxe0 counters count about 1 KiB packets.** The 4-chunk Llama hit (128
  MiB) moved 133,120 received packets, not the 32,768 4-KiB packets
  `stage3.sh` prints. The `pipe11` thresholds must be scaled by 4 (bytes /
  1024 + acks).
- **gpt-oss flags.** Stage 2c served gpt-oss *without*
  `--separate-object-groups` (`stage2c.sh` `L2=` line), so every chunk is
  stored whole for all 36 layers, and a window-limited fetch cannot show up
  in the bytes. `stage3_gpt.sh` therefore runs `e2e08p` with 2c's flags
  (`E2E08_SERVER_FLAGS` empty, comparable with 2c, rxe0 counted as the
  full-size control), and `pipe11` with `--separate-object-groups`, the only
  configuration that can show the sliding-window limit. `GPT_BASE` =
  `stage2/gptoss_ref/base_b16_all.json` (batch 1, same prompt set, the oracle
  for P-exact whole-prompt hits). Prefix hits: `pc256_*` gives the verdict,
  `pc16_r1_*` is reported.

### Next steps

1. D-17: product investigation and fix (S1, blocks T-PIPE-05/06, T-FLT-05
   recompute, T-FLT-06/07 and anything under recompute with layerwise). The
   fix needs a test that fails a layerwise load mid-forward under recompute and
   checks token equality.
2. After the D-17 fix, rerun `stage3.sh pipe05 pipe06 pipe12 rdma06gpu` and
   `stage3_gpt.sh` (tree pinned again; product code then includes the D-14
   change `e9cd0689`, so cfg08/e2e04/e2e05 should be rerun too).
3. D-12 (broadened): report to the Aerospike server team with
   `e2e04/kvsink_e2e04_c4_short/asd-kvsink.log` (late completion on slot 0
   of a single-command fetch on a reused region). Until it is fixed, a
   T-E2E-04 "pipelined on every eligible request" pass needs either a
   no-fence/fixed server or an LMCache restart every 7 fetches.

## CPU half (cpu-prep-s3s5, 2026-10-01)

| Test | Result | Evidence (box: `/root/lmc-work/functional/stage3/logs/rdma06/`) |
|---|---|---|
| T-RDMA-06, fetches of at most 4 chunks | **pass** 100/100 keys byte-identical, in 4 out of 4 runs | `run1.txt`..`run4.txt`, `run*.junit.xml` (`test_rdma_equals_plain_gets_for_100_p_exact_keys_in_fetches_of_4_chunks`) |
| T-RDMA-06, fetches over 4 chunks (the P-exact-15 64-chunk prompt) | **blocked by D-12** (fell back in 2 out of 2 runs) | `run1.txt`, `run4.txt` (`test_pipelined_rdma_fetches_equal_plain_gets_for_100_p_exact_keys`); `asd-kvsink_run1.log`/`_run4.log`: 1 `late completion for slot 256` each, then 3775 / 3519 `region N in error state` lines |
| T-RDMA-06 GPU half (production KV bytes) | prepared, not run | `stage3.sh rdma06gpu`, `test_rdma_equals_plain_gets_for_100_keys_stored_by_vllm` |

**Keys.** The keys are the first 100 P-exact keys of
`corpus_llama-3.1-8b-instruct.json`: P-exact-00..15 whole, plus chunk 0 of
P-exact-16. They are derived by production code (`TokenHasher(256)`, then
`IPCCacheServerKey`, then `ipc_key_to_object_keys`), with vLLM's model name
(the Llama-3.1-8B snapshot path), world size 1 and worker 0.

**Payloads.** Every object uses the production Llama-3.1-8B layout: 32 MiB, written as a meta record
`<key>|m` plus 64 segment records `<key>|s|<i>` of 512 KiB. The bytes are synthetic: each object's
32-bit words are distinct across all 100 objects, so a misplaced segment is
detected. They are not real KV values; the GPU half covers that.

**Fetches.** The payloads are stored through LMCache's own Aerospike adapter.
The keys are then read back in 32 fetches of at most 4 chunks (256 slots, one
server command each):
- P-exact-00..14 as whole prompts of 1, 2 or 4 chunks;
- P-exact-15 as 16 fetches of 4 chunks;
- P-exact-16 chunk 0 alone.

Each fetch goes through the production pipelined path into a freshly
registered L1 RDMA window and must complete `PIPELINED`. Every landed object
is compared byte for byte with a plain (non-RDMA) get, and with the stored
payload.

Runs 2 and 3 ran only the 4-chunk test, and their kv-sink logs show no late
completions and no error lines. Runs 1 and 4 also ran the original 64-chunk
test, which hit D-12 exactly as in `item2/`.

**Finding (it shapes T-FLT-07 and T-PIPE-07).** rxe0 sits on `lo` and both
ends use GID 127.0.0.1, so RDMA writes bypass `lo` entirely. One run moved
6.72 GB over `lo`, which is the TCP stores and plain gets, 2 × 3.36 GB
(`lo_counters.txt`). The 3.36 GB of RDMA writes did not appear. So netem and
iptables cannot touch the RDMA path on this box (see `stage5/flt07_rdma_down.sh`).

## Defects found

| Sev | Defect | Evidence |
|---|---|---|
| S3 | `kvsink_smoke.sh` pytest crashed in 2 of 7 warm-ups: once `Fatal Python error: Aborted` in `alloc_pinned_ptr`, once a segfault in logging. Both times it was building the second test's `StorageManager`, right after the first test fell back on a cold server. This suggests a write after free from the abandoned fetch in a torn-down process (the test, not the server's steady state). `kvsink_restart` now retries the warm-up once. | segfault: `logs/smoke_crash/pipelined_it_segv.txt`; the abort's log was overwritten by the next dry run |

## Harness

[HARNESS.md](HARNESS.md) is the runbook. It covers each section's tests, faults
and pass criteria, the fault hooks that exist and are missing, and the CPU
dry-run results. Host changes are in [CHANGES.md](CHANGES.md); versions are in
[VERSIONS.md](VERSIONS.md).
