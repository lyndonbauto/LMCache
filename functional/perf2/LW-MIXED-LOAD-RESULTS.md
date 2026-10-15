# Layerwise per-request load mode under mixed load: test results

Plan: [`LW-MIXED-LOAD-PROPOSAL.md`](LW-MIXED-LOAD-PROPOSAL.md). Earlier single-class
results: [`LW-ASYNC-FULL-HITS.md`](LW-ASYNC-FULL-HITS.md). Code: `prototype-stage-1g`
at `18db855b` (default R = 0.05).

Three configs replay the same open-loop traces, where misses, L1 hits and L2 hits
of every kind arrive together with outputs of 128 to 8192 tokens:

| Config | Load mode |
|---|---|
| **aon** | layerwise off: every hit loads async |
| **lw R=0** | every hit layerwise (1f's behavior, "before") |
| **lw R=0.05** | per request: layerwise iff `new_chunks >= 0.05 * hit_chunks`, else async (1g) |

## Conclusion

**lw R=0.05 is the best of the three for the batch as a whole.** It keeps
layerwise's TTFT gain where it picks layerwise, and keeps aon's decode
smoothness.

- **TTFT, layerwise-picked classes** (L2 partial hits with a long suffix,
  borderline L2 hits, most L1 partial hits): lw R=0.05 matches lw R=0. Both are
  0.2-0.6 s faster than aon at the median, for example in S2 at 0.35 req/s:
  0.37 / 0.35 s against aon's 0.67 s.
- **TTFT, async-picked classes** (full hits, short suffixes): lw R=0.05 equals
  aon. lw R=0 is 0.1-0.4 s faster here too (except L1 full hits in S2 and S3,
  where all three are within 0.12 s): under mixed load, a layerwise full hit
  costs less than in the full-hits-only test, so sending it async gives up some
  TTFT.
- **Decode smoothness:**
  - lw R=0 adds stalls for everyone. Gaps over 100 ms per minute are higher than
    aon's in 12 of 13 points, by up to 2.3x (S3 at 0.45 req/s: 118 against 51).
    p99.9 gaps double at light load (S3 at 0.2 req/s: 147 ms against 16 ms), and
    p99 gaps triple in S2 at 0.35 req/s (52 ms against 18 ms).
  - lw R=0.05 stays within 6% of aon's stall rate at light and moderate load. At
    the heaviest points it has 11-17% fewer stalls than aon (S1 at 0.9, S4 at
    0.88 and 1.12 req/s); why is not explained yet.
- **Misses:** same TTFT in all three configs. Their decode suffers the extra
  stalls under lw R=0.
- **Throughput:** the same in every config at every point (output tokens per
  second within 1-3%). 0 errors and 0 engine stops in all 39 traces.

The mode the rule gives for each request's found hit matched vLLM's own load-mode
log lines exactly in every trace, every lookup found its planned hit, and the L1
and L2 hit tokens equal the plan.

## Setup

- MI300X droplet, Llama-3.1-8B, TP=1, vLLM 0.27.1 with the per-step piecewise
  patch, `--distributed-executor-backend mp`, prefix caching off (so a repeated
  prompt is an LMCache L1 hit).
- LMCache server with an Aerospike kv-sink L2 (Soft-RoCE `rxe0`, 16 queue pairs,
  `--pipelined-fetch` for lw). L1: 100 GB general (plus 8 RDMA windows of 2 GiB for
  lw). One data file per scenario phase, at most 1200 GB (Aerospike Community
  Edition refuses 1447 GB).
- Request classes, all in one trace (`make_trace.py`):

  | Class | Prompt | Hit from | Mode at R=0.05 |
  |---|---|---|---|
  | miss | 2k-16k fresh tokens | none | none |
  | l2_full | stored 8k or 16k document | L2 | async |
  | l2_long | document + 2k-8k fresh | L2 | layerwise |
  | l2_short | 16k document + 256-768 fresh | L2 | async |
  | l2_border | 16k document + 1024 fresh | L2 | layerwise |
  | l1_full | an earlier miss or L1 prompt, resent | L1 | async |
  | l1_part | an earlier miss or L1 prompt + 256-2048 fresh | L1 | by ratio |

- Outputs: 50% 128 tokens, 30% 1k, 15% 4k, 5% 8k (`ignore_eos`). Poisson
  arrivals, open loop. Every config replays the same trace; fresh tokens are
  salted per session, so nothing one config stores is a hit for the next.
- Scenarios (class shares in the proposal): **S1** chat (mostly L1 partial
  hits), **S2** RAG (mostly short-suffix and borderline L2 hits), **S3** agent
  (mostly full hits), **S4** cold start (60% misses, no L1).
- Rates: aon was calibrated per scenario (3-minute traces on a ladder, stopping
  at the first unstable rate), then each config ran at 0.4, 0.7 and 0.9 of the
  highest stable rate for 240 s (S4: 101 s, to fit the data file):

  | Scenario | Highest stable aon rate | Main rates (req/s) |
  |---|---|---|
  | S1 | 1.0 (1.5 unstable) | 0.40, 0.70, 0.90 |
  | S2 | 0.5 (0.75 unstable) | 0.20, 0.35, 0.45 |
  | S3 | none (0.5 unstable) | 0.20, 0.35, 0.45 |
  | S4 | 1.25 (top of ladder) | 0.50, 0.88, 1.12 |

- Metrics: TTFT per class; gaps between streamed tokens (every gap, not a mean);
  stalls = gaps over 100 ms, per minute of trace; goodput = share of requests
  with TTFT <= 2 s and their own p99 gap <= 100 ms.
- Raw results: `lwaon3/m` (local, untracked). Per-class tables:
  `lwaon3/m/report_S<n>.md` (`functional/perf2/scripts/report_mixed.py`).

## Whole trace (all classes)

TTFT is the median over the class group: **lw-picked** = l2_long, l2_border,
l1_part; **async-picked** = l2_full, l2_short, l1_full (the groups R=0.05 sends
each way; l1_part is mostly layerwise). Each cell: aon / lw R=0 / lw R=0.05.

| Point | TTFT lw-picked (s) | TTFT async-picked (s) | TTFT miss (s) | gap p99 (ms) | gap p99.9 (ms) | stalls /min | goodput % |
|---|---|---|---|---|---|---|---|
| S1 0.40 | 0.39 / 0.26 / 0.30 | 0.17 / 0.17 / 0.18 | 0.61 / 0.61 / 0.59 | 16 / 16 / 16 | 497 / 484 / 485 | 131 / 149 / 133 | 85 / 85 / 84 |
| S1 0.70 | 0.72 / 0.44 / 0.53 | 0.64 / 0.41 / 0.62 | 0.70 / 0.78 / 0.78 | 120 / 148 / 111 | 662 / 723 / 715 | 327 / 388 / 335 | 41 / 41 / 43 |
| S1 0.90 | 0.79 / 0.46 / 0.50 | 0.68 / 0.41 / 0.66 | 0.94 / 0.94 / 0.92 | 240 / 253 / 236 | 1031 / 1037 / 1045 | 902 / 840 / 771 | 5 / 8 / 8 |
| S2 0.20 | 0.59 / 0.33 / 0.34 | 0.50 / 0.34 / 0.52 | 0.16 / 0.16 / 0.16 | 11 / 11 / 10 | 13 / 18 / 13 | 8 / 11 / 8 | 100 / 100 / 100 |
| S2 0.35 | 0.67 / 0.35 / 0.37 | 0.55 / 0.34 / 0.54 | 0.59 / 0.59 / 0.59 | 18 / 52 / 28 | 452 / 492 / 461 | 106 / 119 / 102 | 86 / 87 / 90 |
| S2 0.35 repeat | 0.66 / 0.36 / 0.37 | 0.55 / 0.34 / 0.56 | 0.58 / 0.59 / 0.57 | 18 / 52 / 22 | 450 / 492 / 468 | 104 / 116 / 102 | 87 / 88 / 89 |
| S2 0.45 | 0.65 / 0.37 / 0.38 | 0.52 / 0.35 / 0.55 | 0.21 / 0.22 / 0.21 | 17 / 17 / 17 | 279 / 390 / 374 | 98 / 129 / 103 | 95 / 94 / 95 |
| S3 0.20 | 0.65 / 0.35 / 0.36 | 0.10 / 0.09 / 0.11 | 0.10 / 0.09 / 0.10 | 12 / 12 / 12 | 16 / 147 / 22 | 10 / 14 / 10 | 100 / 98 / 100 |
| S3 0.35 | 0.69 / 0.44 / 0.51 | 0.30 / 0.19 / 0.29 | 0.48 / 0.48 / 0.48 | 16 / 17 / 16 | 152 / 308 / 146 | 33 / 50 / 35 | 98 / 99 / 100 |
| S3 0.45 | 0.59 / 0.37 / 0.38 | 0.25 / 0.20 / 0.26 | 0.28 / 0.28 / 0.28 | 17 / 18 / 17 | 165 / 318 / 165 | 51 / 118 / 50 | 94 / 87 / 95 |
| S4 0.50 | 1.18 / 0.82 / 0.79 | 0.86 / 0.54 / 0.92 | 1.00 / 0.86 / 0.88 | 72 / 79 / 72 | 1006 / 1002 / 1006 | 204 / 233 / 202 | 57 / 45 / 55 |
| S4 0.88 | 1.65 / 1.31 / 1.28 | 0.87 / 0.50 / 0.96 | 0.55 / 0.64 / 0.62 | 254 / 262 / 257 | 1015 / 1023 / 1026 | 535 / 560 / 474 | 11 / 17 / 19 |
| S4 1.12 | 2.49 / 1.93 / 1.86 | 1.17 / 0.81 / 1.13 | 0.90 / 0.91 / 0.90 | 513 / 514 / 516 | 1044 / 1041 / 1044 | 937 / 1083 / 773 | 11 / 5 / 17 |

## Selected classes

TTFT p50 (s), aon / lw R=0 / lw R=0.05, and the mode R=0.05 picked (L layerwise,
A async). From `report_S<n>.md`. Cells with 2-3 requests: S1 l2_border, S3
l2_border, S4 l2_short and l2_border.

| Class | S1 0.70 | S2 0.35 | S3 0.45 | S4 0.88 |
|---|---|---|---|---|
| l2_long | 1.14 / 0.80 / 0.80 L | 1.23 / 0.78 / 0.78 L | 1.34 / 0.83 / 0.82 L | 2.15 / 1.36 / 1.31 L |
| l2_border | 2.49 / 1.82 / 1.75 L | 0.67 / 0.35 / 0.37 L | 0.76 / 0.37 / 0.37 L | 1.21 / 0.51 / 0.48 L |
| l1_part | 0.60 / 0.35 / 0.38 mostly L | 0.26 / 0.19 / 0.23 mostly L | 0.33 / 0.21 / 0.24 mostly L | (none) |
| l2_short | 0.90 / 0.50 / 0.93 A | 0.57 / 0.35 / 0.57 A | 0.56 / 0.35 / 0.56 A | 1.98 / 1.83 / 1.90 A |
| l2_full | 1.01 / 0.65 / 0.90 A | 0.45 / 0.20 / 0.40 A | 0.32 / 0.21 / 0.29 A | 0.82 / 0.45 / 0.84 A |
| l1_full | 0.58 / 0.32 / 0.57 A | 0.11 / 0.23 / 0.19 A | 0.14 / 0.16 / 0.15 A | (none) |

Stalls over 100 ms per minute by class show where R=0's extra stalls land: in S3
at 0.45 req/s, l1_full 21 / 49 / 21, l2_full 12 / 27 / 11, l2_short 9 / 19 / 9
(aon / R=0 / R=0.05).

## Noise

S2 at 0.35 req/s was run twice per config (same trace structure, fresh tokens):
TTFT group medians within 0.02 s, stall rates within 3 per minute (R=0.05: 102
and 102), p99 gap within 6 ms. Differences larger than that in the tables are
not noise. S4's points have only 58-124 requests (101 s traces), so its per-class
cells with few requests (l2_short, l2_border: 1-4 requests) are not reliable.

## Limits

- **S2 and S3 ran light.** The calibration check (p90 TTFT of the last third at
  most max(2x, +1 s) of the first third) marked S2 at 0.75 and S3 at 0.5 req/s
  unstable by a margin of 0.06 s, so their main rates are about 60% of their
  likely capacity. S4 passed the same check at a heavier load (p90 TTFT 4-5 s),
  because its many long-prefill misses make TTFT high from the start. A rerun of
  S2 and S3 at 0.6 and 0.75 req/s is planned.
- **S4 traces are 101 s** (the data file cap), not 240 s.
- **R=0.05's extra smoothness at the heaviest points** (fewer stalls than aon) is
  measured but not explained.
- **L1 classes reuse earlier misses and L1 prompts only**, whose KV sits in
  general L1 in every config. A reused prompt that was loaded by a pipelined
  fetch stays in an RDMA window instead (8 windows), so it may fall back to L2
  sooner under lw; not tested.
- Calibration ran on aon only; lw configs ran at aon's rates.
