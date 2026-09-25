# Changes to the layerwise contracts

`lmcache/v1/layerwise/contract.py` is frozen in the sense that two other
tracks build against it, not in the sense that it is right. When it changes,
the change lands here so that Track A and Track B can see what moved without
reading a diff, and so the reason survives longer than the commit message.

Entries are newest first. Each says what changed, what breaks, and why the
old shape was wrong -- the last of those being the part that stops the same
defect coming back.

---

## The conformance suite drives sources through an `ArrivalDriver`

**Who is affected:** Track A (registers its source); anyone using
`ScriptedLayerArrivalSource`.

**What changed.**

- New `ArrivalDriver` protocol in `fakes.py`: `land_slot(slot_index,
  generation)` and `decline_slot(slot_index, generation)`. Slots are named by
  their position in `plan.slots`, the number the wire carries. An arrival
  quoting a generation that is not active is dropped, not raised, as on the
  wire.
- `ScriptedLayerArrivalSource` counts slots: a layer is resident once all of
  its slots land and unservable once any is declined. It gains `land_slot` /
  `decline_slot`; `deliver_layer` / `decline_layer` still work, and
  `deliver_layer` no longer turns a declined layer resident.
  `ScriptedArrivalDriver(source)` is its driver.
- `tests/v1/layerwise/test_arrival_source_conformance.py` runs every test
  once per entry in `SOURCE_HARNESS_FACTORIES`. To add a source:

  ```python
  # tests/v1/layerwise/conftest.py
  def _aerospike_harness() -> SourceHarness:
      session = ...  # fabric-free native session; pytest.skip() if unbuilt
      return SourceHarness(AerospikeLayerArrivalSource(...), FabricFreeDriver(session))

  SOURCE_HARNESS_FACTORIES["aerospike"] = _aerospike_harness
  ```

**What breaks.** Nothing in the contract. A test that delivered a layer after
declining it and expected `RESIDENT` now sees `UNSERVABLE`.

**Why.** The pump tests could only drive the scripted source, by layer, with
methods a real source does not have, so they could not show that a real
source keeps a layer pending until its last slot, or ignores a late slot from
an abandoned fetch. Driving by slot index puts those rules in one suite that
every implementation must pass.

## `pipelined_fetch_arguments` is one entry per slot

**Who is affected:** Track A (the slot-level native call takes this shape).

**What changed.** `pipelined_fetch_arguments(plan)` no longer takes
placements. It returns `node_names` and one `(node_index, record_key,
dest_offset, length, layer_id)` per slot, in plan order; position is the slot
number. The previous three-list shape for `issue_pipelined_fetch_by_keys` is
now `chunk_fetch_arguments(plan, placements)` / `ChunkFetchArguments`, used
by the adapter until the slot-level call exists.

**What breaks.** Callers of `pipelined_fetch_arguments(plan, placements)`;
switch to `chunk_fetch_arguments` for the old shape.

**Why.** Option 1 from the Track A/C review: the plan is the only source of
truth, so the native side must not re-plan. The flat shape also carries each
slot's own node, which the chunk-level call cannot, because one chunk's
records hash to different partitions.

## Plans are held to the slot ceiling; transports can refuse a plan as too large

**Who is affected:** Track A (raises the new error); anyone building
`LayerFetchPlan` by hand.

**What changed.**

- `MAX_SLOTS_PER_REQUEST` (65536) moved from `planner.py` to `contract.py`,
  and `LayerFetchPlan` rejects more slots than that with `ValueError`.
  Previously only `FetchPlanner.plan` checked, so a plan built any other way
  could carry slot indices that wrap.
- New `PlanTooLargeError(LayerwiseContractError)`, raised by `begin_fetch`
  when a plan is valid but beyond a transport limit, e.g. more slots than the
  device can post receives for (Track A's A6):

  ```python
  try:
      pump.run(plan)
  except PlanTooLargeError:
      ...  # optional: split the request into smaller plans
  except LayerwiseContractError:
      ...  # fall back to a whole-object load
  ```

- C5 no longer asks the planner to respect `max_sinks`. That cap is per node
  and per command, known only to the transport, which already splits commands
  to fit it.

**What breaks.** Nothing that worked: a plan over the ceiling was already
unfetchable. Imports of `MAX_SLOTS_PER_REQUEST` from `lmcache.v1.layerwise`
are unchanged.

**Why.** A6 raised the same exception for "this plan is too big" as for
"this backend can't do pipelined fetch", so a caller could not tell "try a
smaller plan" from "fall back". Subclassing keeps every existing handler
correct while letting a caller that can split do so.

## Slots name records by Aerospike key, not digest

**Who is affected:** Track A.

**What changed.** `SlotPlacement.digest: bytes` is now
`SlotPlacement.record_key: str`, the user key the record was stored under:

```python
SlotPlacement(layer_id=0, chunk_id=3, node_index=1,
              record_key="model@00000000@0@9f2c...|s|4",
              plane=0, piece=0, offset=4096, length=2048)
```

The planner side follows: `SlotDigestSource.digest_for` is
`RecordKeySource.record_key_for` (returns `str`), `RecordKeyDigests(layout,
cap, cache_keys, digest_of)` is `RecordKeys(layout, cap, cache_keys)`, and
`FetchPlanner.plan(request, record_keys)`.

On the native side:

- `LMCacheAerospikeClient.record_digest_hex(user_key)` returns the client's
  own RIPEMD-160 digest of the key in the connector's namespace and set, as 40
  lowercase hex characters. It is bound in every build.
- `issue_pipelined_fetch_by_keys(placements, chunk_nodes, slot_record_keys)`
  (RDMA builds) takes plain tuples -- `(chunk_id, object_group_id,
  dest_offset)`, `(chunk_id, node_name)`, `(chunk_id, layer_id, plane, piece,
  record_key)` -- turns each key into a digest with `record_digest_hex`, and
  calls the existing `issue_pipelined_fetch`. `PipelinedFetchSession` and the
  info-call sink format are unchanged: the session still receives
  `SlotDigest.digest_hex`.
- `begin_pipelined_fetch` on the storage manager and L2 adapters now takes
  `(plan: LayerFetchPlan, placements: Sequence[ChunkPlacement])` instead of
  three untyped lists. `pipelined_fetch_arguments` in
  `lmcache/v1/layerwise/native_fetch.py` does the flattening, and refuses a
  chunk placed on two nodes, since `ChunkNodeBinding` cannot express it.

**What breaks.** Anything constructing `SlotPlacement(digest=...)`, reading
`slot.digest`, or calling `begin_pipelined_fetch` with three lists. The
`issue_pipelined_fetch` binding taking `PipelinedSlotDigest` still exists.

**Why.** Hashing a key into a digest is the Aerospike client's job, and
most OpenSSL builds disable RIPEMD-160, so Python had to be handed a hash
function to compute something the client already knows how to compute. Keys
are also what every other part of the system uses to name a record, which
makes a plan readable in a log. Keeping the digest behind a native call means
that when the transport stops using an info command, only the native side
changes. The session keeps taking digests so Track A's tested code is
untouched.

## Hybrid models are servable: records follow each kernel group's planes

**Who is affected:** Track A (the meta record gained a bin); nobody's
contract types changed.

**What changed.** The Aerospike writer now cuts each object group's payload
per kernel group, so every record stays inside one plane even when kernel
groups differ in plane size. `ModelLayout.record_index_for` numbers records
the same way and no longer refuses hybrid models; it refuses only an object
group whose payload size another group shares with a different layout. See
[system-design.md](system-design.md) section 10.

**What breaks.** Nothing for uniform models: their records, indices and meta
records are byte-for-byte what they were. A hybrid object's meta record gains
a `runs` bin; a reader built before this change fails loudly on such an
object (each record's size is checked), rather than misreading it.

**Why.** The writer used to be told one plane size per model. A hybrid model
has none, so it fell back to byte-count sharding and 12 of the 16 slots in the
`hybrid_kernel_groups_in_one_object_group` fixture case matched no stored
record -- and the other 4 matched by coincidence, which would have served part
of a layer and reported it ready. Nothing in production published the layout
at all, so in practice every model was byte-count sharded;
`register_kv_cache` now publishes it.

## `SlotPlacement` gained `plane` and `piece`

**Who is affected:** Track A.

**What changed.** `SlotPlacement` carries two new fields:

```python
plane: int   # which K/V plane of the layer, from zero
piece: int   # which record of that plane, from zero, in offset order
```

**What breaks.** Anything constructing a `SlotPlacement` positionally. The
field order is now `layer_id, chunk_id, node_index, digest, plane, piece,
offset, length`.

**Why.** A slot is exactly one stored record, and that record's identity is
`(chunk_id, layer_id, plane, piece)` -- the same four fields
`pipelined_fetch_session.cpp` already joins digests on. The contract
previously let a slot carry its *chunk's* digest, which is wrong once a
chunk holds more than one record.

That defect is invisible at runtime. The transport would ask for a record
that genuinely exists, write it to an address that is genuinely inside the
registered window, and report the slot landed. The model would then read
some other piece's bytes: right shape, right dtype, plausible values, no
error anywhere.

## `LayerFetchPlan` gained `node_names`

**Who is affected:** Track A.

**What changed.** `LayerFetchPlan` now takes a second constructor argument:

```python
LayerFetchPlan(slots, node_names)
```

`node_names` is the cluster nodes the fetch talks to, in the order
`SlotPlacement.node_index` numbers them. `LayerFetchPlan.node_name_for(slot)`
resolves a slot to its node.

**What breaks.** Every `LayerFetchPlan(...)` call site. The constructor
rejects an empty list, a repeated name, and a slot whose `node_index` falls
outside it.

**Why.** `node_index` was documented as an index into "the fetch's node
list", and no such list existed on any type. Every consumer would have had to
be handed the node ordering out of band, and any two that disagreed would
address a fetch to the wrong node -- which, again, fails by returning the
wrong bytes rather than by raising.

It lives on the plan rather than on the planner's `PlanRequest` so that the
transport does not have to depend on a Track C type to read its own input.

## Planning moved into `lmcache/v1/layerwise/planner.py`

**Who is affected:** nobody yet; this is new surface rather than a change.

`FetchPlanner` builds a `LayerFetchPlan` from a `ModelLayout` and a
`PlanRequest`. Digests are supplied through a `SlotDigestSource`, which the
planner calls as it cuts planes, because which pieces exist is the planner's
own output and so cannot be enumerated by the caller beforehand.

`ModelLayout.record_index_for` refuses to name a record the write side did
not align to layer boundaries, rather than naming one that holds other
layers' bytes; which cases that covers is described in
[system-design.md](system-design.md) section 10.
