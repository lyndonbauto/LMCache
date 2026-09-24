# Track A <-> Track C: blockers, answers, and meeting agenda

Raised by Track A while scoping [A1](track-a-acceptance.md#a1-the-contract-is-implemented-and-passes-the-conformance-suite),
then updated with Track C's reply and the contract changes in
[contract-changes.md](contract-changes.md). Track C owns `contract.py` and
`tests/v1/layerwise/`, so contract items need a Track C decision rather than
a Track A workaround. See
[track-c-acceptance.md](track-c-acceptance.md#on-changing-the-contracts) for
how contract changes are made.

## Where things stand

| Item | Status |
|---|---|
| BLK1: which side expands slots | Partly answered; superseded by M1 |
| BLK2: `node_index` had no node list | Resolved: `LayerFetchPlan.node_names` |
| BLK3: slot numbering | Resolved: a slot's position in `plan.slots` is its index |
| Q1: what `SlotPlacement.offset` is relative to | Open: M2 |
| Q2: digest encoding | Resolved: slots carry `record_key`; native hashes it |
| Q3: how the planner learns `max_sinks` | Open |
| Q4: distinct error for an oversized plan | Open |
| S1: conformance-suite arrival driver | Open |
| S2: shape test changed by Track A | Awaiting Track C review |

## Already done on Track A's side

No action needed; listed so Track C knows these invariants hold.

- **Generation `0` is never allocated.** `PipelinedFetchSession` used to hand
  the first fetch of every process generation `0`, which `is_layer_ready`
  also treats as "skip the generation check". It now starts at `1` and skips
  `0` on wrap, matching `NO_GENERATION`. Pinned by
  `test_allocated_generations_are_never_the_reserved_value`.
- **Declined layers are visible from Python.** The connector exposes
  `pipelined_unservable_layers()`, so `poll_layer` can return `UNSERVABLE`.
- **The adapter is implemented except for native issue.**
  `AerospikeLayerArrivalSource` implements `poll_layer`, `finish_fetch`,
  `abandon_fetch`, the generation and layer checks, and native error
  translation. `begin_fetch` issues through an injected `PlanIssuer`. The
  production `NativePlanIssuer` raises `NotImplementedError` until M1 and M2
  are settled; that method is the only code that changes once they are.
  Tested in `tests/v1/layerwise/test_aerospike_layer_arrival_source.py`,
  against the current contract (`record_key`, `plane`, `piece`,
  `node_names`).

## Open for the meeting

### M1. Node ownership: bind per record, by taking the plan's slots

**Agreed problem.** The native session binds a whole chunk to one node
(`ChunkNodeBinding`, `chunk_to_node_`) and builds each node's command from
that map. Aerospike places each record by its own digest, and record keys
differ per piece (`...|s|4`), so one chunk's records usually sit on several
nodes. Today `pipelined_fetch_arguments` refuses such a request; without that
guard a slot would be sent to a node that does not hold its record. The fix
is on Track A's side, and the plan already carries a node per slot.

**Track A's proposal: have the native session accept the plan's slots
directly** instead of chunk placements it re-plans from. Each `SlotPlacement`
already has everything the wire needs:

```text
sink    = <digest(record_key)>@<offset>:<length>#<position in plan.slots>
node    = plan.node_names[slot.node_index]
readiness: layer L is complete when every slot with layer_id == L has landed
```

What this buys:

- **Per-record node binding for free**, because each slot names its own node.
- **One planner on the fetch path.** Today the session re-derives slots from
  `ChunkPlacement` with the C++ `SlotPlanner`, and the Python plan is only
  equal to what is executed because the parity test keeps the two planners in
  step (see M3).
- **`begin_fetch(plan)` is enough.** The contract's `LayerArrivalSource`
  receives only the plan, so a native call that also needs
  `Sequence[ChunkPlacement]` (a Track C type) cannot be reached through the
  contract at all. With slot-level issue, it doesn't need to be.

The C++ `SlotPlanner` keeps its role as the reference the parity test checks
the Python planner against; it just stops being on the fetch path.

**Question for Track C:** is `plan.slots` authoritative for what is fetched,
so that the transport may issue it as-is? If yes, Track A changes the session
and `native_fetch.py` shrinks to key hashing and node lookup.

### M2. Who owns L1 window allocation (was Q1)

`SlotPlacement.offset` is documented as "within the destination host
buffer". The native session checks sinks against `window_bytes` and treats
offsets as relative to the **leased RDMA window**. Track C's stub supplies a
per-object `dest_offset`, but nothing allocates windows yet.

Needed decisions:

1. Who leases the window for a fetch: the retrieve path (Track C) or the
   transport (Track A)?
2. Are plan offsets window-relative (Track A's preference, since that is what
   the wire carries) or buffer-absolute?
3. How the chosen window reaches `begin_fetch`. If the transport leases, it
   needs only the plan's total extent; if the retrieve path leases, the window
   has to travel with the plan.

### M3. The native side does not number slots by list order

`native_fetch.py` says `slot_record_keys` is passed "in plan order, so the
native side's slot numbering matches the plan's". The session does not use
that order: `digests_for_plan` looks keys up by `(chunk, layer, plane,
piece)`, and slot numbers come from `SlotPlanner::plan_request`. The two
numberings agree only because `test_slot_plan_parity.py` keeps the planners
identical. Layer readiness stays internally consistent either way, so this is
not a correctness bug today, but the docstring overstates the guarantee. M1
removes the gap; until then the docstring should point at the parity test.

### M4. Retrieve wiring and the pump disagree on who begins the fetch

Track C's plan for retrieve (their step 3) is: build the plan, call
`begin_pipelined_fetch`, and hand the resulting generation to the pump. But
`LayerArrivalPump.run(plan)` calls `source.begin_fetch(plan)` itself and takes
no generation. The storage-manager path also returns `0` for "unsupported"
and a boolean for readiness, which are the two shapes
[system-design.md](system-design.md#3-contract-1----layer-arrival) says the
contract replaced.

**Proposal:** retrieve builds the plan and calls
`LayerArrivalPump(source, sink).run(plan)` with an `AerospikeLayerArrivalSource`.
It catches `LayerwiseContractError` and falls back to a whole-object load. The
storage-manager `begin_pipelined_fetch` / `is_pipelined_layer_ready` pair then
either goes away or becomes an internal detail behind the source.

### Q3. How does the planner learn each node's `max_sinks`?

Unchanged. Track A splits commands to fit `max_sinks` anyway (A5), so a plan
never fails because of the cap. If C5 only means "don't exceed 65536 slots",
its wording should say so; otherwise the planner needs a way to read the cap,
which is known only after `kv-sink-register` on Track A's side.

### Q4. Should an oversized plan get its own error?

Unchanged. A6 rejects a plan larger than the device's receive queue with
`LayerwiseContractError`, the same exception as "backend cannot do pipelined
fetch", so a caller cannot tell "shrink the plan" from "fall back". Proposal:
a `PlanTooLargeError(LayerwiseContractError)` subclass.

### S1. The conformance suite is hard-wired to the scripted source

Unchanged. The pump tests drive `ScriptedLayerArrivalSource` with
`deliver_layer` / `decline_layer`, which a real source does not have.
Proposal: a source fixture parametrized over implementations, plus a
slot-level driver:

```python
class ArrivalDriver(Protocol):
    def land_slot(self, slot: SlotPlacement, generation: int) -> None: ...
    def decline_slot(self, slot: SlotPlacement, generation: int) -> None: ...
```

Track A provides a device-free driver that feeds encoded immediates and
declined replies into the real session, so the suite can pin A2 (a layer stays
`PENDING` until its last slot) and A3 (a late slot from an old generation is
not credited) for every implementation.

### S2. The shape test was changed by Track A; please review

`test_track_a_source_shape.py` built the source with no arguments and
expected `poll_layer` to be unimplemented. Now the source is built as
`AerospikeLayerArrivalSource(connector, issuer)`, and the `NotImplementedError`
check is on `begin_fetch`, the one step still blocked. The signature-equality
tests are unchanged.

## After the meeting

Track A, once M1 and M2 are decided:

1. Change `PipelinedFetchSession` to take the plan's slots (or, if M1 is
   rejected, bind nodes per slot within the current chunk-placement API).
2. Add `record_node(user_key)` next to `record_digest_hex`, from the C
   client's partition map.
3. Implement `NativePlanIssuer.issue` and replace the fake issuer in the
   adapter tests with the device-free S1 driver.

Track C, in parallel: the PR split (build and CI fixes first), then retrieve
wiring per M4. Both tracks then run C9 over Soft-RoCE.

Suggested PR order: Track C's build/CI fixes, then Track A's small native
changes (generation `0`, `pipelined_unservable_layers`) as their own PR, then
the adapter.
