# D-27 A/B: concurrent retrieves per worker, day 3 (2026-10-07)

Valentyn's Slack command: apply the D-27 fix from `retrieve-serialization.md` and compare
with the existing results. The fix is on `prototype-stage-1c` (`e8a6f6cc`, tests and docs
in `1db5a0ac`); `prototype-stage-1b` (`3df7712b`, product code unchanged since day 2) is
the baseline.

What 1c changes: the RETRIEVE handler does the L1 part, the lease and the generation
registration on the worker's thread, then hands the pipelined fetch to a pool of
`pipelined_window_count()` threads and returns. A tracker publishes the minimum layer
count over the step's in-flight retrieves; one failure stops them all. A per-worker lock
serializes each layer's staging copy, block IDs are cloned, and the native fetch priority
is the table's batch token (older requests first). Design: `docs/design/v1/layerwise/
system-design.md`, "Concurrent retrieves of one worker (D-27)".

Setup: droplet 165.245.134.42, Llama-3.1-8B, asd `3f3940e42` with `KV_SINK_STATS=1`,
`rdma.queue_pairs` 16, data on `/mnt/scratch`. Both versions ran the same grid
(`scripts/lwaon3.sh`, one server session per step, n=4 requests per point at c ≤ 4 and
n=c above), LMCache rebuilt in `lmc-c` for each. aon and lw share one stored data set per
length. TTFT p50 uses NumPy's linear interpolation. All points: no errors, full outputs.
Table: `lwaon3/report.md` (`scripts/lwaon3_report.py`); raw logs stay on the box under
`/root/lmc-work/functional/perf2/lwaon3/`.

## Result

D-27 works, but it does not make lw competitive with aon under concurrency here.

- With 1c, one worker's fetches overlap. The 1c log shows each 8k retrieve taking about
  0.75 s at c=8 while they finish about 0.12 s apart; on 1b they run back to back at
  about 0.134 s each. No fetch fell back to a whole load, and nothing failed.
- lw full hits get 5-11% faster at c ≥ 4 (8k c=32: 4.24 vs 4.67 s; 16k c=32: 8.19 vs
  9.17 s). Other points are within noise.
- lw is still 1.3-1.7x slower than aon at c ≥ 2. Both lw versions read the same bytes
  from the server (73.6 GB in the 8k session), and lw TTFT grows linearly with c (8k:
  c=32 is 24x c=1). The fetch path over Soft-RoCE is bandwidth-bound, so overlapping
  fetches only hides the per-request gaps and the layer compute, not the transfer.
- Partial hits are unchanged (lw / lw 1b = 1.01-1.02 at most points).

The worse 1c points (16k c=2 / 4: 1.10 / 1.21, 8k + 8k c=4: 1.16) come from how vLLM
split 4 requests into prefill steps, not from the fetch. A layerwise load blocks the
forward pass, so every request in one step gets about the same TTFT. At 16k c=4, 1b's
requests landed in separate steps (0.62-1.22 s, p50 1.05) and 1c's in one (all about
1.26 s).

## Table (p50 s)

| cached + new | c | aon 1b | aon 1c | lw 1b | lw 1c | lw 1c / lw 1b | lw 1c / aon 1c |
|---|---|---|---|---|---|---|---|
| 8k (full) | 1 | 0.196 | 0.199 | 0.174 | 0.177 | 1.01 | 0.89 |
| 8k (full) | 2 | 0.236 | 0.248 | 0.330 | 0.332 | 1.01 | 1.34 |
| 8k (full) | 4 | 0.478 | 0.463 | 0.645 | 0.615 | 0.95 | 1.33 |
| 8k (full) | 8 | 0.731 | 0.805 | 1.294 | 1.199 | 0.93 | 1.49 |
| 8k (full) | 16 | 1.371 | 1.359 | 2.376 | 2.174 | 0.91 | 1.60 |
| 8k (full) | 32 | 2.457 | 2.466 | 4.667 | 4.241 | 0.91 | 1.72 |
| 16k (full) | 1 | 0.374 | 0.398 | 0.332 | 0.341 | 1.03 | 0.85 |
| 16k (full) | 2 | 0.442 | 0.441 | 0.613 | 0.677 | 1.10 | 1.54 |
| 16k (full) | 4 | 0.979 | 1.005 | 1.053 | 1.272 | 1.21 | 1.27 |
| 16k (full) | 8 | 1.478 | 1.514 | 2.339 | 2.445 | 1.05 | 1.61 |
| 16k (full) | 16 | 2.759 | 2.608 | 4.586 | 4.489 | 0.98 | 1.72 |
| 16k (full) | 32 | 4.987 | 4.915 | 9.172 | 8.194 | 0.89 | 1.67 |
| 2k + 8k | 1 | 0.716 | 0.726 | 0.655 | 0.679 | 1.04 | 0.94 |
| 2k + 8k | 4 | 2.382 | 2.383 | 2.299 | 2.041 | 0.89 | 0.86 |
| 8k + 8k | 1 | 1.250 | 1.258 | 1.068 | 1.090 | 1.02 | 0.87 |
| 8k + 8k | 2 | 1.755 | 1.757 | 1.630 | 1.643 | 1.01 | 0.94 |
| 8k + 8k | 4 | 2.929 | 3.040 | 3.271 | 3.802 | 1.16 | 1.25 |
| 8k + 8k | 8 | 6.061 | 6.056 | 5.879 | 5.948 | 1.01 | 0.98 |
| 8k + 8k | 16 | 9.749 | 9.252 | 10.111 | 10.260 | 1.01 | 1.11 |
| 8k + 8k | 32 | 18.835 | 18.980 | 18.713 | 18.879 | 1.01 | 0.99 |
| 16k + 8k | 1 | 1.968 | 1.970 | 1.591 | 1.616 | 1.02 | 0.82 |
| 16k + 8k | 4 | 5.203 | 5.190 | 4.888 | 4.960 | 1.01 | 0.96 |

## Against day 2 (`LW-VS-AON.md`, asd `314564cfb`, repeats a / b)

| tokens | c | lw day 2 | lw 1b today | lw 1c | aon day 2 | aon today (1b / 1c) |
|---|---|---|---|---|---|---|
| 8k | 1 | 0.191 / 0.191 | 0.174 | 0.177 | 0.271 / 0.215 | 0.196 / 0.199 |
| 8k | 2 | 0.291 / 0.388 | 0.330 | 0.332 | 0.324 / 0.243 | 0.236 / 0.248 |
| 8k | 4 | 0.600 / 0.762 | 0.645 | 0.615 | 0.530 / 0.481 | 0.478 / 0.463 |
| 16k | 1 | 0.361 / 0.349 | 0.332 | 0.341 | 0.383 / 0.412 | 0.374 / 0.398 |
| 16k | 2 | 0.689 / 0.677 | 0.613 | 0.677 | 0.428 / 0.453 | 0.442 / 0.441 |
| 16k | 4 | 1.377 / 1.372 | 1.053 | 1.272 | 0.900 / 0.904 | 0.979 / 1.005 |

Today's server (`3f3940e42`) is a little faster for lw at c=1 than day 2's. Day 2's
reading that lw loses at c ≥ 2 holds with D-27.

## Notes

- Two external power-offs of the droplet (16:23:39 and 16:55:42 UTC) interrupted the 1b
  runs; see `functional/HOST-CHANGES.md`. 1b's aon 8k points come from the 16:49 session
  (saved before the second power-off); the other 1b steps ran from 17:19, with the server
  restarted on the stored 8k data. 1c ran in one go from 18:00 to 18:46.
- Single run per point; the day-2 repeats show about 10-30% spread at c=2-4.
