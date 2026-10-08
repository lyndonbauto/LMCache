# Layerwise admission under a byte budget, day 3 (2026-10-07 / 08)

Valentyn's Slack command (22:57 UTC): admit pipelined requests into steps in groups
bounded by a byte budget, so early requests of a burst get their first token early, and
report TTFT p50 / p90 and inter-token latency against aon. The change is on
`prototype-stage-1e` (`b4ad7386`, off `prototype-stage-1d` `f1d1a2c9`; the runs used
`d58af30c`, which differs only in the design doc).

What 1e changes: `LMCacheMPConnector.get_num_new_matched_tokens` sizes each loading
request as (tokens to load) x (KV bytes per token from vLLM's KV cache config) and asks a
`LayerwiseAdmissionGate`. A request that would push the step past
`lmcache.mp.layerwise_inflight_budget_bytes` gets `(None, True)` and stays in vLLM's
waiting queue; FIFO (once one is held, the rest of the step is held); the first loading
request of a step is always admitted; the gate resets once per step in
`build_connector_meta`, so no exit path can leak budget. Default 4 GiB, 0 = off (today's
behavior). Design: `docs/design/v1/layerwise/system-design.md`, section 12.

Setup: droplet 165.245.134.42, Llama-3.1-8B (128 KiB of KV per token: an 8k request is
1 GiB, a 16k request 2 GiB), asd `3f3940e42` with `KV_SINK_STATS=1`, `rdma.queue_pairs`
16, data on `/mnt/scratch`, `scripts/lwaon3.sh` (one server session per step, n=4
requests per point at c ≤ 4 and n=c above, 128 output tokens). One store per length
serves every budget (`lw8@<G>` steps). aon control: 1c's runs (`D27-AB.md`); aon does
not use the gate. TTFT p50 / p90 use NumPy's linear interpolation. Every point: no
errors, full outputs, every lw request pipelined. Report script:
`scripts/lw_admission_report.py`; raw results in `lwaon3/1e` (requested grid), `1e2`
(1 GiB at 8k) and `1e-sync` (async scheduling off), untracked.

## Result

With the harness's vLLM settings (`--async-scheduling`), admission brings lw close to aon
only at high concurrency. Without async scheduling, a budget of about one request per
step (1-2 GiB here) makes lw p50 match aon at every c ≥ 4. Either way the price is decode
stalls: early requests wait one group's transfer (about 0.14 s per GiB) between their
first and second token, and mean inter-token latency at c=32 is 3.5-6.5x aon's (2.5-5x
with the gate off).

- **The gate works as designed.** TTFTs form a staircase of groups, about 0.14 s per GiB
  apart (the Soft-RoCE link, ~7 GiB/s): 8k c=16 at 2 GiB lands in pairs 0.28 s apart.
  p90 does not move, since the last group still waits for the whole burst's transfer.
- **Async scheduling costs every group but the last one group's transfer.** vLLM's
  engine core submits step N+1 before it emits step N's tokens, and with the in-process
  executor the submit runs N+1's forward on the engine thread, which a layerwise load
  blocks until N+1's data lands. 8k c=4 at 1 GiB (TTFT per request, s):

  ```text
  sync   0.19  0.35  0.50  0.65    each token leaves after its own load
  async  0.35  0.50  0.63  0.64    ... after the next request's load
  ```

  On the server, 8k c=8 at 2 GiB finished pairs at 0.32 / 0.59 / 0.88 / 1.14 s; the
  client saw 0.62 / 0.90 / 1.17 / 1.18 s.
- **Async scheduling (harness default):** best budget per point vs aon 1c:
  8k c=4 1.07-1.22 (noisy), c=8 1.05 (1 GiB), c=16 1.09, c=32 1.06; 16k c=4-32
  1.03-1.10 at 2 GiB. Without the gate: 1.34-1.78.
- **Async scheduling off:** at 2 GiB, 8k c=4-32 is 1.07 / 0.96 / 0.98 / 1.01 x aon 1c
  and 16k is 0.80 / 0.93 / 0.98 / 0.97. aon itself is a little slower without async
  scheduling (8k c=32: 2.70 s vs 2.47-2.58 s), so against aon in the same mode lw is
  below aon at c ≥ 8 and within 4% at 8k c=4.
- **The 4 GiB default is too large for this model and link.** 4 GiB is four 8k requests
  per step; it helps only at c ≥ 16 (8k c=32: 2.98 s vs 4.38 s off, aon 2.47 s).
- **Partial hits are unchanged** (lw / aon the same at every budget): they are bound by
  the prefill of the 8k new tokens, not by the fetch.

## TTFT, async scheduling (harness default), p50 / p90 (s)

`off` is the cap disabled; ratios are p50 over aon 1c.

| cached + new | c | aon 1c | lw 1c | off | 1G | 2G | 4G | 8G | off / aon | 1G / aon | 2G / aon | 4G / aon | 8G / aon |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 8k (full) | 1 | 0.199 / 0.229 | 0.177 / 0.203 | 0.180 / 0.208 | 0.180 / 0.200 | 0.182 / 0.193 | 0.182 / 0.189 | 0.176 / 0.201 | 0.90 | 0.91 | 0.92 | 0.91 | 0.88 |
| 8k (full) | 2 | 0.248 / 0.364 | 0.332 / 0.347 | 0.341 / 0.350 | 0.338 / 0.350 | 0.321 / 0.325 | 0.321 / 0.343 | 0.316 / 0.337 | 1.38 | 1.37 | 1.30 | 1.30 | 1.28 |
| 8k (full) | 4 | 0.463 / 0.642 | 0.615 / 0.622 | 0.619 / 0.622 | 0.566 / 0.639 | 0.629 / 0.637 | 0.494 / 0.602 | 0.636 / 0.637 | 1.34 | 1.22 | 1.36 | 1.07 | 1.37 |
| 8k (full) | 8 | 0.805 / 1.223 | 1.199 / 1.201 | 1.219 / 1.230 | 0.845 / 1.220 | 1.038 / 1.179 | 1.201 / 1.219 | 1.187 / 1.188 | 1.52 | 1.05 | 1.29 | 1.49 | 1.47 |
| 8k (full) | 16 | 1.359 / 2.182 | 2.174 / 2.182 | 2.229 / 2.263 | 1.476 / 2.326 | 1.657 / 2.353 | 1.957 / 2.233 | 2.258 / 2.293 | 1.64 | 1.09 | 1.22 | 1.44 | 1.66 |
| 8k (full) | 32 | 2.466 / 4.198 | 4.241 / 4.284 | 4.379 / 4.418 | 2.619 / 4.379 | 2.751 / 4.521 | 2.975 / 4.455 | 3.450 / 4.422 | 1.78 | 1.06 | 1.12 | 1.21 | 1.40 |
| 16k (full) | 1 | 0.398 / 0.413 | 0.341 / 0.379 | 0.317 / 0.362 | - | 0.324 / 0.352 | 0.329 / 0.363 | 0.332 / 0.356 | 0.80 | - | 0.81 | 0.83 | 0.83 |
| 16k (full) | 2 | 0.441 / 0.653 | 0.677 / 0.684 | 0.466 / 0.625 | - | 0.630 / 0.643 | 0.493 / 0.661 | 0.498 / 0.656 | 1.06 | - | 1.43 | 1.12 | 1.13 |
| 16k (full) | 4 | 1.005 / 1.381 | 1.272 / 1.287 | 1.206 / 1.219 | - | 1.059 / 1.210 | 1.169 / 1.181 | 1.158 / 1.172 | 1.20 | - | 1.05 | 1.16 | 1.15 |
| 16k (full) | 8 | 1.514 / 2.371 | 2.445 / 2.450 | 2.260 / 2.262 | - | 1.673 / 2.372 | 2.008 / 2.293 | 2.257 / 2.265 | 1.49 | - | 1.10 | 1.33 | 1.49 |
| 16k (full) | 16 | 2.608 / 4.352 | 4.489 / 4.545 | 4.415 / 4.422 | - | 2.823 / 4.506 | 3.159 / 4.595 | 3.601 / 4.391 | 1.69 | - | 1.08 | 1.21 | 1.38 |
| 16k (full) | 32 | 4.915 / 8.396 | 8.194 / 8.290 | 8.770 / 8.780 | - | 5.039 / 8.618 | 5.437 / 8.709 | 5.660 / 8.517 | 1.78 | - | 1.03 | 1.11 | 1.15 |
| 2k + 8k | 1 | 0.726 / 0.743 | 0.679 / 0.686 | 0.653 / 0.673 | - | 0.663 / 0.686 | 0.656 / 0.674 | 0.656 / 0.686 | 0.90 | - | 0.91 | 0.90 | 0.90 |
| 2k + 8k | 4 | 2.383 / 2.712 | 2.041 / 2.690 | 2.297 / 2.638 | - | 2.000 / 2.481 | 2.020 / 2.491 | 2.312 / 2.671 | 0.96 | - | 0.84 | 0.85 | 0.97 |
| 8k + 8k | 1 | 1.258 / 1.273 | 1.090 / 1.106 | 1.070 / 1.090 | - | 1.087 / 1.101 | 1.089 / 1.108 | 1.092 / 1.124 | 0.85 | - | 0.86 | 0.87 | 0.87 |
| 8k + 8k | 2 | 1.757 / 2.343 | 1.643 / 2.185 | 1.639 / 2.186 | - | 1.620 / 2.151 | 1.609 / 2.157 | 1.631 / 2.181 | 0.93 | - | 0.92 | 0.92 | 0.93 |
| 8k + 8k | 4 | 3.040 / 4.336 | 3.802 / 4.368 | 3.814 / 4.366 | - | 3.339 / 4.352 | 3.779 / 4.333 | 3.827 / 4.386 | 1.25 | - | 1.10 | 1.24 | 1.26 |
| 8k + 8k | 8 | 6.056 / 8.630 | 5.948 / 8.611 | 5.910 / 8.589 | - | 5.882 / 8.576 | 5.884 / 8.561 | 5.886 / 8.536 | 0.98 | - | 0.97 | 0.97 | 0.97 |
| 8k + 8k | 16 | 9.252 / 16.637 | 10.260 / 16.633 | 10.194 / 16.663 | - | 10.210 / 16.690 | 10.141 / 16.617 | 10.149 / 16.581 | 1.10 | - | 1.10 | 1.10 | 1.10 |
| 8k + 8k | 32 | 18.980 / 31.982 | 18.879 / 32.496 | 18.820 / 32.289 | - | 18.752 / 32.212 | 18.731 / 32.249 | 18.758 / 32.239 | 0.99 | - | 0.99 | 0.99 | 0.99 |
| 16k + 8k | 1 | 1.970 / 1.988 | 1.616 / 1.634 | 1.612 / 1.632 | - | 1.588 / 1.608 | 1.614 / 1.622 | 1.589 / 1.614 | 0.82 | - | 0.81 | 0.82 | 0.81 |
| 16k + 8k | 4 | 5.190 / 6.786 | 4.960 / 6.428 | 5.002 / 6.088 | - | 4.911 / 6.395 | 4.899 / 5.995 | 4.959 / 6.060 | 0.96 | - | 0.95 | 0.94 | 0.96 |

## TTFT, async scheduling off (`VLLM_ASYNC=0`), p50 / p90 (s)

aon 1c is the async control as above; `aon sync` is aon served the same way (this run's
store steps).

| tokens | c | aon 1c | aon sync | off | 1G | 2G | 4G | 1G / aon 1c | 2G / aon 1c | 2G / aon sync |
|---|---|---|---|---|---|---|---|---|---|---|
| 8k | 1 | 0.199 / 0.229 | 0.210 / 0.229 | 0.172 / 0.187 | 0.187 / 0.201 | 0.171 / 0.189 | 0.178 / 0.198 | 0.94 | 0.86 | 0.81 |
| 8k | 2 | 0.248 / 0.364 | 0.265 / 0.357 | 0.285 / 0.380 | 0.262 / 0.350 | 0.268 / 0.355 | 0.316 / 0.341 | 1.06 | 1.08 | 1.01 |
| 8k | 4 | 0.463 / 0.642 | 0.473 / 0.651 | 0.645 / 0.645 | 0.425 / 0.605 | 0.494 / 0.644 | 0.646 / 0.647 | 0.92 | 1.07 | 1.04 |
| 8k | 8 | 0.805 / 1.223 | 0.790 / 1.238 | 0.850 / 1.094 | 0.794 / 1.218 | 0.772 / 1.196 | 0.912 / 1.195 | 0.99 | 0.96 | 0.98 |
| 8k | 16 | 1.359 / 2.182 | 1.443 / 2.366 | 2.339 / 2.345 | 1.382 / 2.261 | 1.332 / 2.143 | 1.308 / 2.223 | 1.02 | 0.98 | 0.92 |
| 8k | 32 | 2.466 / 4.198 | 2.700 / 4.500 | 3.642 / 4.354 | 2.577 / 4.436 | 2.483 / 4.163 | 2.380 / 3.956 | 1.04 | 1.01 | 0.92 |
| 16k | 1 | 0.398 / 0.413 | 0.385 / 0.402 | 0.316 / 0.352 | - | 0.336 / 0.360 | - | - | 0.84 | 0.87 |
| 16k | 2 | 0.441 / 0.653 | 0.425 / 0.605 | 0.486 / 0.654 | - | 0.480 / 0.638 | - | - | 1.09 | 1.13 |
| 16k | 4 | 1.005 / 1.381 | 0.879 / 1.216 | 0.953 / 1.242 | - | 0.808 / 1.154 | - | - | 0.80 | 0.92 |
| 16k | 8 | 1.514 / 2.371 | 1.552 / 2.365 | 1.469 / 2.272 | - | 1.402 / 2.180 | - | - | 0.93 | 0.90 |
| 16k | 16 | 2.608 / 4.352 | 2.617 / 4.362 | 3.339 / 4.371 | - | 2.557 / 4.271 | - | - | 0.98 | 0.98 |
| 16k | 32 | 4.915 / 8.396 | 5.000 / 8.426 | 5.191 / 8.496 | - | 4.788 / 8.372 | - | - | 0.97 | 0.96 |

Without async scheduling and without the gate, vLLM already splits some bursts by
arrival (8k c=8 off: 0.85 s), but not reliably (8k c=16 off: 2.34 s).

## Inter-token latency (full hits)

Mean ITL is the median over requests of `(total - TTFT) / (out_tokens - 1)`, 128
output tokens; max gap is the p50 / p90 over requests of the largest gap between
streamed chunks after the first (`max_gap_s`, new in `perf_client.py`). aon is this
build's aon (async: 1e store steps; sync: 1e-sync store steps).

| scheduling | tokens | c | aon ITL ms | lw off | lw 1G | lw 2G | lw 4G | lw 8G | max gap aon | max gap off | max gap 1G | max gap 2G | max gap 4G | max gap 8G |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| async | 8k | 4 | 7.0 | 7.7 | 8.1 | 8.3 | 9.2 | 7.9 | 0.015 / 0.017 | 0.010 / 0.106 | 0.074 / 0.144 | 0.016 / 0.200 | 0.139 / 0.139 | 0.010 / 0.017 |
| async | 8k | 8 | 8.6 | 9.0 | 11.9 | 10.1 | 9.1 | 8.9 | 0.017 / 0.018 | 0.020 / 0.186 | 0.159 / 0.159 | 0.142 / 0.286 | 0.021 / 0.176 | 0.016 / 0.288 |
| async | 8k | 16 | 9.6 | 12.0 | 18.8 | 17.4 | 14.1 | 12.0 | 0.016 / 0.016 | 0.042 / 0.483 | 0.146 / 0.152 | 0.286 / 0.286 | 0.267 / 0.543 | 0.043 / 0.043 |
| async | 8k | 32 | 10.6 | 27.4 | 40.5 | 40.3 | 38.6 | 34.5 | 0.020 / 0.021 | 0.050 / 2.082 | 0.148 / 0.157 | 0.293 / 0.293 | 0.561 / 0.600 | 0.933 / 1.095 |
| async | 16k | 4 | 8.3 | 9.0 | - | 10.2 | 9.0 | 8.9 | 0.022 / 0.024 | 0.019 / 0.027 | - | 0.149 / 0.288 | 0.022 / 0.025 | 0.023 / 0.030 |
| async | 16k | 8 | 9.3 | 12.0 | - | 17.4 | 14.1 | 12.0 | 0.021 / 0.022 | 0.017 / 1.539 | - | 0.283 / 0.295 | 0.276 / 0.549 | 0.021 / 1.040 |
| async | 16k | 16 | 10.5 | 17.0 | - | 31.1 | 28.6 | 23.4 | 0.021 / 0.022 | 0.019 / 1.484 | - | 0.289 / 0.298 | 0.548 / 0.567 | 0.765 / 1.074 |
| async | 16k | 32 | 11.5 | 47.0 | - | 75.3 | 73.6 | 69.9 | 0.024 / 0.025 | 0.052 / 0.170 | - | 0.386 / 0.386 | 0.566 / 0.633 | 1.054 / 1.054 |
| sync | 8k | 4 | 7.8 | 8.5 | 10.0 | 9.3 | 8.1 | - | 0.015 / 0.016 | 0.017 / 0.017 | 0.152 / 0.156 | 0.154 / 0.299 | 0.009 / 0.309 | - |
| sync | 8k | 8 | 9.6 | 12.8 | 14.3 | 13.4 | 12.4 | - | 0.018 / 0.019 | 0.269 / 0.423 | 0.158 / 0.160 | 0.288 / 0.299 | 0.287 / 0.549 | - |
| sync | 8k | 16 | 11.4 | 12.9 | 21.6 | 20.5 | 20.4 | - | 0.019 / 0.019 | 0.016 / 0.982 | 0.156 / 0.165 | 0.275 / 0.307 | 0.533 / 0.533 | - |
| sync | 8k | 32 | 13.1 | 34.5 | 43.9 | 44.2 | 44.1 | - | 0.022 / 0.025 | 0.719 / 0.719 | 0.160 / 0.160 | 0.297 / 0.299 | 0.549 / 0.549 | - |
| sync | 16k | 8 | 11.0 | 19.3 | - | 20.8 | - | - | 0.025 / 0.032 | 0.806 / 0.899 | - | 0.283 / 0.298 | - | - |
| sync | 16k | 16 | 12.9 | 26.9 | - | 35.2 | - | - | 0.032 / 0.036 | 1.041 / 1.041 | - | 0.294 / 0.300 | - | - |
| sync | 16k | 32 | 14.4 | 75.6 | - | 78.7 | - | - | 0.027 / 0.042 | 3.333 / 3.333 | - | 0.373 / 0.373 | - | - |

What it costs:

- **The largest gap is one group's transfer**: about 0.15 s at 1 GiB, 0.29 s at 2 GiB,
  0.55 s at 4 GiB and 1.0 s at 8 GiB, for every request that is not in the last group.
  aon's largest gap is 15-40 ms.
- **Mean ITL** rises by 30-80% at c ≥ 16 (8k c=32 async: 27 ms off, 40 ms at 1-2 GiB;
  16k c=32: 47 / 75 ms). lw's decode is already 2.5-5x aon's at c=32 with the gate
  off, because every later arrival's layerwise prefill stalls the running
  decodes too; aon loads outside the forward pass and keeps ITL at 11-14 ms.
- Over 128 output tokens the stall is a small part of the total; for short outputs the
  first-token gain dominates.

## Notes

- One run per point; the day-2 repeats show about 10-30% spread at c = 2-4, which
  covers the odd 8k c=4 points (e.g. 4 GiB at 1.07, async).
- `1e2` and `1e-sync` re-stored the data (same prompts and salts as `1e`); the
  harness's async switch is `VLLM_ASYNC` (`perf.sh` / `perf_session.sh`).
- Fixing the async-scheduling lag would need the layerwise wait off the engine thread
  (a GPU-side wait on each layer's load event), not a different budget; out of scope
  here.
