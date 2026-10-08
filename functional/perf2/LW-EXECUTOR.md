# Layerwise with vLLM's multiprocess executor, 8k full hits (2026-10-08)

Task A1 of the layerwise inter-token latency plan: does lw's delayed first token under
`--async-scheduling` go away when vLLM's forward pass leaves the engine thread
(`--distributed-executor-backend mp` at TP=1)?

Background (`LW-ADMISSION.md`): vLLM's engine core submits step N+1 before it emits
step N's tokens. With the default in-process executor (`UniProcExecutor`, "uni" below)
the submit runs N+1's forward pass on the engine thread, and a layerwise load blocks
that thread until N+1's data lands. So every admission group but the last gets its first
token one group's transfer late. With `mp`, the forward pass runs in a worker process;
the engine thread can emit N's tokens while N+1 loads.

Setup: droplet 129.212.190.233 (MI300X, same type as the 1c / 1e runs on
165.245.134.42), vLLM `0.27.1` (model runner V2), Llama-3.1-8B (128 KiB of KV per token:
an 8k request is 1 GiB), asd `3f3940e42` with `KV_SINK_STATS=1`, `rdma.queue_pairs` 16,
data on `/mnt/scratch`, LMCache `prototype-stage-1e` (`b4ad7386`) with the 1e admission
gate, harness `258ea003` (new `VLLM_EXECUTOR` switch and `aon8` step). One 8k store
(32 prompts) serves every session; c = 4, 8, 16, 32; n=4 requests at c=4 and n=c above;
128 output tokens. Every point: 0 errors, 100% external hits.

| label | executor | async scheduling | sessions |
|---|---|---|---|
| `a1u` | uni (vLLM default) | on (harness default) | aon (`store8`), lw at 1 and 2 GiB |
| `a1m` | `mp` | on | aon (`aon8`), lw at 1 and 2 GiB |
| `a1s` | uni | off (`VLLM_ASYNC=0`) | aon (`aon8`), lw at 1 and 2 GiB |

Chain: `/root/lmc-work/chain_a1.sh`, 23:09-23:49 UTC. Raw results in `lwaon3/a1u`,
`a1m`, `a1s`, untracked. p50 / p90 below are nearest-rank (index `int(q * n)` of the
sorted values), so at n=4 p50 is the third request.

## Result

The multiprocess executor removes the async-scheduling first-token lag and keeps async
scheduling's decode speed. With `mp`, lw's TTFT matches or beats aon at every c (0.90-1.01
x aon), and at 2 GiB c=32 it beats aon on both p50 and p90 (2.56 / 4.25 s vs 2.65 /
4.45 s). The price is 1-2 ms per token on lw's decode against uni; aon's decode does
not change. Decode pauses (one group's transfer) do not change with the executor.

- **The staircase is back.** 8k c=4, 1 GiB, TTFT per request (s):

  ```text
  uni, async on    0.39  0.56  0.70  0.71    each token after the next request's load
  mp,  async on    0.22  0.39  0.54  0.72    each token after its own load
  uni, async off   0.20  0.37  0.54  0.69
  aon, uni         0.23  0.41  0.57  0.71
  ```

  At c=8, 1 GiB: uni async 0.22 / 0.54 / 0.71 / 0.87 / 1.02 / 1.17 / 1.32 / 1.33 s;
  mp 0.22 / 0.38 / 0.54 / 0.70 / 0.85 / 1.00 / 1.15 / 1.30 s, the same as async off
  (0.24 / 0.40 / 0.55 / 0.72 / 0.87 / 1.02 / 1.17 / 1.33 s).
- **mp keeps async scheduling's decode speed; turning async off does not.** aon's mean
  time between tokens at c=32 is 9.3 ms (uni) and 9.2 ms (mp), but 12.7 ms with async
  off. So async off fixes TTFT at a cost of 2-4 ms per token for every request, and mp
  fixes it without that cost.
- **lw's decode pays 1-2 ms per token more under mp** (c=4-32: +1.1 / +1.2 / +1.1 /
  +1.2 ms at 1 GiB, +1.6 / +2.3 / +2.0 / +1.6 ms at 2 GiB). aon shows no such cost, so
  it is specific to layerwise steps. Not investigated.
- **Pauses are unchanged.** lw's largest gap between two tokens (p50) is one group's
  transfer in every mode: 0.15-0.18 s at 1 GiB, 0.29-0.32 s at 2 GiB (c ≥ 8; c=4 at
  2 GiB depends on how the 4 requests split into groups). aon's is 14-23 ms.
  The executor changes when the first token leaves, not how long decodes wait for other
  requests' loads (cost 2 in the plan).

## TTFT p50 / p90 (s)

Ratio = lw p50 / aon p50 with the same executor and scheduling.

| c | mode | aon | lw 1 GiB | ratio | lw 2 GiB | ratio |
|---|---|---|---|---|---|---|
| 4 | uni, async | 0.569 / 0.707 | 0.704 / 0.714 | 1.24 | 0.658 / 0.667 | 1.16 |
| 4 | mp, async | 0.563 / 0.697 | 0.541 / 0.716 | 0.96 | 0.508 / 0.663 | 0.90 |
| 4 | uni, sync | 0.554 / 0.707 | 0.536 / 0.694 | 0.97 | 0.697 / 0.699 | 1.26 |
| 8 | uni, async | 0.906 / 1.367 | 1.017 / 1.325 | 1.12 | 1.255 / 1.268 | 1.39 |
| 8 | mp, async | 0.910 / 1.390 | 0.854 / 1.303 | 0.94 | 0.857 / 1.308 | 0.94 |
| 8 | uni, sync | 0.850 / 1.312 | 0.874 / 1.326 | 1.03 | 0.842 / 1.278 | 0.99 |
| 16 | uni, async | 1.624 / 2.551 | 1.618 / 2.526 | 1.00 | 1.851 / 2.445 | 1.14 |
| 16 | mp, async | 1.508 / 2.394 | 1.436 / 2.323 | 0.95 | 1.376 / 2.222 | 0.91 |
| 16 | uni, sync | 1.465 / 2.407 | 1.465 / 2.379 | 1.00 | 1.415 / 2.271 | 0.97 |
| 32 | uni, async | 2.712 / 4.464 | 2.829 / 4.622 | 1.04 | 3.041 / 4.783 | 1.12 |
| 32 | mp, async | 2.654 / 4.446 | 2.685 / 4.483 | 1.01 | 2.559 / 4.248 | 0.97 |
| 32 | uni, sync | 2.768 / 4.548 | 2.745 / 4.624 | 0.99 | 2.559 / 4.264 | 0.92 |

- **c=4 at 2 GiB, uni sync (1.26)** is the 2-per-group staircase (0.39 / 0.39 / 0.70 /
  0.70 s): with n=4, p50 is the third request, the first of the second group. mp at the
  same point gave 0.36 / 0.36 / 0.51 / 0.66 s; one run each.
- **aon's uni control ran in the store session** (`store8`: store, then points), the mp
  and sync controls in a points-only session (`aon8`). aon uni vs mp differs by 0.01-0.12
  s p50, within the run-to-run spread seen before.

## Mean time between tokens (ms) and largest gap p50 (s)

Mean time between tokens: median over requests of `(total - TTFT) / (out_tokens - 1)`.

| c | mode | aon | lw 1 GiB | lw 2 GiB | gap aon | gap lw 1 GiB | gap lw 2 GiB |
|---|---|---|---|---|---|---|---|
| 4 | uni, async | 6.8 | 9.0 | 8.3 | 0.014 | 0.150 | 0.010 |
| 4 | mp, async | 6.6 | 10.1 | 9.9 | 0.015 | 0.176 | 0.155 |
| 4 | uni, sync | 7.5 | 10.2 | 9.7 | 0.015 | 0.169 | 0.306 |
| 8 | uni, async | 8.0 | 11.7 | 10.1 | 0.016 | 0.156 | 0.293 |
| 8 | mp, async | 8.0 | 12.9 | 12.4 | 0.019 | 0.160 | 0.314 |
| 8 | uni, sync | 9.3 | 14.1 | 13.5 | 0.023 | 0.159 | 0.294 |
| 16 | uni, async | 9.1 | 18.8 | 17.2 | 0.017 | 0.160 | 0.295 |
| 16 | mp, async | 8.6 | 19.9 | 19.2 | 0.015 | 0.164 | 0.304 |
| 16 | uni, sync | 11.2 | 21.6 | 20.7 | 0.019 | 0.163 | 0.289 |
| 32 | uni, async | 9.3 | 40.7 | 40.4 | 0.018 | 0.164 | 0.302 |
| 32 | mp, async | 9.2 | 41.9 | 42.0 | 0.021 | 0.160 | 0.319 |
| 32 | uni, sync | 12.7 | 44.4 | 44.1 | 0.020 | 0.162 | 0.299 |

uni async at c=4, 2 GiB has a 0.010 s gap p50 (p90 0.161 s): in that run three of the
four requests got their first token together at 0.66 s, after the last load, so they did
not wait for a later one.

## Limits

- One run per point; earlier repeats at c = 2-4 varied by 10-30%. The c=4 staircases are
  the clearest evidence, the p50 ratios at c ≥ 16 are within a few percent of each
  other.
- 8k full hits only; 16k and partial hits were not run with `mp`.
- TP=1, one MI300X, Soft-RoCE at about 7 GiB/s.
- Absolute numbers are not comparable with the 1c / 1e tables in `LW-ADMISSION.md`:
  this droplet's aon is slower (8k c=32 p50 2.71 s vs 2.47 s there, c=1 time between
  tokens 5.5 vs 5.6 ms but lw 8.3 vs 7.6 ms). Compare within this file.

## Next

- Use `--distributed-executor-backend mp` for lw runs from now on (harness:
  `VLLM_EXECUTOR=mp`), and as the lw baseline for task B (per-step CUDA graph mode).
- The +1-2 ms per token on lw's decode under mp is unexplained; check whether it is the
  per-layer hooks' cross-process cost once task B removes them from decode steps.
