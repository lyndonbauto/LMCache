# Changes to the layerwise contracts

`lmcache/v1/layerwise/contract.py` is frozen in the sense that two other
tracks build against it, not in the sense that it is right. When it changes,
the change lands here so that Track A and Track B can see what moved without
reading a diff, and so the reason survives longer than the commit message.

Entries are newest first. Each says what changed, what breaks, and why the
old shape was wrong -- the last of those being the part that stops the same
defect coming back.

---

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
