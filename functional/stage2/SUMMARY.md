# Functional testing, Stage 2a: the plain path end to end on Llama-3.1-8B

Setup: Aerospike CE 8.2 as L2 (`127.0.0.1:3000`, namespace `lmcache`), no
RDMA, no pipelined fetch, Llama-3.1-8B-Instruct in `lmc-c`, LMCache at
`00cd3eee`. Every test ran twice, layerwise off and then on (worker wait 5 s).
`VLLM_BATCH_INVARIANT=1`, temperature 0, batch size 1, default async
scheduling, `kv_load_failure_policy` `fail`, vLLM and LMCache HTTP on
127.0.0.1. Each session truncates the Aerospike set, starts LMCache and vLLM
(vLLM's own prefix cache off, so every hit is LMCache's), sends the prompt
set once ("cold": computed and stored) and again ("warm": served from cache).
Oracle: token equality with the recorded batch-invariant baseline
`functional/day1/step4/bi_run1.json` (every set, including P-multi and
P-long, has one; 130/130 deterministic against `bi_run2.json`).

Evidence is on the box under `/root/lmc-work/functional/stage2/<test>/`:
session output, both logs, the client JSON (now with per-request metric
deltas), the Aerospike statistics before and after each send, and
`report_<session>.md`, which checks every request. The driver is
`stage2a.sh`; the T-E2E-10 table is `metrics_table.md`. GPU time: 37
minutes (12 sessions, 15:58 to 16:35 UTC on 2026-10-01).

## Results by test ID

| Test | What it checks | Result | Evidence (under `stage2/`) |
| --- | --- | --- | --- |
| T-E2E-01 | P-short: output equal, zero L2 traffic | **Partial.** Output 80/80 equal (cold and warm, layerwise off and on); no L2 lookup, read or load; warm send zero Aerospike traffic. But the cold send wrote 390 records: the 6 prompts of 216 to 255 tokens plus their 48 generated tokens fill a chunk, and LMCache stores that chunk (6 chunks x 65 records). The 14 prompts where prompt + output stays under 256 tokens caused no traffic | `e2e01/` |
| T-E2E-02 | P-exact and P-ragged, warm send hits L1 | **Pass.** 80/80 equal per mode. Each warm hit is exactly the full chunks: P-ragged 257 -> 256, 511 -> 256, 12543 -> 12288; P-exact a whole-prompt hit (n - 1, vLLM computes the last token). All from L1, 40/40 `not_deferred` | `e2e02/` |
| T-E2E-03 | Same, LMCache restarted so the hit comes from L2 | **Pass.** 80/80 equal per mode; vLLM re-registered 4 s after restart; 35,964 Aerospike reads plus 468 batch reads in the warm send; LMCache hit tokens 0 from L1 and 141,312 from L2; 40/40 `not_deferred` | `e2e03/` |
| T-E2E-05 | P-long at 64 chunks (the cap) and 65 | **Partial: pipelined half in Stage 3.** Plain path: 20/20 equal per mode; 16384 -> 16383 and 16640 -> 16639 hit tokens; every retrieve `not_deferred` (as expected with pipelined fetch off) | `e2e05/` |
| T-E2E-06 | P-shared: 10 requests on one 2048-token system prompt | **Pass.** 20/20 equal per mode. Cold send: request 1 misses, requests 2 to 10 each hit 2048 tokens (8 chunks); warm: 10/10 hit 2048 | `e2e06/` |
| T-E2E-07 | P-multi: 10 five-turn conversations | **Pass.** 100/100 equal per mode. Cold send: turn 1 misses; turn k hits exactly the previous turn's full chunks (e.g. 366, 728, 1085, 1443, 1806 tokens -> 0, 256, 512, 1024, 1280); warm: every turn hits its own full chunks | `e2e07/` |
| T-E2E-10 | `pipelined_outcome`, deferred-retrieve counter and vLLM external hit rate match each setup | **Pass** for these runs. Every retrieve `not_deferred`; `lmcache_mp_num_deferred_retrieves_total` never incremented; vLLM external hit tokens equal the expected hit on 680/680 requests (table below) | `metrics_table.md`, `*/report_*.md` |

### T-E2E-10 per test (layerwise off; layerwise on is identical in every column)

| Test | Send | Requests | Retrieves by outcome | Deferred counter | vLLM external hit rate | LMCache hit tokens L1 / L2 |
| --- | --- | --- | --- | --- | --- | --- |
| T-E2E-01 | cold / warm | 20 / 20 | none / none | 0 / 0 | 0.0% / 0.0% | 0 / 0 |
| T-E2E-02 | cold | 40 | none | 0 | 0.0% (0/143816) | 0 / 0 |
| T-E2E-02 | warm | 40 | not_deferred 40 | 0 | 98.2% (141292/143816) | 141312 / 0 |
| T-E2E-03 | warm (after restart) | 40 | not_deferred 40 | 0 | 98.2% (141292/143816) | 0 / 141312 |
| T-E2E-05 | warm | 10 | not_deferred 10 | 0 | 100.0% (165110/165120) | 165120 / 0 |
| T-E2E-06 | cold | 10 | not_deferred 9 | 0 | 84.6% (18432/21780) | 18432 / 0 |
| T-E2E-06 | warm | 10 | not_deferred 10 | 0 | 94.0% (20480/21780) | 20480 / 0 |
| T-E2E-07 | cold | 50 | not_deferred 40 | 0 | 56.6% (30720/54318) | 30720 / 0 |
| T-E2E-07 | warm | 50 | not_deferred 50 | 0 | 89.5% (48640/54318) | 48640 / 0 |

"Expected hit" in the per-request reports comes from replaying the session
against a model of the cache (every full chunk of every prompt already sent
is stored; a whole-prompt hit reads `n - 1`). LMCache's lookup counts the
whole chunk (`n`), vLLM's external counter `n - 1`, hence 141,312 vs 141,292
(20 whole-prompt hits).

## Defects and findings

No S1 or S2. No errors or tracebacks in any LMCache or vLLM log.

| Severity | Finding | Cause | Owner |
| --- | --- | --- | --- |
| Info (test design) | T-E2E-01's "zero L2 traffic" does not hold for P-short prompts of 209 tokens or more: prompt + 48 generated tokens complete a chunk, and LMCache stores that chunk to L1 and L2. No reads, loads or lookups happen, and outputs are equal | The corpus assumes only prompt tokens are stored; the MP connector also stores KV computed during decode | Test plan / corpus (decision below) |
| Info | After an LMCache restart, the warm send rewrote the same 6 decode-completed chunks (390 records) that were already in L2: for P-ragged prompts at N chunks + 255 tokens, the chunk finished by generated tokens is not a prefix of the next prompt, so it is recomputed and stored again without checking L2 | The store path deduplicates against L1 only | L2 store controller (write amplification, not correctness) |

Known items seen but not re-filed: S2 recompute gap before the next heartbeat
after a restart (the harness waits for re-registration, 4 s here).

## Decisions for the humans (T-E2E-01)

1. Accept decode-token storage as intended and amend the pass rule to "no
   L2 lookup, read or load; writes only for chunks completed by generated
   tokens". Under that rule T-E2E-01 passes now.
2. Rebuild P-short so prompt + `max_tokens` stays under one chunk (lengths at
   most 208 with `max_tokens` 48), record its baseline, and rerun (about 5
   GPU-minutes).
3. Keep P-short as is and run it with `max_tokens` small enough per prompt;
   the oracle is then a prefix of the recorded baseline output.

## Harness changes (committed under `functional/harness/`)

- `client.py --metrics-urls`: per-request growth of vLLM and LMCache counters,
  and vLLM's request id so LMCache log lines can be joined to it.
- `l2_stats.py`: dump and diff Aerospike namespace statistics.
- `hit_report.py`: per-request check of output equality, expected hit,
  deferred retrieves and `pipelined_outcome`.
- `run_lmcache.sh`: passes `--metrics-urls`; `L2_STATS=1` dumps Aerospike
  statistics around each send.

## Next steps

- Decide T-E2E-01 (above).
- Stage 3: the pipelined half of T-E2E-05 and T-E2E-04, which need the
  `kv-sink` server build.
- Optional: rerun T-E2E-06 and 07 with the restart between sends, so their
  prefix hits also come from L2 (not required by the plan).

# Stage 2b: prefix semantics, tenant and model isolation, concurrency, T-E2E-01 redo

Same setup as Stage 2a (Aerospike CE 8.2 L2 on 127.0.0.1:3000, no RDMA, no
pipelined fetch, `VLLM_BATCH_INVARIANT=1`, temperature 0, default async
scheduling, `kv_load_failure_policy` `fail`), product code unchanged since
`00cd3eee` (box tree at `49f18d12`). Equality tests ran at batch size 1,
layerwise off then on. GPU time: 45 minutes (9 sessions plus two
baseline runs, 16:50 to 17:35 UTC on 2026-10-01).

The driver is `stage2b.sh`; sessions are scripted with
`functional/harness/run_steps.sh` (send a prompt subset, restart, truncate
L2, snapshot or delete L2 records, switch models). Evidence is under
`stage2/{base2b,e2e01v2,lkp03,lkp04,conc,lkp05}/` on the box:
`report_<session>.md` checks each request (output equal to the baseline,
vLLM external hit = expected hit, no deferred retrieve, `not_deferred`
outcome); the T-E2E-10 table is `metrics_table_2b.md`.

New oracle files: `base2b/bi_run1.json` covers P-short-v2 and P-prefix,
deterministic against `bi_run2.json` from a second fresh server (28/28).
Every other set uses `day1/step4/bi_run1.json`; the v2 corpus builds those
sets token-identical to v1.

## Results by test ID

| Test | What it checks | Result | Evidence (under `stage2/`) |
| --- | --- | --- | --- |
| T-E2E-01 | P-short-v2 (20 prompts of 48-208 tokens, so prompt + 48 output tokens stays in one chunk): output equal, zero L2 traffic | **Pass** (option 2 of the Stage 2a decision). 80/80 equal (cold and warm, both modes). The Aerospike namespace counters did not move between the cold send's start and the warm send's end: zero reads, writes, batch or lookup traffic; no L2 store or lookup log lines | `e2e01v2/` |
| T-LKP-03 | Prefix semantics: hits at 0, 1, 3, 5 and 6 of 6 chunks, and a gap | **Pass**, both modes. B = P-ragged-11 (6 chunks + 99 tokens) stored; variants changed in chunk 0, 1, 3, 5 hit exactly 0, 256, 768, 1280 tokens, B itself 1536. Gap: B2 stored chunk by chunk (2, then 1, then 3 chunks per send), the 65 records of chunk 2 alone deleted from Aerospike, LMCache restarted (L1 empty): B2 hit 512 tokens (chunks 0-1) from L2, 2 chunks loaded, chunks 3-5 not used though present (`L2 prefetch lookup completed: 2 prefix hits`; 130 record reads for the load). 22/22 equal | `lkp03/` |
| T-LKP-04 | P-salt: salt B never hits salt A's entries | **Pass**, both modes. L1: A, then B, then A. B's first request hit 0 tokens (L1 0, L2 0) although A had stored its whole prefix; B's later requests hit only the prefix B's first request stored; A's repeat hit 2048 on 10/10. L2: L2 truncated, A sent, LMCache restarted, then B: B's first request again hit 0 (Aerospike: 8 lookups not found, no reads); A afterwards hit 2048 from L2 on 10/10. 120/120 equal | `lkp04/` |
| T-LKP-05 | Same prompt, different model: no hit | **Pass (model half)**, both modes. One LMCache server and one Aerospike set: Llama-3.1-8B stored P-exact and P-shared; vLLM restarted with gpt-oss-120b (registered with 36 layers after Llama unregistered) and sent the **same token IDs**: P-exact 0 hits from L1 (20/20); after an LMCache restart, P-shared's first request 0 hits from L2 (8 lookups not found). Llama re-served afterwards hit its own entries from L2 on 30/30 (equal to the baseline), so the entries were there. No errors in any log. `--gpu-memory-utilization 0.45` in this test only, so the two models fit one after the other. **TP half: N/A on this box (one GPU)** | `lkp05/` |
| Concurrency regression (layerwise + async deadlock fix `6eb51a66`) | 4, 8 and 16 concurrent cached requests, layerwise on, async scheduling on | **Pass.** P-ragged stored once, then batches of 4, 8, 16 from L1, and after an LMCache restart before each batch, 4, 8, 16 from L2. Every batch finished in about 3 s (timeout 600 s); 56/56 concurrent outputs token-equal to the batch-size-1 baseline; batch hit totals equal the expected hit (for example 16 requests: 49,152 tokens, L1 or L2 as set up); every retrieve `not_deferred`. No hang, no HANG marker, no engine error | `conc/` |
| T-E2E-10 | `pipelined_outcome`, deferred counter, vLLM external hit tokens per setup | **Pass** for these runs (plain path). 478 requests: every retrieve `not_deferred`, the deferred-retrieve counter never incremented, external hit tokens = expected on every sequential request and every concurrent batch | `metrics_table_2b.md` |

### T-E2E-10 rows (layerwise off; layerwise on is identical in every column)

| Session | Send | Requests | Retrieves by outcome | Deferred | vLLM external hit | LMCache hit tokens L1 / L2 |
| --- | --- | --- | --- | --- | --- | --- |
| T-E2E-01 v2 | cold / warm | 20 / 20 | none / none | 0 / 0 | 0 / 2524 tokens, both | 0 / 0 |
| T-LKP-03 | probe (0, 1, 3, 5, 6 chunks) | 5 | not_deferred 4 | 0 | 3840 = expected 3840 | 3840 / 0 |
| T-LKP-03 | gap probe (after restart) | 1 | not_deferred 1 (2 chunks) | 0 | 512 = expected 512 | 0 / 512 |
| T-LKP-04 | B first pass, L1 | 10 | not_deferred 9 | 0 | 18432 = expected (request 1: 0) | 18432 / 0 |
| T-LKP-04 | B first pass, after restart | 10 | not_deferred 9 | 0 | 18432 = expected (request 1: 0) | 18432 / 0 |
| T-LKP-04 | A repeat, after restart | 10 | not_deferred 10 | 0 | 20480 = expected | 0 / 20480 |
| Concurrency | 4 / 8 / 16 from L1 (layerwise on) | 4 / 8 / 16 | not_deferred 4 / 8 / 16 | 0 | 30720 / 41984 / 49152 = expected | all L1 |
| Concurrency | 4 / 8 / 16 from L2 (layerwise on) | 4 / 8 / 16 | not_deferred 4 / 8 / 16 | 0 | 30720 / 41984 / 49152 = expected | all L2 |
| T-LKP-05 | gpt-oss on Llama's P-exact (L1) | 20 | none | 0 | 0 = expected 0 | 0 / 0 |
| T-LKP-05 | gpt-oss on Llama's P-shared (after restart) | 10 | not_deferred 9 (its own prefix) | 0 | 18432 = expected (request 1: 0) | 18432 / 0 |
| T-LKP-05 | Llama again | 30 | not_deferred 30 | 0 | 111340 = expected | 0 / 111360 |

## Defects and findings

No S1, S2 or S3. No tracebacks or ERROR lines in any LMCache or vLLM log.

| Severity | Finding | Cause | Owner |
| --- | --- | --- | --- |
| Info (D-13) | Chunks prefetched from L2 are evicted from L1 right after the retrieve (`L1 eviction: 2 keys` after `L1 read finished`). A repeat of the gap probe therefore read all 6 chunks from L2 again (L1 hit 0), although chunks 2-5 had just been stored to L1: the L1 lookup counts a leading run from chunk 0, and chunk 0 was gone | Prefetch buffers are temporary in L1 | L1 / prefetch controller (locality, not correctness) |
| Info | The L2 lookup reads every key of the request (5 found, 1 not found for the gap probe) and then uses the leading run; keys after the first miss cost one read each but are never loaded | Lookup design | none |

Known items seen, not re-filed: the LMCache server often outlives the
harness's 5 s SIGTERM grace and is SIGKILLed (19 times across the Stage 2a
and 2b sessions; no effect on results; the known "ignores SIGTERM" trap);
S3 gpt-oss block size 16.

## Next steps

- Stage 3: the pipelined halves of T-E2E-04, 05 and 10, and T-E2E-09 (16
  concurrent clients over P-shared and P-multi, which batch invariance now
  allows with token equality).
- The TP half of T-LKP-05 needs a multi-GPU droplet.
