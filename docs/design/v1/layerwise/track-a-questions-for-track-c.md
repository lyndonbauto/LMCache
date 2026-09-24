# Track A -> Track C: blockers and open questions

Raised by Track A while scoping [A1](track-a-acceptance.md#a1-the-contract-is-implemented-and-passes-the-conformance-suite).
Track C owns `contract.py` and `tests/v1/layerwise/`, so each item here needs a
Track C decision rather than a Track A workaround. See
[track-c-acceptance.md](track-c-acceptance.md#on-changing-the-contracts) for
how contract changes are made.

Each item says what is blocked, why, and what Track A would propose. The
proposals are starting points, not decisions.

## Already fixed on Track A's side

No action needed; listed so Track C knows the invariants now hold.

- **Generation `0` is never allocated.** `PipelinedFetchSession` used to hand
  the first fetch of every process generation `0`, which `is_layer_ready`
  also treats as "skip the generation check". It now starts at `1` and skips
  `0` on wrap, matching `NO_GENERATION`. Pinned by
  `test_allocated_generations_are_never_the_reserved_value`.
- **Declined layers are visible from Python.** The connector now exposes
  `pipelined_unservable_layers()`. Before, Python could only see ready or not
  ready, so `poll_layer` could never return `UNSERVABLE`.
- **The adapter is implemented except for plan translation.**
  `AerospikeLayerArrivalSource` implements `poll_layer`, `finish_fetch`,
  `abandon_fetch`, the generation and layer checks, and native error
  translation. `begin_fetch` issues through an injected `PlanIssuer`. The
  production `NativePlanIssuer` raises `NotImplementedError` until BLK1-BLK3
  are decided, and that method is the only code that changes once they are.
  Tested in `tests/v1/layerwise/test_aerospike_layer_arrival_source.py`.

## Blockers

These stop `AerospikeLayerArrivalSource.begin_fetch` from being written.

### BLK1. The plan and the native fetch are at different granularities

`LayerFetchPlan` hands the transport **fully expanded slots**. The native entry
point takes **chunk placements** and expands them itself:

```text
contract:  LayerFetchPlan(slots=(SlotPlacement(layer_id, chunk_id, node_index,
                                               digest, offset, length), ...))

native:    issue_pipelined_fetch(
               placements   = [ChunkPlacement(chunk_id, object_group_id, dest_offset)],
               chunk_nodes  = [ChunkNodeBinding(chunk_id, node_name)],
               slot_digests = [SlotDigest(chunk_id, layer_id, plane, piece, digest_hex)])
           -> SlotPlanner::plan_request expands chunks into slots
```

So there are two planners, and the adapter sits between them. It could reverse
the expansion to rebuild chunk placements, but that is fragile, and it can't
recover `object_group_id` or split a slot into `(plane, piece)`, because
`SlotPlacement` carries neither.

**Question:** which side is the source of truth for slot expansion?

- **Option 1 (Track A's preference):** the contract plan is authoritative. Track A
  adds a native entry point that accepts pre-expanded slots, and `SlotPlanner`
  becomes Track C's tool for *building* a `LayerFetchPlan` instead of something
  the transport calls again. There's one expansion, and it's in Track C's code,
  which is where C2 (disjoint K/V ranges) is tested.
- **Option 2:** the transport stays chunk-level. The contract plan carries
  chunk placements and object group ids, and the transport expands them. Then
  `SlotPlacement` becomes derived data, and the conformance suite has to build
  plans through the planner rather than directly.

### BLK2. `node_index` has nothing to index into

`SlotPlacement.node_index` is documented as "index into the fetch's node list",
but `LayerFetchPlan` has no node list. The transport addresses nodes by name,
and `kv-sink-register` state is keyed by that name.

**Proposal:** add `nodes: tuple[str, ...]` to `LayerFetchPlan`, and validate in
`__post_init__` that every `node_index` is in range. The alternative is to pass
the node list to the adapter separately, but then the same fetch can be
described two ways, and the plan no longer says where its data lives.

### BLK3. Slot numbering is not in the plan

The immediate is `(generation << 16) | slot`. C4 makes slot indices
request-scoped and unique, but `SlotPlacement` has no slot index, and the
contract says slot order is "not significant". Today the native planner
assigns the indices.

**Question:** under Option 1 of BLK1, is the slot index the position in
`plan.slots`, or an explicit field? Track A prefers an explicit
`slot_index: int` validated as unique and `< 65536` in `__post_init__`, so that
C4 is enforced by the type instead of by convention.

## Open questions

These don't block `begin_fetch`, but they change what it validates.

### Q1. What is `SlotPlacement.offset` relative to?

The contract says "within the destination host buffer". The native side
treats `dest_offset` as relative to the **leased RDMA window**, and it rejects
slots outside `window_bytes`. If the plan's offsets are buffer-absolute, the
adapter needs to know which window is leased and where it starts. Who chooses
the window, the planner or the transport?

### Q2. What digest encoding does the plan use?

`SlotPlacement.digest` is `bytes`. The wire uses hex (`digest_hex`). Track A
assumes raw 20-byte Aerospike digests, hex-encoded by the adapter. Please
confirm, and consider validating the length in `__post_init__`.

### Q3. How does the planner learn each node's `max_sinks`?

C5 says plans respect the per-command sink cap. Track A splits commands to fit
`max_sinks` anyway (A5), so a plan never *fails* because of the cap. But the cap
is only known after `kv-sink-register`, which runs on Track A's side at
connector startup. If C5 means the planner should be aware of the cap, it needs
a way to read it. If it only means "don't exceed 65536 slots", C5's wording
should say that.

### Q4. Who owns the device slot cap?

A6 rejects at `begin_fetch` any plan with more slots than the device can post
receives for (`max_qp_wr`). That raises `LayerwiseContractError`, the same
exception as "backend can't do pipelined fetch", so the caller can't tell "try
a smaller plan" apart from "fall back". Should there be a distinct
`PlanTooLargeError` subclass?

## Conformance suite

A1 requires `tests/v1/layerwise/` to pass against the Aerospike source. Track C
owns that directory, so these are requests.

### S1. The suite is hard-wired to the scripted source

The pump tests construct `ScriptedLayerArrivalSource` directly and drive it with
`deliver_layer` and `decline_layer`. A real source has no such methods. Arrivals
come from immediates and declines from node replies.

**Proposal:** a source fixture parametrized over implementations, plus a small
driver protocol the suite uses to cause arrivals:

```python
class ArrivalDriver(Protocol):
    def land_slot(self, slot: SlotPlacement, generation: int) -> None: ...
    def decline_slot(self, slot: SlotPlacement, generation: int) -> None: ...
```

The scripted source gets a trivial driver. Track A provides a device-free
driver that feeds encoded immediates and declined replies into the real
session, so the suite runs without a fabric (A9, C10). Driving at slot rather
than layer granularity also lets the suite pin A2 (a layer stays `PENDING`
until its *last* slot lands) and A3 (a late slot from an old generation is not
credited to the new one) for every implementation, not just Track A's.

### S2. The shape test pins the skeleton

**Changed by Track A; please review.** The test asserted that `poll_layer`
raises `NotImplementedError` and built the source with no arguments. Neither
holds now that everything except the plan translation is implemented. The
source is built as `AerospikeLayerArrivalSource(connector, issuer)`, and the
`NotImplementedError` check moved to `begin_fetch`, which is the one step still
blocked. The signature-equality tests are unchanged.

## What Track A needs back

1. A decision on **BLK1**. BLK2 and BLK3 mostly follow from it.
2. Answers to **Q1** and **Q2**, which decide what `begin_fetch` validates.
3. Agreement on the **S1** driver shape, or an alternative, so that Track A can
   write the device-free driver in parallel.
