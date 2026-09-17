# Workload hit-rate regime (AIE-101)

Measures the thing that decides whether layer-wise pipelining can pay at all:
**how much of a prompt still needs prefill after a cache lookup.**

This runs entirely offline. No cluster, no EFA, no GPU, no model download. It
exists so the AIE-101 gate can be answered while hardware is still being
procured, instead of blocking on it.

## Why this is the gate

Pipelining can only hide transfer behind prefill that is *still required*.
Working through the timing model, the saving per request is

```
saving = min(C_rem, T * (1 - 1/L))
```

where `T` is the transfer time for the cached tokens, `C_rem` is the prefill
still needed for the uncached tokens, and `L` is the layer count (one layer's
transfer can never be hidden). Two consequences drive everything:

- On a **complete hit**, `C_rem = 0` and the saving is **zero**, no matter how
  fast the fabric is. There is nothing to overlap with.
- On a **complete miss**, `T = 0` and the saving is **zero** again. Nothing was
  fetched.

So the value lives entirely in the partial-hit middle, and its size is set by
the *distribution* of the uncached fraction across real traffic — not by the
aggregate hit rate, which averages the two useless ends together.

## How it works

Three stages, two of which are upstream LMCache code:

```
gen_traces.py          →  lmcache tool cache-simulator hash-trace  →  payoff.py
(synthetic workloads)     (LMCache's real rolling chunk hashing)      (ms saving)
                                        ↓
                          lmcache tool cache-simulator simulate
                          (histograms + PNG, if you want the charts)
```

The middle stage calls LMCache's own `TokenHasher`, which matters more than it
looks. LMCache's chunk hashes are **rolling** — chunk *N*'s hash depends on
chunks 0..*N*-1 — so a hit requires an exact *prefix* match. A content-only
reimplementation would report far higher hit rates than reality. It also
discards **trailing partial chunks**, so the tail of every prompt is
permanently prefill. Both behaviours are reproduced rather than approximated,
and both are covered by tests in `tests/tools/test_trace_hasher.py` upstream.

## Running it

```bash
# 1. Generate a workload (token IDs, so no tokenizer needed)
./gen_traces.py chat --requests 500 --turns 8 --message-tokens 256 -o traces/chat.jsonl

# 2. Hash it the way LMCache would
python -m lmcache.tools.cache_simulator.trace_hasher \
    -i traces/chat.jsonl -o lookup/chat.jsonl \
    --chunk-size 256 --layers 32 --kv-heads 8 --head-dim 128 --dtype bfloat16

# 3. Price it
PYTHONPATH=/path/to/LMCache ./payoff.py -i lookup/chat.jsonl --label chat \
    --capacity-gib 64 --link-gbps 12.2 --gpu-tflops 400 --active-params-b 8 --layers 32
```

Set `--active-params-b` to **active** parameters, not total. Production serving
is mostly MoE at 3-6% sparsity, so the two differ by 20x or more, and prefill
tracks the active count.

Set `--link-gbps` deliberately. The M0 sweep measured a single object at
1.88 GB/s against a 12.2 GB/s NIC ceiling, so line rate is not a safe default.

## What it says so far

Synthetic traces only — parameters below were chosen by us, so treat these as
*the tooling working and showing plausible magnitudes*, not as the answer. The
real numbers need a production trace, which is what AIE-101 still has to
source.

Llama-3-8B geometry (32 layers, 8 KV heads, 128 head dim, bf16 → 128 KiB of KV
per token), 12.2 GB/s, 400 TFLOP/s, 8B active, 64 GiB cache:

| workload | hit rate | T (ms) | C_rem (ms) | saving (ms) | saving % |
|---|---|---|---|---|---|
| chat (8 turns, 256-token messages) | 84.5% | 15.0 | 10.3 | 9.3 | 36.8% |
| rag (20 docs, 4 K-token docs) | 94.2% | 47.3 | 10.8 | 2.7 | 4.6% |
| blend (scattered fragments) | 21.3% | 10.5 | 145.0 | 10.2 | 6.6% |
| cold (no reuse) | 0% | 0.0 | 163.8 | 0.0 | 0.0% |

Three things are worth pulling out.

**Multi-turn chat lands in the pipelining sweet spot, contrary to our starting
assumption.** We expected chat to trend toward the complete-hit regime where
pipelining wins least; the design doc simply left the distribution unmeasured.
It does not trend that way. Each turn appends
roughly one chunk of new tokens while the cached prefix grows, so the uncached
fraction settles around 10-30% — precisely the band where the saving peaks. In
the run above, 75% of chat requests landed in that band. Sweeping message size
64→1024 tokens and turn count 8→24, the saving stayed within **21-39%** of the
cache-hit path, so this is structural rather than an artefact of one parameter
choice.

**RAG is the weak case, and for a non-obvious reason.** Its hit rate is
*higher* than chat's (94% vs 85%) yet its saving is 8x smaller. A large cached
document and a tiny question make it transfer-bound with almost no prefill left
to hide behind. Higher hit rate, less pipelining value — which is exactly why
the aggregate hit rate is the wrong metric for this decision.

**The synthetic `blend` workload shows why CacheBlend needs its own matcher.**
Reusing the same fragments in a different order yields only a 21% hit rate,
because rolling prefix hashing invalidates everything after the first
reordered fragment. The prefix path cannot see scattered reuse at all. This is
a property of the hashing, not a flaw in the workload, and it is the reason
CacheBlend carries a separate non-contiguous `BlendTokenRangeMatcher` rather
than relying on prefix lookup.

## Caveats that bound the answer

- `C_rem` uses the two-FLOPs-per-parameter-per-token approximation and ignores
  attention's quadratic term, so it understates prefill at long context. That
  understates available overlap, and therefore **understates** the saving.
- Transfer is modelled at one sustained rate, ignoring the per-object ramp the
  M0 sweep observed.
- The per-request saving is computed per request. The printed means are means
  over requests and the saving is not recoverable from them, since `min()` does
  not commute with averaging.
- Synthetic reuse is exact or absent. Real traffic has near-misses — prompts
  sharing a long prefix that diverges mid-chunk — which rolling hashing treats
  as total misses from the divergence onward. Expect real hit rates below these.
