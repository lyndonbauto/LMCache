# Where layerwise (lw) beats all-or-nothing (aon), day 2 (2026-10-06)

Task 2 of the day-2 control-tower prompt: find a setup where lw over Soft-RoCE beats aon
over TCP. Droplet 134.199.201.175, Llama-3.1-8B, LMCache `prototype-stage-1b` unchanged.
The server is asd `314564cfb` with the Q3 settings (`BREAKDOWN2.md`): device namespace,
`KV_SINK_PLACE_THREADS=32`, `KV_SINK_MAX_IN_FLIGHT=128`, `KV_SINK_STATS=1`, and
`rdma.queue_pairs` 16. aon and lw run in the same server cycle on the same stored
prompts. Every point was run in two repeats (a, b), each with n=4 requests.

- Script: `scripts/lwaon.sh`; controls: `scripts/lwaon_ctl.sh`, `scripts/lwaon_ctl2.sh`;
  table: `scripts/lwaon_report.py`. Outputs: `lwaon/` (`report.md`, `results.csv` with
  72 rows, `controls.txt`, `progress.txt`).
- TTFT p50 is NumPy's linear interpolation over a point's requests, as in
  `functional/perf/`. All 72 points are valid: no failed requests, full outputs, and
  external prefix-cache hits ≥ 95% of the stored tokens.
- "lw wins" means lw's p50 is below aon's in both repeats.

## Result

lw wins in two cases:

1. **One request at a time, any full-hit length.** lw is 6-30% faster than aon from 8k to
   128k tokens. Recompute is far behind (day 1: 0.31 s at 8k, 25 s at 128k).
2. **A partial hit with a long cached prefix and a short new part.** lw beats both aon and
   recompute: 8k cached + 2k new 0.26 s (aon 0.44, recompute 0.45), and 16k + 2k 0.41 s
   (aon 0.74, recompute 1.01). lw loads layer by layer while the new tokens prefill. aon
   loads everything first.

lw loses at full hits with 2 or 4 concurrent requests: 1.5-1.6x slower at 16k, and at
8k c=4. Retrieves for one vLLM worker still run one at a time (D-27). With 8k new
tokens after a 2k or 8k cache, plain recompute beats both lw and aon. aon is also
slower than recompute at those points, so caching there costs more than it saves (cause
not isolated).

## Full hits (p50 s, repeat a / b)

| tokens | c | aon | lw | lw / aon | lw wins |
|---|---|---|---|---|---|
| 8k | 1 | 0.271 / 0.215 | 0.191 / 0.191 | 0.70 / 0.89 | yes |
| 8k | 2 | 0.324 / 0.243 | 0.291 / 0.388 | 0.90 / 1.59 | no |
| 8k | 4 | 0.530 / 0.481 | 0.600 / 0.762 | 1.13 / 1.58 | no |
| 16k | 1 | 0.383 / 0.412 | 0.361 / 0.349 | 0.94 / 0.85 | yes |
| 16k | 2 | 0.428 / 0.453 | 0.689 / 0.677 | 1.61 / 1.49 | no |
| 16k | 4 | 0.900 / 0.904 | 1.377 / 1.372 | 1.53 / 1.52 | no |
| 32k | 1 | 0.777 / 0.770 | 0.673 / 0.651 | 0.87 / 0.84 | yes |
| 64k | 1 | 1.498 / 1.411 | 1.351 / 1.320 | 0.90 / 0.94 | yes |
| 128k | 1 | 3.040 / 2.815 | 2.611 / 2.658 | 0.86 / 0.94 | yes |

Day 1 (server `9c16972132`, 8 placers, cap 32): lw 0.232 vs aon 0.205 at 8k c=1, and
0.437 vs 0.388 at 16k. So the Q3 server settings turned a 13% loss into a win at c=1.

Repeat a's aon at 8k c=1 (0.271) is an outlier. Repeat b gave 0.215, and the control on
day 1's `9c16972132` at defaults gave 0.208 / 0.239 / 0.483 at c=1 / 2 / 4. Day 1 itself
was 0.205 / 0.263 / 0.496. The 8k c=1 win holds against all of them.

## Partial hits (p50 s, repeat a / b; recompute = the whole prompt uncached)

| cached + new | c | aon | lw | recompute | fastest |
|---|---|---|---|---|---|
| 2k + 2k | 1 | 0.184 / 0.180 | 0.147 / 0.152 | 0.141 | recompute (lw +4-8%) |
| 2k + 8k | 1 | 0.688 / 0.688 | 0.638 / 0.640 | 0.445 | recompute |
| 8k + 2k | 1 | 0.444 / 0.442 | 0.263 / 0.265 | 0.447 | **lw** (0.59x aon) |
| 8k + 8k | 1 | 1.211 / 1.203 | 1.030 / 1.025 | 0.820 | recompute |
| 16k + 2k | 1 | 0.754 / 0.724 | 0.405 / 0.417 | 1.010 | **lw** (0.54-0.58x aon) |
| 16k + 8k | 1 | 1.858 / 1.857 | 1.523 / 1.521 | 1.522 | lw = recompute |
| 2k + 8k | 4 | 2.267 / 2.283 | 1.966 / 2.216 | 1.548 | recompute |
| 8k + 8k | 4 | 2.841 / 2.872 | 3.238 / 3.582 | 3.710 | aon |
| 16k + 8k | 4 | 4.990 / 4.951 | 4.720 / 4.707 | 6.358 | **lw** (0.95x aon) |

lw beats aon at 8 of 9 partial-hit points in both repeats. The recompute column is one
run (`lwaon_ctl2.sh`), added after the repeats to check whether the partial-hit wins
matter.

## Validity and deviations

- Full hits: the 32-prompt store used `FS_PCT` 140 (45G / 90G files) and ended at 71%
  used with `stop_writes true` in both repeats. Every record was present (66,690 of
  66,560 needed at 8k; 133,250 of 133,120 at 16k), and the points only read. Partial
  and long steps stayed under 62% and 6%.
- The planned control of aon on untuned `314564cfb` was not run. `lwaon_ctl.sh` was
  stopped after the `9c16972132` control to run Sriram's round 2 (`BREAKDOWN3.md`)
  first.
- n=4 per point, so p50 is the mean of the middle two requests. The repeats agree
  within ~10% except 8k c=1 aon (above) and the lw c=2-4 full hits at 8k.
