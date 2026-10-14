# Proposal: mixed-load test for the per-request load mode

Status: proposal, not implemented. Follows `LW-ASYNC-FULL-HITS.md`, whose runs
covered full and partial L2 hits one class at a time, with c requests always in
flight and 128 output tokens.

## Goal

Compare three configurations on the same realistic traffic:

| Config | Server and connector | Why |
|---|---|---|
| **aon** | layerwise off | the reference: every load async |
| **lw R=0** | layerwise on, every request layerwise | the 1f behavior ("before") |
| **lw R=0.05** | layerwise on, per-request mode (1g default) | the new behavior |

The question: under traffic that mixes misses, L1 hits, L2 hits and long outputs,
arriving on their own schedule, is lw R=0.05 at least as good as aon for every
request class, and better than lw R=0 for decode smoothness?

## Request classes

Every class runs in the same trace. Counts are in chunks of 256 tokens. The mode
column is what R = 0.05 should pick (`new_chunks >= 0.05 * hit_chunks`).

| Class | Prompt | Hit comes from | Mode at R=0.05 |
|---|---|---|---|
| **Miss** | a new prompt, never sent before (unique id and salt), 2k-16k tokens | nothing | nothing to load |
| **L2 full** | a stored prompt sent unchanged, first use in the trace | L2 (Aerospike) | async |
| **L2 partial, long suffix** | stored prefix + 2k-8k new tokens | L2 | layerwise |
| **L2 partial, short suffix** | stored 16k prefix + 256-768 new tokens (1-3 chunks; new/hit 0.016-0.047) | L2 | async |
| **L2 partial, borderline** | stored 16k prefix + 1k new (new/hit 0.0625) | L2 | layerwise |
| **L1 full** | a prompt already used earlier in the same trace, sent again | L1 (CPU) | async |
| **L1 partial** | an earlier prompt in the trace + new tokens (a next turn) | L1 | by ratio |

Why each one:

- **Miss:** no load, only prefill. It checks that misses do not wait behind other
  requests' layerwise loads, and gives the prefill-only baseline.
- **L2 full / L2 partial, long suffix:** the cases already measured alone; here
  they compete with the rest.
- **Short suffix and borderline:** the risk the earlier runs did not cover: a
  large prefix loaded layerwise for a tiny prefill stalls everyone's decode. The
  pair sits on both sides of R = 0.05.
- **L1 full / L1 partial:** an L1 load is a CPU-to-GPU copy only, with no
  Aerospike read, so it should be much faster than an L2 load (not yet measured
  on the box). A layerwise L1 load then stalls the batch far less, so
  async may cost more than it saves here. This is the one class where R = 0.05
  might pick the wrong mode.

## Arrivals and output lengths

- **Open loop.** Requests are sent at precomputed times, whether or not earlier
  ones have finished, so queueing shows up as latency instead of being hidden by
  a fixed concurrency. Inter-arrival times are exponential (Poisson) with rate λ.
- **One seeded trace file per scenario,** replayed byte-for-byte in every
  config: the same prompts, classes, output lengths and send times. Differences
  between configs then come from the server only.
- **Output lengths** drawn per request: 50% 128 tokens, 30% 1k, 15% 4k, 5% 8k,
  with `ignore_eos` so every request produces exactly its length (otherwise
  configs that produce different text would decode different amounts).
- **Rates.** A calibration run finds aon's sustainable request rate for the
  scenario (the highest λ where its queue does not grow over 5 minutes). The
  test then runs at 40%, 70% and 90% of it.

## Scenarios

Class shares per scenario (percent of requests):

| Class | S1 chat | S2 RAG | S3 agent | S4 cold start |
|---|---|---|---|---|
| Miss | 15 | 10 | 10 | 60 |
| L2 full | 5 | 10 | 25 | 10 |
| L2 partial, long suffix | 15 | 10 | 10 | 20 |
| L2 partial, short suffix | 5 | 35 | 10 | 5 |
| L2 partial, borderline | 5 | 15 | 5 | 5 |
| L1 full | 15 | 5 | 30 | 0 |
| L1 partial | 40 | 15 | 10 | 0 |

- **S1 chat:** multi-turn conversations, most turns extend an earlier prompt
  still in L1.
- **S2 RAG:** a set of 16k documents with short questions appended: mostly
  short-suffix and borderline partial hits, the case most likely to stall decode.
- **S3 agent:** loops that resend the same context: many full hits, L1 and L2.
- **S4 cold start:** a fresh server under traffic: mostly misses and first-time
  L2 hits, no L1.

## Data setup

- **L2 set:** 16k and 8k "documents" stored before the trace (as today's
  `store` step), at least as many as the trace's first-use L2 requests, so every
  L2-class request uses a prompt no earlier request in the trace has touched.
- **L1 set:** the trace's first request for each L1-class prompt is an L2 hit
  or a miss; its later uses are L1 hits. The trace generator places the first
  use at least 30 s earlier.
- **L1 capacity.** The harness gives aon 180 GB of L1 and lw 140 GB plus the RDMA
  windows. On Llama-3.1-8B a 16k prompt is 2 GiB of KV, and every L2 load also
  lands in L1, where it can evict L1-class prompts (LRU). The generator keeps the
  KV volume between an L1 prompt's first use and its reuse under 100 GB, so both
  configs see the same L1 hits; the hit source is checked per run anyway (below).
- **No server restart during a trace** (today's `point` step restarts it, which
  empties L1). The server restarts once before each trace.
- **Misses** are stored when they complete, like any request; their prompts are
  unique, so they never become hits within the trace.

## Metrics

Per class and per config:

- **TTFT** p50, p90, p99.
- **Time between tokens**, from every inter-token gap (not one mean per
  request): p50, p99, p99.9.
- **Stalls:** gaps over 100 ms and over 500 ms, per minute of trace, and the
  share of all decoding requests affected.
- **Throughput:** output tokens per second over the trace; request rate actually
  served.
- **Goodput:** share of requests meeting both TTFT ≤ 2 s and p99 gap ≤ 100 ms.
- **Mode and hit source per request,** checked against the class: the load mode
  and `pipelined_outcome` from the server log, L1 and L2 hit tokens from the
  lookup metrics, joined on request id. A run whose sources differ from the plan
  by more than 5% is redone with a smaller L1 working set.

## Pass criteria for lw R=0.05

- **Against aon:** for every class, TTFT p50 and p99 no worse than aon beyond the
  run-to-run noise; p99 time between tokens within 10% of aon; stall rate no
  higher than aon's.
- **Against lw R=0:** p99 time between tokens and stall rate clearly lower at
  70% and 90% load; TTFT of layerwise classes unchanged.
- **Per-class mode** matches the table above in at least 95% of requests.
- **0 errors** in every run.

If L1 partial hits or short-suffix hits fail against aon, that is the evidence
for a second ratio for L1 hits, or for counting load bytes instead of chunks.

## Harness changes

1. **`make_trace.py`** (new): writes the trace for a scenario, seed and rate:
   one line per request with send time, class, prompt spec (id, prefix length,
   suffix length, salt), output length.
2. **`perf_client.py --trace <file>`:** open-loop replay (one task per request,
   started at its send time), `max_tokens` and `ignore_eos` per request, a
   request id per request (`X-Request-Id`), and every inter-token gap in the
   output (today only the longest one is kept).
3. **`perf_session.sh` step `trace file=<f>`:** replays a trace without
   restarting the LMCache server; the `store` steps before it fill L2.
4. **`report_mixed.py`** (new): per-class tables of the metrics above, and the
   mode and hit-source check from the server log and metrics.
5. **Chain script:** for each scenario: store the L2 set, calibrate on aon, then
   aon, lw R=0 and lw R=0.05 at three rates, twice each for noise.

## Cost

| Item | Time |
|---|---|
| Harness (items 1-4) and a dry run on the CPU VM | 1.5-2 days |
| Calibration, 4 scenarios | about 1 h on the box |
| Runs: 4 scenarios x 3 configs x 3 rates x 2 repeats x 10 min, plus restarts | about 8 h on the box |
| Report | 0.5 day |

Running S1 and S2 first (about 4 h) answers the main open questions: the L1
classes and the short-suffix risk.

## Open questions

1. **Does the pipelined fetch fill L1?** A deferred key lands in an RDMA window
   and goes straight to the GPU. If it is not also kept in L1, a reused prompt
   in lw is an L2 hit again, and lw and aon would see different hit sources for
   the same trace. Check before building the L1 classes.
2. **Output length mix and rates:** the proposed shares are guesses; replace
   them with production numbers if there are any.
3. **Preemption:** at 90% load with long outputs, vLLM may preempt requests. A
   preempted request is looked up again and may change mode; the report should
   count preemptions per config.
