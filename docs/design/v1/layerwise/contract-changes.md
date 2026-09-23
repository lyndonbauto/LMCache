# Changes to the layerwise contracts

`lmcache/v1/layerwise/contract.py` is frozen in the sense that two other
tracks build against it, not in the sense that it is right. When it changes,
the change lands here so that Track A and Track B can see what moved without
reading a diff, and so the reason survives longer than the commit message.

Entries are newest first. Each says what changed, what breaks, and why the
old shape was wrong -- the last of those being the part that stops the same
defect coming back.

---

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

Note the limitation in [system-design.md](system-design.md) section 10:
a layerwise fetch cannot be served for a model whose kernel groups disagree
on their plane size, because the write side then shards by byte count and no
stored record matches a slot. `ModelLayout.record_index_for` refuses for such
a model rather than naming a record that holds other layers' bytes.
