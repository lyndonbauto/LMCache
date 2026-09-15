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
> `#step=6&arch=linear&layer=5` lands on the non-contiguity problem for a
> sliding-window layer.

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
- The pages are **byte-opaque**, so CacheGen and CacheBlend do not apply. That
  removes the CacheBlend interaction from scope for this family.
- vLLM forces a **unified block size of 544–944 tokens** (model-specific, read
  from its startup log), and the LMCache chunk size must be a multiple of it.
  Chunks are therefore much larger than the 256 typical elsewhere, which moves
  per-layer payload sizes and the slot budget accordingly.

**Aux groups** (`ObjectGroupInfo.aux`, `extra_object_group_tag > 0`) are
connector-private — notably the blend fused-aux pool. Out of scope; CacheBlend
has its own retrieve path and its own all-or-nothing scatter invariant.

**Compressed groups** have `tokens_per_block != slots_per_block`, so
`num_slots` is not the token count. Always take slot counts from
`get_slots_per_chunk_in_sw` / `calculate_slots`, never from tokens directly.

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
