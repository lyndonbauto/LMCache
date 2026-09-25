# Track C -- Planning and junction: goals and acceptance criteria

Read [exercise-goal.md](exercise-goal.md) for why, then
[system-design.md](system-design.md) for how the tracks fit together. This
document is what Track C is responsible for and how we will know it is done;
[track-c-status.md](track-c-status.md) is where each criterion stands and
what is blocked by whom.

## What this track is for

Two things:

1. **Planning.** Turn "this request needs these tokens' KV cache" into a
   `LayerFetchPlan` -- every byte range, attributed to a layer, a chunk and a
   cluster node.
2. **The junction.** Drive the transport into the loader, and own both
   contracts between them.

Track C is also the integrator. When A and B disagree about what the contract
means, Track C decides, and the contract changes by agreement rather than by
one side working around the other.

## Scope

You own:

- `csrc/storage_backends/aerospike/`: `slot_planner`, `shard_plan`,
  `layer_pipeline`, `memory_layout_conversion`
- All of `lmcache/v1/layerwise/`: `contract.py`, `pump.py`, `fakes.py`
- `tests/v1/layerwise/`, the conformance suite both other tracks must pass
- `docs/design/v1/layerwise/`

You do not own: the RDMA transport itself, or anything that touches a GPU.

## Acceptance criteria

### C1. Plans come from real requests

A real vLLM request produces a `LayerFetchPlan` whose slots cover exactly the
bytes needed -- no gaps, no overlaps, correct layer attribution, correct node
attribution. Today the planner lives in C++ and nothing in production builds a
plan at all; closing that is the bulk of the planning work.

### C2. A layer is split into disjoint ranges, not one

For a given layer, the plan yields `kv_size` disjoint ranges rather than a
single range. This is easy to get wrong in a way that still looks plausible --
the fetch succeeds and the data is wrong. There is already a planner test for
this; keep it and extend it to plans built from real requests.

### C3. Sliding-window requests are correct

Callers cannot be trusted to work out which chunks participate under a sliding
window, so the planner offers a helper and the plan is built through it. Test
window boundaries, including a window that starts mid-chunk.

### C4. Slot indices are request-scoped and unique

Slot indices are unique across the whole request, not per node. The immediate
encodes `(generation << 16) | slot`, giving 65536 slots and 16 bits of
generation. Test a multi-node plan and assert global uniqueness; per-node
numbering passes a single-node test and corrupts a multi-node fetch.

### C5. Plans stay inside the request's slot space

A request addresses at most 65536 slots (`MAX_SLOTS_PER_REQUEST`), because
the immediate has 16 bits of slot index. `LayerFetchPlan` rejects a larger
plan at construction with a clear error rather than truncating it, whether
the planner built it or a test did; a truncated or wrapped plan would credit
one slot's arrival to another.

The per-command sink cap is not the planner's concern. Each node advertises
`max_sinks` (default 256) in its `kv-sink-register` reply, which only the
transport sees, and the transport splits a node's sinks into commands that
fit (Track A's A5). A plan never fails because of it. A plan that fits the
slot space but exceeds what the device can accept in one fetch is refused by
the transport with `PlanTooLargeError` instead.

### C6. The pump is correct under out-of-order arrival

Layers are handed to the loader in ascending order regardless of the order
they land in. Already covered in `tests/v1/layerwise/`; keep it green as the
real implementations arrive. Deliver out of order in every pump test -- an
in-order test cannot tell a correct pump from one that ignores arrival
entirely.

### C7. Every failure path abandons both sides

Unservable layer, timeout, and a sink that raises mid-load all abandon both
the source and the sink before propagating. A caller that catches the error
must be able to fall back without knowing how far the fetch got. Already
covered; keep it green.

### C8. The conformance suite is the shared definition of correct

`tests/v1/layerwise/` grows as A and B find cases the contract did not pin. It
is written against the public surface only, and it stays that way -- it is the
example both other tracks copy.

`test_arrival_source_conformance.py` runs once per source registered in
`SOURCE_HARNESS_FACTORIES` (`tests/v1/layerwise/conftest.py`). Each entry
pairs a fresh source with an `ArrivalDriver` that lands and declines slots by
index, which is how the suite pins slot-level rules -- a layer stays
`PENDING` until its last slot lands, a late slot from an abandoned generation
is not credited -- for every implementation, not just the scripted one.

### C9. End-to-end integration

Track A's real source and Track B's real loader, driven by the pump, serve a
real request. This is the criterion that proves the split worked, and it is
yours because nobody else touches both halves.

### C10. No hardware anywhere in your test suite

Track C tests are pure CPU logic. If one needs a GPU or a fabric, something
has leaked across a contract boundary.

## On changing the contracts

`contract.py` is frozen in the sense that two other people are building
against it, not in the sense that it is perfect. It will be wrong somewhere.
When it is:

- Change it deliberately, in one commit, with both other tracks told.
- Update `tests/v1/layerwise/` in the same commit, so the new behaviour is
  pinned before anyone builds on it.
- Update [system-design.md](system-design.md) so the rationale does not rot.

The failure to avoid is a track quietly adapting to a contract defect on its
own side. That works until integration and then costs a week.

Two properties are worth defending hard, because both replace a defect that
already bit this codebase: arrival is three-valued, and generations are
explicit and non-zero. The reasoning is in
[system-design.md](system-design.md) sections 3 and 4.

## Done

C1--C8 and C10 are green, and C9 has run.
