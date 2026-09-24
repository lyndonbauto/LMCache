# Track A <-> Track C: blockers, decisions, and meeting agenda

Raised by Track A while scoping [A1](track-a-acceptance.md#a1-the-contract-is-implemented-and-passes-the-conformance-suite),
then updated with Track C's replies and the contract changes in
[contract-changes.md](contract-changes.md). Track C owns `contract.py` and
`tests/v1/layerwise/`. See
[track-c-acceptance.md](track-c-acceptance.md#on-changing-the-contracts) for
how contract changes are made.

## Where things stand

| Item | Status | Owner of follow-up |
|---|---|---|
| BLK1 / M1: who expands slots | **Decided: Option 1** (the plan is the only source of truth) | Track A: done (see "Work that follows") |
| BLK2: node list | Done: `LayerFetchPlan.node_names` | - |
| BLK3: slot numbering | Done: position in `plan.slots`; plan rejects > 65536 slots | - |
| Q1 / M2: offsets and window ownership | **Open for the meeting** (see M2) | Both |
| Q2: digest encoding | Done: slots carry `record_key`; native hashes it | - |
| Q3: planner and `max_sinks` | **Decided:** the planner does not need it | Track C: reword C5 |
| Q4: oversized-plan error | **Decided:** `PlanTooLargeError(LayerwiseContractError)` | Track C: contract; Track A: native side done |
| S1: conformance driver | **Decided:** `ArrivalDriver` as proposed | Track C: parametrize suite; Track A: fabric-free driver |
| S2: shape test | **Decided:** Track A updates it with the implementation | Done |
| M3: native numbering docstring | Resolved by Option 1 | - |
| M4: retrieve wiring vs the pump | Open (see M4) | Track C |

## Decisions

### Option 1: the native session takes the plan's slots as given

The Python plan is the only source of truth for what is fetched. The native
side no longer re-plans from chunk placements.

`pipelined_fetch_arguments` flattens the plan into the node names plus one
entry per slot, in plan order:

```text
(node_index, record_key, dest_offset, length, layer_id)
```

The native session's job becomes:

1. hash each record key with `record_digest_hex`;
2. group slots by `node_names[node_index]`, which fixes node ownership, since
   each record's own partition decides its node;
3. build `kv-sink-fetch-pipelined` commands using each slot's position as its
   slot number, split to each node's `max_sinks`;
4. count readiness per layer from the slots' `layer_id`.

It keeps every check it does today: the device notification cap (A6), sinks
inside the leased window, declined-slot handling (A4), and stale-generation
rejection (A3). `SlotPlanner` and the chunk-placement entry point stay until
the old path is removed.

### Q3: `max_sinks` belongs to the transport

The planner caps a request at 65536 slots. The per-command sink cap is the
transport's concern, and Track A already splits commands to fit it (A5).
Track C rewords C5 to say this.

### Q4: `PlanTooLargeError`

Track C adds `PlanTooLargeError(LayerwiseContractError)` to `contract.py`.
Because it subclasses the existing error, current handlers still catch it.
Track A raises it from `begin_fetch` when a plan exceeds the device's
notification cap. For now the retrieve path treats it like any other contract
error and falls back to a whole-object load.

Track A has done both: the native side now throws a distinct
`PlanTooLargeError`, and the adapter checks `len(plan.slots)` against
`pipelined_max_slots_per_request()` before issuing.

### S1: conformance driver

Track C parametrizes `tests/v1/layerwise/` over source implementations.
Track A writes a fabric-free `ArrivalDriver` for the Aerospike source, which
feeds encoded immediates and declined replies into the real session.

## Open for the meeting

### M2. Window ownership: bounded windows or the whole L1 region?

**Track C's proposal:** offsets are relative to the start of the leased RDMA
window, and Track A owns the lease. The window would be the registered L1
region, so a destination offset is the L1 allocation's address minus the
region base. That fits Track C's `ChunkPlacer`: the production placer turns L1
allocations into offsets.

**Agreed:** offsets are window-relative, and Track A owns the lease.

**The conflict:** making the window the whole L1 region reverses a decision
recorded in
[aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#registration-scope-bounded-windows).
A slab-wide rkey was rejected there on purpose, because it lets any node write
anywhere in the KV cache:

- A late write from an **abandoned** fetch still lands. The generation stops
  LMCache from *counting* it, but the NIC performs the write anyway.
- Once the abandoned fetch's L1 objects are freed and reallocated, that write
  overwrites **another request's KV**. Nothing raises, and the model produces
  confident wrong tokens.
- Bounded windows keep the damage inside the abandoned request's own buffer,
  which is the only reason the failure stays attributable.

So the real question is where a pipelined retrieve's L1 objects are allocated:

| Option | What it means | Cost |
|---|---|---|
| **A. Allocate inside a leased window** (Track A's preference) | For a pipelined retrieve, Track A leases a window and hands out destination offsets inside it. The production `ChunkPlacer` asks the transport, not the general L1 allocator. | L1 objects for pipelined retrieves live in the window pool. `window_count` caps concurrent pipelined fetches, and `window_bytes` caps the size of one. |
| B. Whole L1 region as one window | The production `ChunkPlacer` turns ordinary L1 allocations into offsets from the region base. | Reverses the bounded-window decision. A late write from an abandoned fetch can corrupt an unrelated request, so abandon would have to re-register the memory region (expensive) or quarantine the freed objects until writes are known to have stopped. |

Needed from the meeting:

1. Option A or B.
2. Under A: how a window-resident object becomes an ordinary L1 object once
   the fetch finishes. Either copy it out, or have the window pool be part of
   L1's allocator so the object can stay where it is.
3. Who writes the production `ChunkPlacer`. Under A it wraps a Track A lease
   API, so Track A owns the offsets and Track C calls them.

### M4. Retrieve wiring and the pump disagree on who begins the fetch

Track C's retrieve plan builds the plan, calls `begin_pipelined_fetch`, and
hands the resulting generation to the pump. But `LayerArrivalPump.run(plan)`
calls `source.begin_fetch(plan)` itself and takes no generation. The
storage-manager path also returns `0` for "unsupported" and a boolean for
readiness, which are the two shapes
[system-design.md](system-design.md#3-contract-1----layer-arrival) says the
contract replaced.

**Proposal:** retrieve builds the plan and calls
`LayerArrivalPump(source, sink).run(plan)` with an `AerospikeLayerArrivalSource`.
It catches `LayerwiseContractError`, including `PlanTooLargeError`, and falls
back to a whole-object load. The storage-manager `begin_pipelined_fetch` /
`is_pipelined_layer_ready` pair then goes away, or becomes internal to the
source.

## Work that follows

**Track A, unblocked by Option 1:**

1. **Done.** Native slot-level issue:
   `PipelinedFetchSession::begin_request_from_slots` takes
   `PlannedSlot{node_name, digest_hex, layer_id, offset, length}` in plan
   order. It is exposed as `issue_pipelined_fetch_by_slots(node_names, slots)`
   on the pybind client, where each slot is
   `(node_index, record_key, dest_offset, length, layer_id)`. The
   logic-harness tests cover one chunk whose records sit on two nodes,
   per-layer readiness across nodes, declines, `max_sinks` splitting, stale
   generations, input rejection, and an unreachable node.
2. **Done, but not built yet.** `record_node(user_key)` returns the master
   node from the C client's partition map (`as_partition_info_init` plus
   `as_partition_get_node`, client 7.3.0), next to `record_digest_hex`.
3. **Done.** `NativePlanIssuer.issue` flattens the plan and calls (1).
   Track C's `pipelined_fetch_arguments` doesn't need the new shape for this
   path: the issuer flattens the plan itself.
4. **Done on the native side.** The session throws `rdma::PlanTooLargeError`,
   which pybind raises as `PipelinedPlanTooLargeError(RuntimeError)`.
   `NativePlanIssuer` also checks `len(plan.slots)` against
   `pipelined_max_slots_per_request()` before sending anything. Both raise
   `LayerwiseContractError` today and will switch to `PlanTooLargeError` once
   it is in `contract.py`.
5. **Waiting.** The fabric-free `ArrivalDriver` (S1) needs Track C's
   protocol in the suite. It also needs a way to drive the real session from
   Python without a device, which the pybind client doesn't offer today.

Items 1 to 3 compile and pass only in the device-free logic harness. The
connector, driver and pybind changes need a build with `libaerospike` and
verbs, which Track A's workstation doesn't have.

**Track A, after M2:** the window lease API, and a production `ChunkPlacer`
on top of it if Option A is chosen.

**Track C:** `PlanTooLargeError`, the C5 rewording, the parametrized suite,
`pipelined_fetch_arguments` in the new flattened shape, the PR split, and
retrieve wiring per M4.

**Together:** C9 over Soft-RoCE.

**Suggested PR order:** Track C's build and CI fixes, then Track A's small
native fixes (generation `0`, `pipelined_unservable_layers`), then the adapter,
then native slot-level issue.
