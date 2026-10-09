# Layerwise: per-step CUDA graph mode (Task B)

Result: with a small vLLM patch and two LMCache connector hooks, lw decodes as fast as
aon at c=1 (5.49 vs 5.48 ms per token at 8k, 6.22 vs 6.21 ms at 16k), down from 7.6-7.7
ms with the global PIECEWISE downgrade, and keeps lw's faster first token. Full-hit
outputs are as close to aon as lw's ever were; the negative control shows the check
catches a broken hook.

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
room to spare. The c = 4-32 budget grid (B4 second half) is not run yet.

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

### Partial hits: an older lw-vs-aon difference, not from this patch

Partial hits (`part` points: 2k, 8k and 16k stored prefixes plus an 8k new suffix) had
0 errors but lw outputs rarely equal aon's (0-2 of 4 per point). Repeats separate the
causes:

| Comparison | c=1 points (P2048, P8192, P16384) | All points |
|---|---|---|
| aon vs aon repeat | 12/12 | 74/84 |
| lw global PIECEWISE vs lw per-step (same data) | 12/12 | 82/84 |
| lw global PIECEWISE vs aon | 2/12 | 13/84 |
| first lw per-step run vs its repeat (new data file) | 3/12 | 18/84 |

- The patch does not change partial-hit outputs: the old global-PIECEWISE behavior
  gives the same text.
- lw and aon give different partial-hit outputs, and that predates this patch.
- The first lw run (its own `part` step and data file) differed from the later two lw
  runs, while aon is stable across both data files. This needs a look before trusting
  lw partial-hit outputs; it is not a Task B issue. Next check: one partial-hit prompt
  at c=1, lw vs aon, with batch-invariant mode, as the functional E2E tests did.

## Limits

- One run per correctness point; timing has one run per point with 16 requests (the
  earlier c=1 repeats agreed within 0.2 ms).
- Only model runner V2 was exercised; the V1 runner change is untested.
- `mp` executor only; TP=1.
- Debug logging was on for the correctness runs (B3), not for timing (B4).

## Next steps

1. B4 second half: the 1e budget grid (off / 1 / 2 GiB, c = 4-32) with the patch.
2. Partial-hit lw-vs-aon difference: the check above.
3. Push `prototype-stage-1f` (needs approval); vLLM upstreaming needs approval.
