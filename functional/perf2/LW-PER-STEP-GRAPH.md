# Layerwise: per-step CUDA graph mode (Task B)

Result: with a small vLLM patch and two LMCache connector hooks, lw decodes as fast as
aon at c=1 (5.49 vs 5.48 ms per token at 8k, 6.22 vs 6.21 ms at 16k), down from 7.6-7.7
ms with the global PIECEWISE downgrade, and keeps lw's faster first token. Full-hit
outputs are as close to aon as lw's ever were; the negative control shows the check
catches a broken hook. Partial hits are correct too: lw equals aon exactly on 96/96
partial-hit requests. An earlier suspected lw partial-hit bug was a reference artifact:
aon differs from a no-cache run on exactly the same requests (see "Partial hits").
At c = 4-32 the patch changes decode speed little (within about 10% of global
PIECEWISE); there lw's decode cost comes from pauses for other requests' transfers,
which the patch does not address (see "Timing (c = 4-32)").

Plan: `LW-ITL-FIX-PLAN.md` Task B. Raw results: box
`/root/lmc-work/functional/perf2/lwaon3/{b3,b3pw,b3neg,b4,b4pw,b3p,b3ppw}`.

## What changed

- **vLLM 0.27.1 patch** (local to the box, not upstreamed):
  [`vllm-0.27.1-per-step-piecewise.patch`](vllm-0.27.1-per-step-piecewise.patch)
  (8 files; apply with `patch -p1 -d .../dist-packages/vllm`).
  - `KVConnectorBase_V1`: `supports_per_step_piecewise(extra_config)` and
    `requires_piecewise_for_step(metadata)`, both False by default.
  - `VllmConfig`: if the connector supports the per-step check, keep
    `FULL_AND_PIECEWISE` instead of downgrading the whole run to PIECEWISE.
  - Model runner V2 (the box's runner) and V1: when the connector flags a step, exclude
    FULL for that step (it falls back to PIECEWISE, then eager; both run the layer hooks).
  - `MultiConnector` forwards both hooks.
- **LMCache** `prototype-stage-1f` (`8191c2ca`, on 1e, local only), in
  `LMCacheMPConnector`:
  - `supports_per_step_piecewise`: True when `lmcache.mp.use_layerwise` is on.
  - `requires_piecewise_for_step`: True when the step's metadata has a `RETRIEVE`
    request (exactly when `start_load_kv` submits one), and in every step when the
    experimental dispatcher is on (its `save_kv_layer` works in every step).
  - `requires_piecewise_for_cudagraph` still returns True, so an unpatched vLLM still
    downgrades the whole run.
  - 9 new unit tests (`tests/v1/test_mp_connector_layerwise_scheduler.py`; 30/30 pass in
    `lmc-c`); pre-commit clean; design doc `docs/design/v1/multiprocess/layerwise-load.md`
    updated.

## Setup

MI300X, Llama-3.1-8B, server `3f3940e42`, lw `queue_pairs` 16, `VLLM_EXECUTOR=mp`
(Task A1), async scheduling, 16 full-hit prompts per point, 128 output tokens,
temperature 0. Correctness runs log each step's graph mode (`VLLM_LOGGING_LEVEL=DEBUG`);
timing runs do not. `perf_client.py` now records each request's text.

## Timing (c=1, full hits, INFO logging)

| Context | Run | Time between tokens, median | TTFT p50 | Wall, 16 requests |
|---|---|---|---|---|
| 8k | aon, full graphs | 5.48 ms | 0.199 s | 14.33 s |
| 8k | lw per-step (new) | 5.49 ms | 0.164 s | 13.82 s |
| 8k | lw global PIECEWISE (old) | 7.74 ms | 0.169 s | 18.68 s |
| 16k | aon, full graphs | 6.21 ms | 0.379 s | |
| 16k | lw per-step (new) | 6.22 ms | 0.308 s | 17.64 s |
| 16k | lw global PIECEWISE (old) | 7.60 ms | 0.313 s | 20.58 s |

0 errors everywhere. The plan's pass bar (lw per-step within 5% of aon) is met with
room to spare.

## Timing (c = 4-32, full hits, admission budgets)

Same setup, 32 prompts per point, one run per point; lw per-step at
`LW_ADMIT_BUDGET` off / 1 / 2 GiB, and lw global PIECEWISE (old) at budget off. Box
results: `functional/perf2/lwaon3/{b4g,b4gpw}`. 0 errors in every run. Time between
tokens is the per-request mean over its 128 tokens; the column is the mean over
requests.

| Context, c | Run | TTFT p50 | TTFT p90 | Time between tokens | Max gap |
|---|---|---|---|---|---|
| 8k, 4 | aon | 0.24 s | 0.26 s | 6.9 ms | 19 ms |
| | lw per-step, off / 1 / 2 GiB | 0.47 / 0.43 / 0.54 s | 0.65 / 0.58 / 0.59 s | 8.0 / 8.5 / 8.2 ms | 422 / 166 / 323 ms |
| | lw PIECEWISE, off | 0.59 s | 0.60 s | 8.7 ms | 425 ms |
| 8k, 16 | aon | 1.11 s | 1.94 s | 9.1 ms | 21 ms |
| | lw per-step, off / 1 / 2 GiB | 2.06 / 0.74 / 1.23 s | 2.23 / 1.94 / 2.02 s | 14.5 / 21.5 / 19.9 ms | 1872 / 164 / 294 ms |
| | lw PIECEWISE, off | 1.93 s | 2.08 s | 15.0 ms | 1700 ms |
| 8k, 32 | aon | 2.63 s | 4.34 s | 8.8 ms | 23 ms |
| | lw per-step, off / 1 / 2 GiB | 2.84 / 2.63 / 2.45 s | 4.37 / 4.37 / 4.10 s | 34.3 / 40.4 / 40.9 ms | 2585 / 180 / 300 ms |
| | lw PIECEWISE, off | 4.46 s | 4.46 s | 32.4 ms | 3234 ms |
| 16k, 4 | aon | 0.41 s | 0.44 s | 8.2 ms | 31 ms |
| | lw per-step, off / 1 / 2 GiB | 0.88 / 0.61 / 0.60 s | 1.20 / 0.97 / 0.93 s | 10.8 / 12.4 / 12.8 ms | 841 / 377 / 374 ms |
| | lw PIECEWISE, off | 1.14 s | 1.23 s | 11.5 ms | 584 ms |
| 16k, 16 | aon | 3.50 s | 4.15 s | 9.2 ms | 22 ms |
| | lw per-step, off / 1 / 2 GiB | 2.94 / 1.73 / 1.52 s | 4.44 / 3.68 / 3.80 s | 27.4 / 36.9 / 37.5 ms | 2839 / 382 / 383 ms |
| | lw PIECEWISE, off | 4.03 s | 4.47 s | 25.6 ms | 3498 ms |
| 16k, 32 | aon | 4.95 s | 8.22 s | 9.7 ms | 27 ms |
| | lw per-step, off / 1 / 2 GiB | 7.04 / 4.97 / 4.99 s | 8.69 / 8.51 / 8.51 s | 58.0 / 75.1 / 74.7 ms | 6617 / 374 / 385 ms |
| | lw PIECEWISE, off | 9.26 s | 9.27 s | 63.2 ms | 4995 ms |

c = 8 is left out; it falls between c = 4 and c = 16 in every column.

- **Per-step barely changes decode speed at c >= 4.** Against global PIECEWISE at
  budget off, time between tokens is within about 10% either way (8k c=32: 34.3 vs 32.4
  ms; 16k c=32: 58.0 vs 63.2 ms). With many requests arriving together, most steps
  contain a retrieve, so they run PIECEWISE and wait for layers anyway. Per-step does
  lower TTFT p50 at c=32 (8k 2.84 vs 4.46 s, 16k 7.04 vs 9.26 s).
- **lw decode is far behind aon at c >= 8** (aon stays at 8-10 ms; lw 15-75 ms). This
  is the plan's cost 2: decode steps pause while other requests' layers arrive. The
  graph-mode fix cannot remove it; Task C (full hits loaded outside the forward pass)
  targets it.
- **The budget trades decode speed for gaps and TTFT.** 1 or 2 GiB caps the max gap at
  about 165-300 ms (8k) and 375-390 ms (16k), against 0.4-6.6 s with budget off, and
  lowers TTFT p50 at c = 16-32 (16k c=16: 1.5-1.7 s vs 2.9 s off and 3.5 s aon). It
  raises the mean time between tokens by 15-50% (8k c=16: 21.5 vs 14.5 ms).
- Against aon, lw's best budget wins on TTFT p50 only at c = 16 (8k 0.74 vs 1.11 s, 16k
  1.52 vs 3.50 s) and ties at c=32; it loses at c = 4.

## Correctness

### Graph modes

In every patched run, decode steps ran FULL and every flagged step ran PIECEWISE or
eager: no step with `connector_needs_piecewise=True` ran FULL.

| Run | FULL | PIECEWISE, flagged | Eager, flagged |
|---|---|---|---|
| lw per-step 8k, c = 1 and 4 | 2612 | 27 | 0 |
| lw per-step 16k, c = 1 and 4 | 2614 | 31 | 0 |
| lw per-step, partial hits | 2789 | 11 | 84 |

### Outputs, full hits

Graph mode alone changes greedy outputs on these random-token prompts: aon (FULL
decode) and lw global PIECEWISE differ on 3 of 16 requests at 8k c=1, some from the
third character. So "exactly equal to global PIECEWISE" is not a usable bar; the
useful comparison is with aon, whose decode steps also run FULL.

| Context, c | lw per-step = aon | lw per-step = lw global PIECEWISE | lw global PIECEWISE = aon |
|---|---|---|---|
| 8k, 1 | 15/16 | 12/16 | 13/16 |
| 8k, 4 | 13/16 | 13/16 | 13/16 |
| 16k, 1 | 15/16 | 16/16 | 15/16 |
| 16k, 4 | 16/16 | 15/16 | 15/16 |

The one 8k c=1 difference from aon (request 14) starts at character 26 of 623, in
coherent text: numeric drift, not missing KV.

### Negative control

`requires_piecewise_for_step` forced to always return False (8k, c = 1 and 4), then
reverted (`git checkout --`; the chain checked that no product file stayed changed):

- Every loading step ran FULL, so `wait_for_layer_load` never ran.
- 0/16 outputs equal aon or lw at both concurrencies; most differ from the first
  character, with generic text as if the context were empty.
- TTFT p50 fell to 0.028 s (nothing waits for the KV). No errors, no timeouts, no engine
  stop.

So a broken hook fails silently with wrong tokens, and this output check catches it.

### Partial hits: lw equals aon; the no-cache reference is not exact

Partial hits: a stored 8k or 16k prefix plus an 8k new suffix, at c=1, and the 8k
prefix at c=4, 16 prompts per point. Checked in batch-invariant mode
(`PERF_BATCH_INVARIANT=1`) against a no-cache run that sends the same prompts
(`PART_SALT_TAG`). Box results on `165.245.136.135`: `functional/perf2/lwaon3/{fs,
fsaon_F1,fsaon_F2}` (decisive), earlier runs in `{bi,bi2,bi3,bi4}`.

Decisive run (`fs`, `fsaon_*`): two prompt sets (salts F1, F2), each run with
no-cache, aon and lw per-step, data file 15x the stored prefixes so Aerospike never
reaches stop-writes.

| Prompt set, point | aon = no-cache | lw = no-cache | lw = aon |
|---|---|---|---|
| F1, 16k prefix, c=1 | 14/16 | 14/16 (same 2 requests) | 16/16 |
| F1, 8k prefix, c=1 | 16/16 | 16/16 | 16/16 |
| F1, 8k prefix, c=4 | 16/16 | 16/16 | 16/16 |
| F2, 16k prefix, c=1 | 14/16 | 14/16 (same 2 requests) | 16/16 |
| F2, 8k prefix, c=1 | 14/16 | 14/16 (same 2 requests) | 16/16 |
| F2, 8k prefix, c=4 | 16/16 | 16/16 | 16/16 |

- **lw partial hits are correct**: 96/96 equal aon. The differences from no-cache are
  identical in aon and lw, request by request and down to the first differing
  character.
- **The no-cache reference is not exact for partial hits.** A partial hit computes
  only the suffix against loaded prefix KV; no-cache prefills the whole prompt in one
  pass. Batch-invariant mode makes outputs independent of batch shape, not of that
  split, so some greedy outputs differ, sometimes from the first token (near-tied
  logits on these random-token prompts). About 4% of requests here.
- **Why it looked like an lw bug**: the earlier aon check had only 32 requests (0
  differences is likely at a 4-8% rate), and the earlier lw runs compared only with
  no-cache. In those runs (`bi3`, `bi4`) every lw suffix store also failed with
  `AEROSPIKE_ERR_SERVER_FULL`, because the data file (`EXP_FS_PCT=300`) filled up
  during the chain; that did not cause the differences either (the `fs` run had none
  and the same rate).
- Full hits (`bi5`, 192/192 lw = aon) behave the same way: one 8k prompt differs from
  no-cache in aon and every lw run alike.

The first comparison (B3 night run) compared aon and lw texts directly and found 13 of
84 equal. That comparison was invalid: the harness salts the partial-hit suffix with the
session name, so aon and lw sessions sent different prompts.

## Limits

- One run per correctness point; timing has one run per point with 16 requests (the
  earlier c=1 repeats agreed within 0.2 ms).
- Only model runner V2 was exercised; the V1 runner change is untested.
- `mp` executor only; TP=1.
- Debug logging was on for the correctness runs (B3), not for timing (B4).

## Next steps

1. Task C (needs approval): the c >= 4 grid shows decode pauses from other requests'
   transfers, not the graph mode, dominate lw's decode cost at load.
2. Output checks: compare lw with aon (same prompts), not with no-cache; size the
   data file for the suffix stores (`EXP_FS_PCT` 1500 for partial-hit chains).
3. Push `prototype-stage-1f` (needs approval); vLLM upstreaming needs approval.
