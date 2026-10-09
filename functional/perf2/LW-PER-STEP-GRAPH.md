# Layerwise: per-step CUDA graph mode (Task B)

Result: with a small vLLM patch and two LMCache connector hooks, lw decodes as fast as
aon at c=1 (5.49 vs 5.48 ms per token at 8k, 6.22 vs 6.21 ms at 16k), down from 7.6-7.7
ms with the global PIECEWISE downgrade, and keeps lw's faster first token. Full-hit
outputs are as close to aon as lw's ever were; the negative control shows the check
catches a broken hook. **Separately, lw partial hits give wrong outputs on about 6-12%
of requests, with or without this patch and over RDMA or TCP**; aon is exact. That is
a bug in LMCache's layerwise load, found while checking this patch (see "Partial
hits"). Full hits are exact in the same check (192/192 equal to aon), so the per-step
gain holds for full-hit workloads.

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

### Partial hits: lw gives wrong outputs, with or without this patch (open)

Partial hits: a stored 2k, 8k or 16k prefix plus an 8k new suffix, at c=1, and the 8k
prefix at c=4. Checked in batch-invariant mode (`PERF_BATCH_INVARIANT=1`), where outputs
do not depend on batch shape, against a no-cache run that sends the same prompts
(`PART_SALT_TAG`). Exact equality is the bar. Box results on `165.245.136.135`:
`functional/perf2/lwaon3/{bi,bi2,bi3,bi4}`.

| Run | Exact matches |
|---|---|
| aon (4 prompts per point), 2 runs | 32/32 |
| lw global PIECEWISE (old behavior), RDMA, 3 runs | 105/112 |
| lw per-step, RDMA, 4 runs | 112/128 |
| lw per-step, plain TCP L2 (`exp2_tcplw`, no RDMA), 2 runs | 88/96 |

- **The per-step patch is not the cause.** The old behavior fails too (7 of 112). The
  first two small runs (16/16 old vs 28/32 per-step) suggested otherwise only by
  chance. Per-step failed somewhat more often (16 of 128), but on these counts the
  difference is not established.
- **The RDMA path is not the cause.** Layerwise over plain TCP fails at the same rate.
  aon, which reads the same L2 data without layerwise loading, is exact.
- Failures are silent (0 errors, no timeouts) and look like wrong KV, not drift: some
  outputs diverge in the first characters and degenerate, others after 80-160
  characters.
- Temporary debug logging on the worker (`LWDBG`, `bi3`, reverted) shows the worker
  follows the wait protocol for bad loads as for good ones: every retrieve gets all 32
  layer waits in its own step, no retrieve starts while an older one still has waits
  outstanding, and the wait times look the same. So the wrong bytes most likely come
  from behind the wait: the daemon's per-layer copies or the event that says a layer
  has landed.
- Candidates to check next, none verified:
  1. A host staging buffer reused before its async H2D copy has run.
  2. The pooled per-ordinal IPC event re-recorded by the next retrieve before the
     worker's stream wait takes effect (ROCm IPC event semantics).
  3. The watermark (copies enqueued) covering an ordinal whose copy depends on data
     not yet in the source buffer.
- **Full hits are not affected.** Same check on full hits (`bi5`: 8k and 16k at c=1, 8k
  at c=4, 16 prompts per point): all 4 lw runs (2 old global PIECEWISE, 2 per-step)
  equal aon exactly, 192/192. One 8k prompt differs from no-cache in aon and in every
  lw run alike, so that is the reference (a full hit computes its last prompt token in
  a separate one-token step), not lw. The bug needs a partial hit: a load that overlaps
  a large prefill in the same step.

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

1. B4 second half: the 1e budget grid (off / 1 / 2 GiB, c = 4-32) with the patch.
2. lw partial-hit wrong outputs (see "Partial hits"): instrument the daemon's
   per-layer copy and event recording for loads that overlap a prefill.
3. Push `prototype-stage-1f` (needs approval); vLLM upstreaming needs approval.
