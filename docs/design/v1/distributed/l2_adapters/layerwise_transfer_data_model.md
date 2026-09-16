# Layer-wise transfer data model

How a real KV payload maps onto per-layer RDMA delivery and per-layer
readiness. This is the missing half of the pipelining prototype:
[`aerospike_rdma.md`](aerospike_rdma.md) covers the *signaling* mechanism
(`RDMA_WRITE_WITH_IMM`, `FetchPlan`, `LayerReadiness`) and proves it works,
but it moves synthetic layers with fabricated offsets. This document defines
the mapping from actual LMCache layout to those slots.

> **Interactive walkthrough:**
> [`layerwise-transfer-data-model.html`](layerwise-transfer-data-model.html) steps
> through the same material in plain language, drawing the shape at each stage.
> Open it in a browser. Steps are deep-linkable, e.g.
> `#step=7&arch=linear&layer=5` lands on the non-contiguity problem for a
> sliding-window layer, and `#step=5` on the CacheBlend comparison.
> [`layerwise-transfer-data-model.check.js`](layerwise-transfer-data-model.check.js)
> asserts the claims the page makes; run it after editing the page.

**Scope.** This changes no keys, no object model, and no L2 interface. It is
derivation only: everything here is computed from structures LMCache already
builds. That is deliberate — the moment layout crosses the adapter boundary we
are back in AIE-89 territory, which was deferred for good reason.

## The payoff this serves

Aerospike pushes layer-major: every chunk of layer 0, then layer 1, keeping the
link saturated. The GPU consumes layer *i* while layer *i+1* is still in
flight, so transfer stops being a term you add to TTFT and becomes something
hidden behind compute. You pay for one layer's transfer instead of all of them.

Whether that works is one race per layer, and the ratio that decides it is
independent of prompt length, because tokens cancel from both sides:

```text
transfer per layer     KV bytes per token per layer       GPU FLOP/s
------------------  =  ----------------------------  ×  --------------
 compute per layer     FLOPs per token per layer        network bytes/s
```

Below 1, the GPU never waits after layer 0. Above 1, it starves and the ceiling
is the link. For Llama-3.1 shapes (8 KV heads, head dim 128, fp16 → 4 KiB per
token per layer) against the M0 measurements:

| | @ 12.2 GB/s (NIC ceiling) | @ 1.88 GB/s (single-object rate) |
|---|---|---|
| 8B on H100 | 0.31 | **1.98 — starves** |
| 8B on L40S | 0.08 | 0.55 |
| 70B on H100 | 0.08 | 0.51 |

Two caveats on those numbers. The favourable ratios are a gift from GQA; a
model with full multi-head attention carries 4× the KV per token, which pushes
8B-on-H100 to ~1.2 even at line rate. And the ratio must be evaluated at **p99,
not median** — see the tail argument under Open questions.

## Four coordinate systems

Pipelining has to reconcile four namespaces that do not line up:

| Namespace | Unit | Where it comes from |
|---|---|---|
| vLLM | `layer_name` → global layer index | `wait_for_layer_load(layer_name)` |
| Kernel dispatch | kernel group + position in `layer_indices` | `KVLayerGroupsManager.kernel_groups` |
| Storage | `ObjectKey(chunk_hash, …, object_group_id)` | one object per *(chunk, object group)* |
| RDMA | slot = one write's worth, named by immediate data | `FetchPlan` in `layer_pipeline.h` |

The awkward part is that **storage has no layer coordinate at all**.
`ObjectKey` carries `object_group_id` and nothing finer, so "layer 7 arrived"
is never something a key can express — it has to be derived from byte ranges
inside an object's payload.

## The authoritative layout

Do **not** re-derive strides from model config. The payload layout is already
defined by `MemoryLayoutDesc`, built per object group in
`lmcache_driven_transfer.build_memory_layout_desc`, which emits one shape and
dtype per kernel group in `kernel_group_indices` order. The per-kernel-group
shape comes from `get_kernel_group_shape_dtype`:

```319:321:lmcache/v1/platform/cpu/cache_context.py
        shape = torch.Size(
            (sd.kv_size, group.num_layers, num_slots, group.hidden_dim_size)
        )
```

with one variant for a specific engine format:

```214:218:lmcache/v1/platform/musa/cache_context.py
        if group.engine_kv_format == EngineKVFormat.NL_X_NB_BS_HS:
            return torch.Size((group.num_layers, num_slots, group.hidden_dim_size))
        return torch.Size(
            (sd.kv_size, group.num_layers, num_slots, group.hidden_dim_size)
        )
```

So an object group's payload is a concatenation of per-kernel-group tensors, in
`kernel_group_indices` order, and each kernel group is internally uniform —
every layer in it shares one `shape_desc` by construction. Per-layer offsets
are therefore exactly computable, and the hazard is not "strides are
unpredictable" but "using one group's stride for another group's layers".

## The consequence that shapes everything: a layer is not contiguous

In the standard layout `kv_size` is the **outermost** dimension, not the layer
dimension. Read that shape again: `(kv_size, num_layers, num_slots,
hidden_dim)`. K for all layers comes first, then V for all layers.

So layer *L* of a kernel group occupies **`kv_size` disjoint byte ranges**,
separated by `num_layers × num_slots × hidden_dim` elements — not one range.
Under `NL_X_NB_BS_HS` the layer dimension is outermost and a layer *is*
contiguous, but that format is the exception.

This matters because an RDMA write targets one contiguous remote address
range. The local side can gather from multiple buffers via several SGEs, but
the remote side cannot scatter. Therefore:

```text
slots(layer L, chunk c) = kv_size × ceil(plane_bytes(L) / max_write_bytes)
```

where `plane_bytes(L) = num_slots × hidden_dim × element_size` and
`max_write_bytes` is the smaller of EFA's `max_rdma_size` and the leased
window.

**A layer is ready only when all `kv_size` of its planes have landed, for
every participating chunk.** A design that assumed one slot per layer per
chunk would deliver K, report the layer ready, and hand the model a cache with
uninitialised V — correct-looking tensors full of garbage, no error anywhere.
This is the single most valuable thing found while writing this document.

## Mapping a model layer to slots

Given global layer index *L* from vLLM's `layer_name`:

1. Find the kernel group *k* containing *L*, and its position *p* within
   `k.layer_indices`. This is a static, one-time map built at registration.
2. Find the object group *g* whose `kernel_group_indices` contains *k*, and the
   byte offset of *k*'s tensor within *g*'s payload — the running sum of
   preceding kernel groups' sizes from `MemoryLayoutDesc`.
3. Within *k*'s tensor, for each `kv` plane in `range(kv_size)`, the plane's
   byte range for position *p* is at
   `((kv × num_layers) + p) × num_slots × hidden_dim × element_size`.
4. Split each plane by `max_write_bytes` into slots.
5. Repeat for every participating chunk *c*.

Steps 1–3 are pure arithmetic over existing structures. Step 5 is where the
cross-chunk barrier enters.

## Where the bytes live, and what has to change

An object does not sit on disk as one piece. `AerospikeNativeConnector::plan`
shards it purely by byte count — `nseg = ceil(payload / target)`,
`seg_b = ceil(payload / nseg)` — with no notion of layers. The useful finding is
that **it does not need one**.

Worked example: 32 layers, 8 KV heads, head dim 128, fp16, 256-token chunks.
A plane is 512 KiB, a layer 1 MiB, the object 32 MiB, and at a 1 MiB cap that is
32 records. The K plane is records 0–15 and the V plane 16–31, so:

| Layer | K bytes | V bytes | Records |
|---|---|---|---|
| 0 | 0 – 512 KiB | 16 – 16.5 MiB | 0, 16 |
| 1 | 512 KiB – 1 MiB | 16.5 – 17 MiB | 0, 16 (already read) |
| 2 | 1 – 1.5 MiB | 17 – 17.5 MiB | 1, 17 |

Two reads complete two whole layers. Steady state is one record read per layer,
and the reads are independent, so they still fan out across devices — which the
M0 sweep showed is where throughput comes from.

**So the storage does not get reorganised.** No layer in the key, no change to
the shard plan, no change to the record sizes that were tuned in M0. What
changes is the *order*: `do_single_get` loops `i = 0 … nseg-1`, and serving a
layer-major push means reading in schedule order (`0, 16, 1, 17, …`) with a
small cache so consecutive layers sharing a record do not re-read it.

Per-layer records were considered and rejected. They pin record size to the
plane size, and M0 measured a 60–100% latency swing across record sizes; they
multiply the primary index; and they push layout into the key, which is exactly
the coupling AIE-89 deferred.

### The alignment hazard

That clean mapping is luck. It holds because plane sizes and record caps are
both powers of two. Mamba/GDN hybrids use unified block sizes of 544, 784 and
944 tokens — none of them powers of two. At 784 tokens the plane is 1.53 MiB
against 1 MiB records, every layer straddles, and layer 0 needs records
`{0, 1, 49, 50}`: 4 MiB read to use 3.06 MiB, 31% amplification.

The fix is cheap: have LMCache pass a plane-size alignment hint into `plan()`
so `seg_b` lands on plane boundaries, instead of relying on the arithmetic
happening to work out. The connector currently takes an opaque payload and a
byte target, so this is a small additive parameter, not a contract change.

## Readiness is request-scoped, not fetch-scoped

vLLM computes layer *L* for the **whole sequence**, so it needs layer *L* of
every chunk. Chunks are distinct keys distributed across cluster nodes by
digest, so one layer's data arrives from several nodes, via several fetch
commands, in an order nobody controls.

`LayerReadiness` as prototyped is per-fetch. It needs to become
request-scoped:

```text
expected(L) = Σ over participating chunks c of slots(L, c)
ready(L)    = landed(L) == expected(L)
```

Two design decisions follow, and they are not optional.

**Slot indices must be assigned at request scope, not per fetch command.** All
nodes' notifications land on one queue pair and therefore one readiness table.
If each node's fetch numbered its slots from zero, node 2's slot 5 would be
indistinguishable from node 4's slot 5. The generation field then identifies
the *request*, not an individual fetch.

**"Participating chunks" is group-dependent.** A sliding-window group only
covers a window of chunks (`ObjectGroupInfo.sw_size_chunks`,
`get_slots_per_chunk_in_sw`), so requiring all chunks for such a group would
wait forever. `expected(L)` must be computed from the group's own window, not
from the request's chunk count.

### Immediate-data budget

The 32-bit immediate is `generation(16) | slot_index(16)`, capping a request at
65536 slots. With request-scoped numbering and the `kv_size` multiplier:

```text
slots ≈ chunks × layers × kv_size × pieces_per_plane
```

An 80-layer model over 100 chunks with `kv_size = 2` and one piece per plane is
16,000 — comfortable, but a 4× margin rather than a 4000× one. A long context
with small chunks and multi-piece planes could approach the limit, and
`FetchPlan::add_slot` throws `std::length_error` rather than silently wrapping.
If that ceiling is ever reached, the fix is a coarser readiness granularity,
not more bits.

## Groups needing different treatment

Treat these as the common case, not as edge handling.
[`hybrid_models.rst`](../../../../source/mp/hybrid_models.rst) validates nine
hybrid architectures by name — Gemma 3/4, gpt-oss, Qwen3.5/3.6/3.8,
Kimi-Linear, Kimi K3, DeepSeek-V4-Flash, GLM 5.1/5.2, MiniMax-M3 — against a
single catch-all row for everything uniform. There are two families, and they
differ in how much they disturb this design: sliding-window + full attention
(Gemma 3, gpt-oss), which is still paged KV throughout, and Mamba/GDN + full
attention (Qwen3.5+, Kimi), which is not.

**Recurrent / Mamba groups** (`KernelGroupInfo.recurrent_state`) hold state
snapshots rather than per-token attention KV. They do not carry the same
sequential per-layer dependency, and putting them behind a per-layer barrier is
probably meaningless. Recommend excluding them from the layer barrier and
treating them as all-or-nothing, but this needs confirming against how the
model actually consumes them. Three further constraints come with them:

- `--separate-object-groups` is **required**, so these models always present
  more than one object group. The flag is off by default, which means a
  sliding-window hybrid normally puts all its kernel groups in *one* object
  group — both shapes must work.
- The pages are **byte-opaque**, so CacheGen does not apply and the recurrent
  state itself cannot be blended. This does *not* put the family outside
  CacheBlend: `_classify_cb_read_groups` handles recurrent groups explicitly,
  giving them to the prefix leg only. See the CacheBlend section below.
- vLLM forces a **unified block size of 544–944 tokens** (model-specific, read
  from its startup log), and the LMCache chunk size must be a multiple of it.
  Chunks are therefore much larger than the 256 typical elsewhere, which moves
  per-layer payload sizes and the slot budget accordingly.

**Aux groups** (`ObjectGroupInfo.aux`, `extra_object_group_tag > 0`) are
connector-private — notably the blend fused-aux pool. The blend leg reads at
most one of them alongside the attention group.

**Compressed groups** have `tokens_per_block != slots_per_block`, so
`num_slots` is not the token count. Always take slot counts from
`get_slots_per_chunk_in_sw` / `calculate_slots`, never from tokens directly.

## CacheBlend

CacheBlend reuses chunks that are *not* a prefix of the prompt, rotating their
K vectors to new positions (re-RoPE) and scattering them into the request's
paged blocks. It splits cleanly across this design: **one half comes free and
the other does not**, and the split is worth being precise about, because it
determines how much blend-specific work the pipelining tickets carry.

### The half that comes free: L2 → L1

Blend prefetches through `storage_manager.submit_prefetch_task` and polls
`query_prefetch_status` — the *same* calls the dense path uses
(`blend/lookup.py:203`, `:371`). Nothing in the L2 adapter, the object model,
the record sharding, or the RDMA server-push is blend-aware, and none of it
needs to be. Blend inherits the bandwidth and the CPU offload unchanged.

It arguably benefits more than the dense path does. `CB_UNIFIED_LOOKUP` issues
**two** prefetches — a prefix one and a sparse one — and returns `None` to
defer until both have landed in L1, so a blend request waits on the slower of
two fetches rather than one. The sparse fetch asks for chunks scattered
arbitrarily across the cluster by digest, which is precisely the access pattern
the current connector handles worst: per chunk, a metadata GET followed by
serial segment GETs, so `K` scattered chunks cost `K × (1 + nseg)` round trips.
A server-side push collapses that to one info command plus the writes. **The
strongest single argument for the RDMA work is blend's sparse leg**, not the
dense prefix path that M0 already showed reaching 98% of line rate on TCP.

### The half that needs work: L1 → GPU

Blend reaches the GPU by its own route. `CB_RETRIEVE_PRE_COMPUTED` is a
blocking RPC running blend's fused re-RoPE-and-scatter kernel, dispatched per
kernel group with every layer at once — the spec passes
`num_layers = buf0.shape[1]`, the whole layer extent of the group
(`blend/retrieve.py:215`).

**This is not a blend-specific limitation, and it must not be written up as
one.** The dense path is in exactly the same position: it calls
`device_ops.multi_layer_block_kv_transfer` (`lmcache_driven_transfer.py:623`),
also per kernel group and also every layer at once, batched across chunks. And
`wait_for_layer_load` is a no-op stub for both
(`lmcache_mp_connector.py:786`). **Nothing in MP mode pipelines to the GPU
today.**

So per-layer GPU delivery is unbuilt work in both routes, and it is the same
shape of work in each: make a kernel accept a layer range instead of the whole
group, and make its caller incremental. The difference is only that there are
two such kernels, so blend is a *second instance* of the change rather than an
obstacle to it.

Blend is in fact the more natural fit conceptually. The original CacheBlend
algorithm is inherently layerwise — it inspects deviation at selected layers
(`blend_check_layers`) to choose which tokens to recompute — and the in-process
implementation requires `use_layerwise=True`
(`lmcache/v1/compute/blend/blender.py:48`). Layerwise blend is an existing
idea, just not in MP mode.

**Recommend sequencing, not exclusion:** build the per-layer path on the dense
kernel first, because it is the simpler kernel and the one M0 characterised,
then port the same change to blend's. Blend takes the L2→L1 win immediately
either way.

### Whether pipelining pays off for blend

Structurally it suits blend better than it suits the dense path, for the reason
above: `process_qkv(..., layer_id)` calls `get_kv(layer_id)` for one layer at a
time, because blend needs layer *i*'s cached KV before it can decide which
tokens to recompute at layer *i*. Per-layer delivery is what the algorithm
would ask for.

**What `r` means**, since the natural reading is backwards: `r` is the fraction
of tokens the GPU **computes from scratch** instead of taking from cache
(`blend_recompute_ratios`). It measures how much of the prompt blend *failed*
to reuse, so lower is better. Blend ranks the reused tokens by how much using
cached KV would distort the result and recomputes only the worst `r` of them.
`r = 0.15` is the published operating point; `r = 1.0` means nothing was reused
and the request is simply a full prefill. Re-RoPE is **not** recompute — that
is a rotation applied to the keys of the tokens being *kept*, and it is cheap.

Economically it pays off *less*, and the reason is worth stating plainly
because it is the opposite of intuitive: **pipelining hides transfer behind
compute, and deleting compute is the entire purpose of CacheBlend.** Blend
moves the same bytes as a dense fetch — it still needs the reused chunks' KV —
while recomputing only a fraction `r` of the tokens. So per layer:

```
ratio_blend = transfer / (r × compute) = ratio_dense / r
```

At the CacheBlend paper's `r ≈ 0.15` that is a **6.7× worse** transfer-to-compute
ratio. Using the walkthrough's default model (32 layers, 8 chunks of 256
tokens, 30 B parameters, 400 TFLOP/s, 12.2 GB/s), dense sits at ratio 0.27 —
transfer comfortably hidden — while blend lands at **1.79, i.e. network-bound**.
Pipelining still starts layer 0 sooner, but the ceiling becomes bandwidth
rather than compute, so the win is capped.

### But the ratio is not a score

The ratio answers *can the transfer be hidden*, which is a different question
from *is the cache worth fetching*, and the two come apart badly at high `r`.
At `r = 1.0` the ratio cheerfully reports "transfer hides" for a request that
fetched an entire cache and then recomputed everything anyway. Intuition says a
high recompute ratio is close to a cache miss and so the cache should stop
helping — and intuition is right. The ratio just does not measure it.

Comparing against **no cache at all** (full prefill, nothing fetched) is what
measures it:

| | End-to-end |
| --- | --- |
| No cache at all | `C` |
| Blend today, serial fetch then compute | `T + rC` |
| Blend pipelined | `≈ max(T, rC) + T/L` |

Serial blend stops beating no-cache when `T + rC > C`, i.e. above
`r* = 1 − T/C = 1 − ratio_dense`. On the walkthrough's defaults that is about
**73% recompute** — beyond which today's fetch-then-compute path is actively
worse than not caching, because the fetch is paid for whether or not the data
is then discarded.

**Pipelining nearly removes that cliff, and this is the argument for it in the
blend case.** Overlapped, the fetch hides inside compute that was happening
anyway — all of it except layer 0, which has no earlier compute to hide behind.
The worst-case penalty therefore shrinks from the whole transfer `T` to one
layer's transfer `T/L`, a factor of `L` (32× on the default model). It is not
break-even; `T/L` is a real floor.

**So the conclusion is two-sided, not one-sided.** Blend is transfer-heavy
relative to its compute by design, which is why it is the strongest case for
the RDMA round-trip work. But pipelining's value for blend is not mainly
speedup — it is **downside protection**: it is what makes it safe to leave the
cache enabled when reuse turns out poor. Those serve different risks and should
not be traded off against each other.

Two caveats on the reference lane. The comparison is against a **full prefill**
of every token, which is the cost of computing a layer from scratch — a pure
cache hit does far less work than that, so the reference is an *optimistic*
bound on how much compute is available to hide transfer behind, and it flatters
pipelining in both lanes equally. And `r = 1.0` making the two lanes coincide
is an arithmetic sanity check rather than an operating point: if you recompute
everything you would not fetch the KV at all, so the transfer would be pure
waste.

One caveat on the arithmetic: modelling blend's compute as `r ×` dense assumes
recompute cost scales linearly with the recomputed token count, which ignores
that those tokens still attend over the full sequence. It is a first-order
estimate and it is directionally safe — the true compute is somewhat higher
than `r ×`, so blend's real ratio is somewhat better than quoted. It does not
change the ordering.

### The two legs, and what they mean for readiness

`_classify_cb_read_groups` (`blend/read_set.py:39`) splits a registration's
object groups into two read sets:

| Leg | Reads | Why |
| --- | --- | --- |
| prefix | attention + recurrent | Contiguous history, so position-bound state is valid. |
| blend | attention + aux | Relocates chunks to new positions; a recurrent snapshot is the result of a scan ending at a fixed position and cannot be moved. |

Each leg keys, locks, and reads only its own set. The consequence for the
readiness model: **`expected(L)` must be summed over the leg's group set, not
over every group in the registration.** This is the same rule already stated
for sliding-window groups ("participating chunks is group-dependent"), extended
one level up — participating *groups* is leg-dependent. A readiness tracker
that counts slots across all groups will never complete the blend leg, because
the recurrent group's slots are never requested on it.

### The hard incompatibility

Blend requires **exactly one** object group labelled `"attention"`, and at most
one `"aux"`. The label comes from `kv_layer_groups.py:617`:

```python
"aux" if g.aux else ("recurrent" if g.recurrent else "attention")
```

A sliding-window group is neither aux nor recurrent, so it is labelled
`"attention"`. That yields:

| Architecture | `--separate-object-groups` | Groups | Blend |
| --- | --- | --- | --- |
| Uniform | off (default) | `["attention"]` | works |
| Sliding-window hybrid | off (default) | `["attention"]` | works |
| Sliding-window hybrid | **on** | `["attention", "attention"]` | **`RuntimeError`** |
| Mamba/GDN hybrid | on (required) | `["attention", "recurrent"]` | works |

The failure mode is a startup-time `RuntimeError`, not silent corruption, so it
is safe — but it is easy to hit by accident, since `--separate-object-groups`
looks like a pure performance knob. Note the asymmetry: the flag is *required*
for Mamba hybrids and that configuration is fine, while for sliding-window
hybrids the flag is optional and turning it on is what breaks blend.

## What must not be assumed

- **One global per-layer stride.** Valid within a kernel group, wrong across
  them. Hybrid models have several.
- **A layer is one contiguous range.** False whenever `kv_size > 1`, which is
  the common case.
- **Arrival order.** The *schedule* is layer-major, so the common claim
  "layer 30 can beat layer 2" is wrong — layer 30 has not been sent yet.
  What is true is narrower and still decisive: SRD reorders among the writes
  in flight together, so the slots of a layer land in no particular order and
  neighbouring layers overlap at the frontier. Count arrivals; do not infer
  completion from the last one. See
  [`aerospike_rdma.md`](aerospike_rdma.md#why-arrival-order-cannot-be-trusted).
- **All chunks participate in every group.** False for sliding-window groups.
- **Slot indices are unique per fetch.** They must be unique per *request*.
- **That readiness can be counted over every object group.** Under CacheBlend
  each leg reads a different subset, so `expected(L)` is per leg.

## Open questions

1. **Can one request's fetch reach line rate?** The M0 sweep saw 1.88 GB/s for
   a single object with 8 workers, against a 12.2 GB/s NIC ceiling measured
   with 60 threads and 256 outstanding. Pipelining's value depends on the
   former approaching the latter. Unresolved, and it gates everything.
2. **Tail amplification.** All-or-nothing waits once for the slowest transfer.
   Layer-major waits for the slowest transfer *of each layer*, L times in
   sequence, and a sum of L maxima is worse than one max. With p99 at 1.3–2×
   p50 in the sweep, the per-layer barrier trends toward the tail. The
   transfer-vs-compute ratio must be evaluated at p99, not median.
3. **Is the H2D copy per layer?** Landing in L1 is only half the journey; the
   destination is non-contiguous paged GPU blocks. A per-layer readiness signal
   is only useful if a per-layer H2D copy can be issued, which interacts with
   the existing batched transfer kernel.
4. **Recurrent group semantics**, per above.
5. **Is blend's sparse leg the better benchmark target?** The arithmetic above
   says yes — `K × (1 + nseg)` round trips for scattered chunks, against a
   dense prefix path that already reaches 98% of line rate on TCP. If it holds,
   the RDMA value story should be led by blend, not by bulk throughput. Needs
   a measurement that M0 did not take.

## Relationship to the tickets

- **AIE-91 / AIE-92** (partial completion, partial readability): this document
  supplies the model they were missing. Their current descriptions predate it.
- **AIE-94** (per-layer retrieve): the `expected(L)` aggregation above is the
  core of it.
- **AIE-95** (`wait_for_layer_load`): step 1 of the mapping is exactly the
  `layer_name` → kernel group resolution this needs.
- **AIE-89** (per-layer layout in the L2 contract): remains correctly
  deferred. Everything here is derived LMCache-side. What layout *does* affect
  is Aerospike-side record sharding — aligning record boundaries to layer
  boundaries, traded off against the ~8 MiB record-size crossover from M0.
