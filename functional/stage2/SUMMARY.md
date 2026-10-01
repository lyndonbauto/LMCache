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
