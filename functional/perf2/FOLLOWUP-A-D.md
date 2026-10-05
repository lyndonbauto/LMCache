# perf2 follow-up: D-27 fix A and D-30 instrumentation D

Asked by Valentyn in `#aie-agent-output` on 2026-10-05 ("Agent: start with A and D", from
the proposals in the same thread). Same droplet, builds and data layout as
[SUMMARY.md](SUMMARY.md): server `9c16972132`, client `5a24afdbb6`, Llama-3.1-8B, device
namespace on `/mnt/scratch`, Soft-RoCE `rxe0` on `lo`.

## A: the worker's layer wait as a no-progress timeout (D-27)

**Change** (`2eefa049`, `lmcache/v1/multiprocess/layer_progress.py`): the deadline of
`LayerProgressWaiter` restarts whenever the shared generation or watermark changes. Before,
it was a fixed 5 s from the start of the wait, so a retrieve queued behind older generations
on the same worker timed out while those were still moving, and vLLM's engine stopped. A
daemon that stops moving still fails the wait after 5 s. Two unit tests cover it
(`tests/v1/multiprocess/test_layer_progress.py`; both fail on the old code).

**Rerun** (`scripts/fixa.sh`, `fixa/`): 8k and 16k full hits at c = 16 and 32, 16 queue pairs,
the default 5 s wait. p50 TTFT in seconds (interpolated, `aggregate.py`):

| | 8k c=16 | 8k c=32 | 16k c=16 | 16k c=32 |
|---|---|---|---|---|
| lw, default wait, with A | 3.364 | **6.663** | 6.516 | **12.941** |
| lw, default wait, sweep (no A) | 3.370 | engine stop | 6.324 | engine stop |
| lw, 600 s wait, sweep | 3.346 | 6.538 | 6.421 | 12.63 |
| aon (this run) | 1.462 | 2.642 | 2.849 | 5.301 |

All 8 points valid, 0 request errors, every lw retrieve `pipelined` (51 per length), no
layer-progress timeouts and no D-28 errors. With A the default wait behaves like the 600 s
one (within 2-3%). A removes the engine stops only; the retrieves are still serialized, so lw
TTFT still doubles with c and stays above aon.

## D: where the sink path loses time (D-30)

**Method** (`scripts/trace.sh`): a box-only patch to the server (never committed or pushed;
applied in place, built, reverted, the original binary restored byte for byte, md5
`0b953fb4…`) records five timestamps per placement: submit (the batch row arrived), pick
(the scheduler admitted it under the in-flight budgets), place (a placement thread took it),
post (record read and copied to a staging slot, RDMA write posted) and complete (its
completion reaped). It also adds env overrides for the per-queue-pair budget. Each run is
8k full hits, c=1, 4 requests, one data file; `scripts/trace_analyze.py` reports the median
retrieve. Raw traces are on the box under `/root/lmc-work/functional/perf2/trace/` and in the
local archive.

Times are the median op's, in ms; in flight is time-weighted over the retrieve.

| Run | GiB/s | lw TTFT p50~ (s) | sched wait | pool wait | place | wire | in flight (writes) | queued (MiB) |
|---|---|---|---|---|---|---|---|---|
| 1 QP | 1.39 | 0.772 | 334.6 | 0.02 | 0.30 | 2.40 | 8.0 | 372 |
| 1 QP, budget 16 MiB / QP (4x) | 1.51 | 0.706 | 301.7 | 0.02 | 0.31 | 9.86 | 31.8 | 359 |
| 16 QPs | 4.30 | 0.236 | 85.5 | 1.77 | 0.56 | 0.53 | 27.0 | 302 |
| 16 QPs, 16 placement threads | 5.69 | 0.207 | 65.4 | 0.42 | 0.86 | 0.80 | 28.5 | 299 |
| 16 QPs, 32 placement threads | **5.98** | **0.200** | 66.6 | 0.06 | 1.06 | 0.97 | 29.7 | 323 |

aon c=1 in the same sessions: 0.208 to 0.226 s. `lw TTFT p50~` is the session log's
approximate p50 of 4 requests.

Findings:

1. **The client keeps the server fed.** In every run the server always had ops of the
   retrieve queued (300-370 MiB on average, never idle), and the median op waited 65-335 ms
   in the scheduler. The client's 16 batch workers are not the limit; the server holds the
   work back.
2. **At 1 QP the queue pair is the limit, not the budget.** The 4 MiB budget keeps exactly
   8 writes in flight. Four times the budget fills the server's 32-write cap, but the rate only
   goes from 1.39 to 1.51 GiB/s while each write's wire time grows from 2.4 to 9.9 ms: one
   Soft-RoCE queue pair drains about 1.5-1.6 GiB/s of these writes. Raw `ib_write_bw` gets 2.30
   at both 8 and 128 outstanding. The remaining difference is not isolated: `ib_write_bw`
   rewrites one hot 512 KiB buffer per queue pair, while the sink path copies each record to
   one of 32 staging slots and writes into a 16 GiB window.
3. **At 16 QPs the placement pool and the 32-write cap are the limits.** With the default 8
   placement threads an admitted write waits 1.77 ms for a thread, three times its wire time.
   `KV_SINK_PLACE_THREADS` (an existing env var, no rebuild) at 16 raises the rate 32%, to 5.69
   GiB/s; at 32, to 5.98 GiB/s, and lw at 8k c=1 is then at aon (0.200 against 0.223 s in the
   same session). In flight is then 29.7 of `KV_SINK_MAX_IN_FLIGHT` 32, the next cap; raising it
   needs a server change (staging slots are a 32-bit mask).

Suggested next steps, all server-side (Sriram's branch):

- Default `KV_SINK_PLACE_THREADS` higher than 8 when the region uses several queue pairs, or
  scale it with the queue pairs registered. This is the cheapest gain measured here.
- Raise `KV_SINK_MAX_IN_FLIGHT` above 32 (a wider staging-slot mask, more staging memory) and
  measure at 16 QPs with 32 placement threads.
- The per-queue-pair budget can stay at 4 MiB on Soft-RoCE; recheck it on a hardware NIC,
  where one queue pair is not CPU-bound.

## Box changes

In [CHANGES.md](CHANGES.md) (follow-up section) and `functional/HOST-CHANGES.md`.
