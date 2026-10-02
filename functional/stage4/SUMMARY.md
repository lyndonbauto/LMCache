# Stage 4: concurrency on the new kv-sink stack (GPU)

**Outcome: no wrong token in any Stage 4 run.** T-PIPE-08 passes: retrieves
beyond `window_count` are `refused` and served whole, and every output is
exact. T-E2E-09 passes on the plain path and on the pipelined path under
`--pipelined-shared-keys wait`. T-PIPE-10 is partial: both modes give the
documented outcome, but under `recompute` the busy request can only fail
cleanly (`fail` policy), because a recompute would hit D-17. New S3 finding
D-20: concurrent lookups of a shared prefix that is only in L2 report a
miss for all but one request, so those requests recompute (outputs stay
exact). Worker `gpu-stage4`, 2026-10-02 02:15-03:15Z, GPU time 02:18-03:09Z
(about 0.9 h). Versions are in [`VERSIONS.md`](VERSIONS.md), changes in
[`CHANGES.md`](CHANGES.md), and the harness in [`stage4.sh`](stage4.sh)
and [`HARNESS.md`](HARNESS.md). Box: `/root/lmc-work/functional/stage4/`.

Stack: LMCache `prototype-stage1`, product `b6b0caae` (1a + D-14), `lmc-c`
built against client `523d51ea`; kv-sink `8.1.3.0-111-g046e8558d` in
`aero-kvsink-bp` on 127.0.0.1:3700; Llama-3.1-8B, layerwise on, async
scheduling on, `VLLM_BATCH_INVARIANT=1`, policy `fail`.

## Results

| Test | What | Result | Evidence (box) |
| --- | --- | --- | --- |
| T-PIPE-08 (record) | One vLLM, `window_count` 2, 8 concurrent 4-chunk L2 hits | As designed: 8/8 `pipelined`, 0 `refused`, 8/8 exact (one vLLM serializes these retrieves) | `pipe08s/` |
| T-PIPE-08 | Two vLLMs, `--max-gpu-workers 2`, `window_count` 1, 4 + 4 concurrent L2 hits on distinct keys, 3 rounds | **pass**: 12 `refused` ("every RDMA window is leased (1) or quarantined"), each served whole; 12 `pipelined`; 24/24 exact, batch hits as modelled; no tracebacks, no kv-sink errors | `pipe08/` |
| T-PIPE-10 `recompute` | Both vLLMs send the same 4-chunk prompt at once, 5 prompts | **partial**: 5/5 pairs overlapped. In each pair one request was `pipelined` (exact) and the other `shared_keys_busy` (vLLM `Failing 1 request(s) due to KV load failure`, clean HTTP 500); both vLLMs alive. The recompute half (the busy request recomputed and exact) is blocked by D-17 | `pipe10/` (`*_recompute`) |
| T-PIPE-10 `wait` | The same under `wait` (1.0 s) | **pass on outcome**: 0 `shared_keys_busy`, 10/10 exact. The retrieves overlapped (starts 3-6 ms apart), but the waiter found the key absent (D-13 eviction after an L2 load) and fetched it again, so both requests were `pipelined` and none `reused`. `reused` is seen in T-E2E-09 below | `pipe10/` (`*_wait`) |
| ref16 | No LMCache, P-shared + P-multi at concurrency 16, twice | 60/60 exact against the batch-1 baseline on both runs (agreement 1.0); a vs b 60/60. Token equality is a valid oracle for T-E2E-09 | `ref16/agree_ref16.md` |
| T-E2E-09 plain | 16 clients, 60 prompts: cold, L1, L2 after restart; no pipelining | **pass**: 180/180 exact; top-1 agreement 8517/8517 = 1.0; L1 batch hits = model (69,120). L2 batch hits short of the model (D-20) | `e2e09/*plain*` |
| T-E2E-09 pipelined (`wait`) | The same with `--pipelined-fetch`, cap 4, `--pipelined-shared-keys wait` | **pass**: 180/180 exact; agreement 8517/8517 = 1.0; L2 outcomes 9 `pipelined`, 22 `reused`, 10 `not_deferred`, 0 `shared_keys_busy`, 0 failed requests. L2 hits short of the model (D-20) | `e2e09/*pipewait*` |
| T-E2E-09 pipelined (default `recompute`) | The same with the default shared-keys mode | Recorded, not the verdict: 179/180 exact, 1 `shared_keys_busy` = clean HTTP 500 under `fail`; agreement 8469/8469 over the served tokens. Run 1 had 3 busy retrieves (3 clean 500s) in its L2 send | `e2e09/*_pipe_*`, `run1_e2e09pipe/` |

Every LMCache shutdown was clean with the 60 s grace (22 × exit 143; none
needed kill -9). All 39 listener checks passed. None of the 11 kv-sink logs has a
late completion, region error, failed write or post, or dropped region.
Two vLLMs at `--gpu-memory-utilization 0.3` each fit, so 0.25 was not
needed.

## Findings

- **T-PIPE-08 keeps its meaning on the new protocol.** The sink-fetch
  driver registers the window range once and issues each layer's batch read
  on 16 worker threads. Admission is still per window: `RdmaWindowLeaser`
  grants one lease per window, `SinkFetchTable::begin` takes one fetch per
  window, and a retrieve with no free window is `refused` and loaded whole
  (`pipelined_loading.py`). With `window_count` 1 the second instance's
  retrieves were refused in all three rounds.
- **G-16 is only partly true.** One vLLM serializes retrieves of
  *distinct* keys (pipe08s: 0 `refused` at `window_count` 2). But in
  T-E2E-09 a single vLLM produced `reused` (23 in run 1, 19 and 22 here) and
  `shared_keys_busy` (3, 1) when P-multi turns that share a prefix were in
  flight together. A same-prefix overlap therefore does not need a second
  engine, and the default `--pipelined-shared-keys recompute` turns it into
  a failed request under `fail`, or a D-17 recompute under `recompute`.
- **D-20 (new, S3): concurrent same-prefix L2 lookups miss.** In every
  T-E2E-09 L2 send, when several of the 16 in-flight requests needed a
  prefix that was only in L2, one lookup found it (`found_count=8` for the
  P-shared prefix) and the others returned `found_count=0` or a shorter
  prefix. 31 of the 60 plain-path lookups found nothing, and vLLM recomputed
  those tokens. vLLM external hits were 25,088 (plain), 48,896 (`recompute`)
  and 34,816 (`wait`) against the modelled 69,120, while L1 sends matched
  exactly and Stage 2b's distinct-prompt concurrency from L2 matched too.
  Outputs stay exact. Suspected cause (not confirmed in code): a lookup does
  not count a key that another request's prefetch is loading
  (write-locked in L1) and does not wait for it. The prefetch controller's
  `max_in_flight` of 8 is a second candidate.
- **Heartbeat starts with the first request.** A vLLM that has served no
  request has no LMCache heartbeat thread, so it never notices an LMCache
  restart and never re-registers until a request arrives, and that request
  misses (the known S2 pre-heartbeat gap). The harness now sends one
  request to the second vLLM before any restart.

## Harness changes

| Commit | Change |
| --- | --- |
| `9decc574` | `stage4.sh` on the new stack: adapter port and precheck from `KVSINK_PORT`; `STOP_GRACE` (run_steps.sh/stage3.sh, default 5, Stage 4 uses 60); pipe10 under `fail` (`P10_POLICY`) because of D-17; e2e09 plain with `--no-l1-use-lazy`; `launch.sh` |
| `f79e00bf` | Fixes after run 1: pipe08 `--no-l1-use-lazy` (its server did not start); pipe10 primes vLLM 2 before the restart (it never re-registered); ref16 uses `logprob_agree.py` (`compare.py` refuses because the newer corpus file only adds sets; P-shared and P-multi are identical); e2e09 variants `plain pipe pipewait` with failed-request counts |

Known harness gap: `outcomes_of` (per-pair outcomes in pipe10) matches
client request IDs (`cmpl-<x>`) against LMCache sessions (`cmpl-<y>-0-<z>`),
which differ, so it prints nothing. The verdicts above come from
`hit_report.py`'s per-request retrieve column.

## Defects

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| D-20 (new) | S3 | Concurrent lookups of a shared prefix that is only in L2 return a miss for all but one request; those requests recompute (L2 hit totals 36-71% of the model at concurrency 16; outputs exact) | Open, cause not confirmed |
| D-17 | S1 (vLLM) | Recompute after a layerwise load fails mid-forward gives wrong tokens | Unchanged; blocks the recompute half of T-PIPE-10 and makes the default `--pipelined-shared-keys recompute` unusable under `recompute` |
| D-13 | Info | Chunks loaded from L2 are evicted from L1 after the retrieve | Seen in pipe10 `wait` (waiter fetches again) |
| D-18 | Info | Telemetry makes a clean shutdown take 14-17 s (Stage 3) | Worked around with `STOP_GRACE=60`: every restart here was a clean exit 143 |

## Next steps

- Decide the T-E2E-09 pipelined verdict mode: `wait` (used here), or treat
  the default `recompute` mode's clean 500s as acceptable until D-17 is
  fixed upstream.
- D-20: confirm the cause in the lookup/prefetch path (product
  investigation, needs an "Agent:" ask).
- T-PIPE-10 recompute half and the `P10_POLICY=recompute` run wait on a
  fixed vLLM (vllm#49250).
- Fix `outcomes_of` to map client IDs to LMCache sessions (harness).
