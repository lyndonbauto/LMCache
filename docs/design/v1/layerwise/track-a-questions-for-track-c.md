# Track A <-> Track C: blockers, decisions, and open points

Raised by Track A while scoping [A1](track-a-acceptance.md#a1-the-contract-is-implemented-and-passes-the-conformance-suite),
then updated with Track C's replies, the 2026-09-25 meeting, and the contract
changes in [contract-changes.md](contract-changes.md). Track C owns
`contract.py` and `tests/v1/layerwise/`. See
[track-c-acceptance.md](track-c-acceptance.md#on-changing-the-contracts) for
how contract changes are made.

## Where things stand

| Item | Status | Owner of follow-up |
|---|---|---|
| BLK1 / M1: who expands slots | **Decided: Option 1** (the plan is the only source of truth) | Track A: done |
| BLK2: node list | Done: `LayerFetchPlan.node_names` | - |
| BLK3: slot numbering | Done: position in `plan.slots`; `LayerFetchPlan` rejects > 65536 slots | - |
| Q1 / M2: offsets and window ownership | **Decided 2026-09-25: bounded windows, data stays in place** | Track A: reservation, lease and destination placer done; publishing every window next |
| Q2: digest encoding | Done: slots carry `record_key`; native hashes it | - |
| Q3: planner and `max_sinks` | Done: C5 reworded | - |
| Q4: oversized-plan error | Done: in `contract.py`, raised by the adapter and the native session | - |
| S1: conformance driver | Done: the Aerospike source passes the suite over the real native session | - |
| S2: shape test | Done | - |
| M3: native numbering docstring | Resolved by Option 1 | - |
| M4: retrieve wiring vs the pump | **Decided 2026-09-25: the pump begins the fetch** | Track C: retrieve wiring; Track A: retire the storage-manager pair |
| W1: windows run out when data stays in place | **Decided:** `lease()` reclaims a whole idle window | Track A: done (`RdmaWindowLeaser`, `reclaim_rdma_window`) |
| W2: which error the placer raises | **Decided** as proposed | Track A: placer; Track C: one handler in retrieve |
| W3: one fetch at a time | Known limit; a concurrent retrieve falls back | Track A, later |
| W4: fallback after a failed fetch | **Decided:** reload into fresh general-L1 objects | Track A: `abort_write` and pool choice done; Track C: retrieve wiring |
| N1: an object's records are not on one node | **Open, raised by Track A** | Track C: node per record in the planner; Track A: destination placer |
| P1: publishing every window | **Decided (Track A): one registration covering all windows** | Track A |

## Decisions

### Option 1: the native session takes the plan's slots as given

The Python plan is the only source of truth for what is fetched. The native
side no longer re-plans from chunk placements.

`pipelined_fetch_arguments(plan)` and `NativePlanIssuer` both flatten the plan
into the node names plus one entry per slot, in plan order:

```text
(node_index, record_key, dest_offset, length, layer_id)
```

The native session's job:

1. hash each record key with `record_digest_hex`;
2. group slots by `node_names[node_index]`, which fixes node ownership, since
   each record's own partition decides its node;
3. build `kv-sink-fetch-pipelined` commands using each slot's position as its
   slot number, split to each node's `max_sinks`;
4. count readiness per layer from the slots' `layer_id`.

It keeps every check it did before: the device notification cap (A6), sinks
inside the window, declined-slot handling (A4), and stale-generation
rejection (A3). `SlotPlanner`, the chunk-placement entry point and
`chunk_fetch_arguments` stay until the old path is removed.

### Q3: `max_sinks` belongs to the transport

The plan is capped at 65536 slots. The per-command sink cap is the
transport's concern, and Track A already splits commands to fit it (A5).

### Q4: `PlanTooLargeError`

`PlanTooLargeError(LayerwiseContractError)` is in `contract.py`. Because it
subclasses the existing error, current handlers still catch it. Two places
raise it, and both reach the caller as `PlanTooLargeError`:

- `NativePlanIssuer` checks `len(plan.slots)` against
  `pipelined_max_slots_per_request()` before sending anything.
- The native session throws `rdma::PlanTooLargeError`. pybind binds it as
  `PipelinedPlanTooLargeError`, whose Python base is the contract's
  `PlanTooLargeError`, so the adapter passes it through without special
  handling.

### S1: conformance driver

`ArrivalDriver` in `fakes.py` has `land_slot(slot_index, generation)` and
`decline_slot(slot_index, generation)`. `test_arrival_source_conformance.py`
runs once per entry in `SOURCE_HARNESS_FACTORIES`.

The `aerospike` entry (`tests/v1/layerwise/aerospike_harness.py`) runs
`AerospikeLayerArrivalSource` and `NativePlanIssuer` over the real native
`PipelinedFetchSession`, with no fabric, device, or cluster. The connector is
a test-only pybind module,
`tests/v1/distributed/rdma/csrc/fabric_free_session_pybind.cpp`, built by
`make -C tests/v1/distributed/rdma pyharness`. It also acts as the driver:

| Driver call | What it feeds the session |
|---|---|
| `land_slot(slot, generation)` | an encoded immediate, as `RdmaContext::poll_notifications` would |
| `decline_slot(slot, generation)` | the reply of the command carrying `slot`, with `failed=<slot>` |

The factory skips when `make`, a C++ compiler, or pybind11 is missing. All 15
conformance tests pass for it on the Soft-RoCE VM.

### M2: bounded windows, data stays in place

Recorded in
[system-design.md, "Window ownership"](system-design.md#window-ownership-decided-2026-09-25-m2).
In short:

1. **Window ranges are reserved** in the L1 allocator, for pipelined retrieves
   only.
2. **Data stays where it lands.** A pipelined retrieve's L1 objects are
   allocated inside its leased window and are not copied out.
3. **A window released by an abandon is quarantined** until the fetch timeout
   has passed, because the NIC still performs late writes the generation
   check refuses to count.
4. **Offsets are window-relative.** Track A builds the lease API
   (`lease(bytes) -> (window_id, base)`, `release(window_id, abandoned)`) and
   the production `ChunkPlacer`.
5. **One request, one window**, with `window_bytes` sized at init from the KV
   layout. The 8 MiB default can't hold one 256-token chunk of a 7B model
   (Llama-3-8B needs 32 MiB, Llama-2-7B 128 MiB). A request larger than its
   window falls back to a whole-object load. Spanning windows is deferred.

**The allocator change is Track A's.** It's a small PR reviewed by the
`l1_manager` maintainers. When RDMA is enabled, the general allocator covers
the slab minus the window range, and the windows get their own small
allocator.

**Where the code stands.**

- The windows are reserved: when an adapter enables RDMA, the general
  allocator never hands out memory in `[0, window_count * window_bytes)`, and
  each window has its own allocator. See
  [aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#the-windows-are-reserved-outside-the-general-allocator).
  This is pending review by the `l1_manager` maintainers.
- `RdmaWindowLeaser` hands out windows with reclaim and quarantine (done
  item 9). Nothing calls it yet; the production `ChunkPlacer` will.
- Only window 0 is published to the nodes.

### M4: the pump begins the fetch

Retrieve runs `LayerArrivalPump(source, sink).run(plan)` over an
`AerospikeLayerArrivalSource`, and falls back to a whole-object load on
`LayerwiseContractError`. The storage-manager `begin_pipelined_fetch` /
`is_pipelined_layer_ready` pair is retired or made internal to the source.

The production `ChunkPlacer` runs *before* the pump, so retrieve catches a
placer refusal and falls back there too. Track C builds that into the
retrieve wiring.

## Window lifecycle (W1 to W4, agreed after the meeting)

Track C recorded W1 to W4 in commit `4c634404`, in the "Window ownership" part
of [system-design.md](system-design.md#window-ownership-decided-2026-09-25-m2)
and in [contract-changes.md](contract-changes.md).

### W1. Windows run out when data stays in place

**Decided:** Track C proposed this and Track A added the five conditions
below.

**Track C's proposal:** a window is free again only once every object in it
has been evicted. Cache entries live a long time, so after `window_count`
retrieves every window could be full and the pipelined path would stop being
used. When `lease()` finds no free window, it may evict an idle window's
objects to reclaim it. Those objects were just read from Aerospike, so they
are unmodified and can be read again from L2. Only objects currently being
read hold a window.

**The five conditions:**

1. **Reclaim a whole window or nothing.** Evicting half a window's objects
   throws away cache entries and frees nothing. The check "no object in this
   window is locked" and the delete must happen as one step under the L1
   lock. `L1Manager.delete` refuses locked keys one at a time, after
   deleting earlier ones, so it isn't enough. The same allocator PR adds a
   narrow all-or-nothing delete, so the `l1_manager` maintainers review one
   change.
2. **The victim is the least recently used window with no pinned object.**
   An object is pinned by any of:
   - a read lock (a load in progress);
   - a read reservation (a lookup waiting for its retrieve);
   - a write lock (a fetch in flight).

   If every window is pinned, `lease()` refuses at once instead of waiting,
   and retrieve falls back.
3. **Eviction goes through L1's normal delete path**, so the usual cache
   events reach the key directory and the coordinator. Otherwise they keep
   listing keys L1 no longer holds.
4. **This is the window pool's only eviction.** The windows sit outside the
   general allocator, so L1's memory-pressure eviction never looks at them.
   Without reclaim-on-lease, window objects leave only on explicit deletes.
5. **Only a cleanly finished window can be reclaimed straight away.** If any
   slot neither landed nor was declined, a write may still be on the wire, so
   the release counts as an abandon and goes through quarantine.
   `release(window_id, abandoned)` states this rule; the caller doesn't
   decide it. The pump finishes a fetch only after every layer is resident,
   so a pump-finished fetch is clean by construction, and every other exit
   abandons. A fetch with a declined slot is therefore quarantined too.
   That's more cautious than needed, since a declined slot is never written,
   but it's safe and keeps the rule to one line.

**Cost:** a delete changes metadata only, with no I/O, so reclaiming inside
`lease()` adds no network time to retrieve. Evicted entries become L2 hits
again. If Aerospike has since expired them, it's an ordinary miss and the KV
is recomputed.

**Fallback:** if reclaim evicts too often in practice, copy objects out to
general L1 after the pump finishes. That's off the TTFT path, since every
layer has reached the GPU by then. It's not needed unless measurements say
so.

### W2. Which error the placer raises

**Decided** as below. The `PlanTooLargeError` docstring now says the placer
can raise it too.

With the placer running before the pump, the two refusals mean different
things to the caller:

| Refusal | Error | Why |
|---|---|---|
| Request larger than any window | `PlanTooLargeError` | The caller can split it into smaller requests. |
| No window free (all pinned or quarantined) | `LayerwiseContractError` | Splitting doesn't help; fall back. |

Retrieve then needs one handler around both the placer and the pump.

### W3. One pipelined fetch at a time

A `window_count` above 1 lets that many retrieves' *data* stay resident, but
fetches do not run concurrently. The session holds one active request, and
all fetches share one receive queue. Concurrency needs:

- one session per leased window;
- routing each immediate to its session by generation, which then has to be
  unique across sessions.

Until that exists, `lease()` also refuses while another fetch is in flight,
and the second retrieve falls back to a whole-object load. **Under load the
pipelined path serves only a fraction of retrieves.** Any early benchmark has
to report the share of retrieves that went pipelined, or its TTFT numbers
will understate the pipelined path and mix in fallback latency.

### W4. Fallback after a failed fetch goes into fresh objects

**Decided (Track C, 4c634404).** The meeting first said the fallback reloads
whole objects "into the same L1 objects". That conflicts with W1.5: those
objects sit in the failed fetch's window, which is quarantined because RDMA
writes may still land there. A fallback written into them could be
overwritten after it finished, with no error. Instead:

1. The failed fetch's window objects are deleted.
2. The window is released as abandoned, which quarantines it.
3. The whole-object load goes into **fresh objects in general L1**.

What this requires of Track A's code:

- **An abort-write call in `l1_manager`.** Step 1 deletes objects the failed
  fetch still holds *write-locked*, and neither existing call does that
  cleanly:
  - `finish_write` followed by `delete` briefly makes the half-written object
    readable, and emits a write-finished event for data that never finished.
  - `delete(force=True)` drops locks it doesn't own, including other readers'.

  The allocator PR adds a narrow "abandon these write reservations" call. It
  removes only objects write-locked by this retrieve, emits no write-finished
  event, and returns their memory to the window allocator, which doesn't
  reuse it until the quarantine ends.
- **The caller chooses the pool.** The allocator API takes the pool
  explicitly: the window of a lease, or general L1. The fallback's
  `reserve_write` must land in general L1 even though the same keys were
  just in a window. General L1 is the default, so existing callers don't
  change.
- **Order matters.** Abort the window objects before reserving the fresh
  ones, because L1 holds one object per key. In between, a lookup sees a
  miss, which is correct.

## Raised while building the placer

### N1. An object's records are not on one node (open, for Track C)

`ChunkPlacer.locate(chunk_id, object_group_id, object_bytes)` returns one
node per object, and `ChunkPlacement.node_index` documents why: "A chunk's
object is stored whole on one node, so every slot cut from it is fetched from
there." The write side doesn't store it that way:

- `RecordKeys.record_key_for` names a sharded object's records
  `"{cache_key}|s|{index}"`, and Aerospike places every record by the digest
  of its own key. One object's records are therefore spread over the nodes.
- The native side already works per record: `issue_pipelined_fetch_by_slots`
  takes a node per slot, and `record_node(user_key)` looks up the node that
  masters one record key.

On the single-node Soft-RoCE setup every record is on the same node, so
nothing fails. On a real cluster, most sinks would go to a node that doesn't
hold the record and would come back declined, so the layers would report
`UNSERVABLE`.

**Proposal: split the placer's two jobs.**

| Job | Granularity | Owner | Source |
|---|---|---|---|
| Destination offset | per object | Track A | `RdmaWindowPlacer`: lease a window, reserve each object in it |
| Node | per record | Track C (planner) | a record-to-node lookup, `record_node(record_key)` in production |

For the planner that means:

- `ChunkLocation` and `ChunkPlacement` lose their node field.
- `FetchPlanner` asks the lookup for each slot's record key and sets
  `SlotPlacement.node_index` per slot. It builds `node_names` from the
  answers in order of first appearance.

`locate` also has no `ObjectKey`, but the destination placer needs one to
reserve the object in L1. So the destination half is per request: it is
built from the request's keys. See `RdmaWindowPlacer` in
[aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#placing-a-retrieves-objects).

### P1. Every window is published as one registration (decided)

The server branch (`feat/kv-sink-fetch-pipelined`) decided it:

- Each `kv-sink-register` creates a server queue pair and holds one
  `(rkey, addr, size)`.
- A server allows 16 regions in total, across all clients (`MAX_REGIONS`).

One registration per window would use half a node's regions for one LMCache
instance with 8 windows, so LMCache instead registers the whole window range
once per node.

- Plan offsets become slab offsets, which equal
  `memory_obj.meta.address` for window objects. That changes
  `system-design.md`'s "offsets are relative to the leased window".
- The client still refuses a slot outside the leased window before sending.
- A write can reach another window only through a server bug, never general
  L1. Late writes from an abandoned fetch still land in their own
  quarantined window.

A per-window server check (`kv-sink-add-window` plus `window=<i>` on fetch)
can follow as hardening. Until the client change lands, only window 0 is
reachable, so run with `rdma.window_count = 1`.

## Work that follows

**Track A, done:**

1. Native slot-level issue: `PipelinedFetchSession::begin_request_from_slots`
   takes `PlannedSlot{node_name, digest_hex, layer_id, offset, length}` in
   plan order. It is exposed as `issue_pipelined_fetch_by_slots(node_names,
   slots)` on the pybind client. Logic-harness tests cover:
   - one chunk whose records sit on two nodes;
   - per-layer readiness across nodes;
   - declines;
   - `max_sinks` splitting;
   - stale generations;
   - input rejection;
   - an unreachable node.
2. `record_node(user_key)`: the master node from the C client's partition map
   (`as_partition_info_init` plus `as_partition_get_node`, client 7.3.0).
3. `NativePlanIssuer.issue` flattens the plan and calls (1).
4. `PlanTooLargeError` from both the pre-send slot-count check and the native
   session (Q4).
5. Rebased onto Track C's `4c634404`.
6. The fabric-free conformance harness (S1).
7. The allocator reservation, pending `l1_manager` review:
   - the window range is carved out of the general allocator, one
     `RangeMemoryAllocator` per window, with slab-absolute addresses;
   - `reserve_write(..., pool=L1Pool.rdma_window(i))` (general L1 is the
     default, and a window pool requires `mode="new"`);
   - `L1Manager.delete_if_none_locked(keys)` (W1.1);
   - `L1Manager.abort_write(keys)` (W4). It frees memory straight back to
     the window allocator. Holding the window back until quarantine ends
     is the lease's job, since only the lease hands a window out again;
   - window objects are never picked by memory-pressure eviction (W1.4).
8. `window_bytes` is checked against the KV layout. It can't be sized from
   the layout: the windows are reserved when L1 is built, and the layout only
   arrives later, when a worker registers its KV cache. So the size stays in
   config. When the layout arrives, the native driver checks that one chunk
   (one object per group, each rounded to the L1 alignment) fits in a window.
   If it doesn't, the pipelined path reports not-ready with the size needed,
   the adapter logs it, and retrieves fall back to whole-object loads.
   Sizing examples are in
   [aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#sizing-window_bytes).
9. The window lease: `RdmaWindowLeaser.lease(request_bytes) -> WindowLease`
   and `release(lease, FetchOutcome.FINISHED | ABANDONED)`. See
   [aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#leasing-a-window).
   - Reclaim (W1): an empty window first, then the one released longest ago
     with no locked object. The reclaim is L1's new
     `reclaim_rdma_window(i)`, which uses L1's own record of each window's
     keys.
   - Quarantine: an abandoned window waits `fetch_timeout_seconds`.
   - One lease at a time (W3). Refusals follow W2.

   Two differences from the M2 sketch. `release` takes an outcome enum, not
   a boolean. `lease` returns the window's slab offset, and
   `lease.pool()` gives the pool for `reserve_write`.
10. The destination half of the placer: `RdmaWindowPlacer.place(objects,
    layouts) -> WindowPlacement`, with `dest_offset`, `complete` and
    `abandon`. See
    [aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#placing-a-retrieves-objects).
    - It raises per W2.
    - `abandon` performs W4's first two steps: abort the writes, then
      quarantine the window.
    - The node half waits on N1.

These were verified on the Soft-RoCE VM
([rdma_testing_on_windows.md](../distributed/l2_adapters/rdma_testing_on_windows.md)):

- `lmcache_aerospike` builds with `BUILD_WITH_AEROSPIKE_RDMA=1` against
  C client 7.3.0;
- device-free logic harness: 298 checks pass;
- fabric harness over `rxe0`: 341 checks pass;
- 322 Python tests pass: `tests/v1/layerwise/` (including the conformance
  suite over both sources), pipelined readiness through the real extension,
  RDMA registration, and adapter configuration;
- the pytest wrappers in `tests/v1/distributed/rdma/` pass.

`record_node` and `issue_pipelined_fetch_by_slots` have not run end to end.
That needs an Aerospike server, and for the slot path, one built from the
`kv-sink` branch (A8).

**Track A, next:**

1. Get the allocator reservation (done item 7) through `l1_manager` review.
2. ~~Size `window_bytes` at init from the KV layout.~~ Done as item 8, as a
   check rather than sizing.
3. ~~The lease API with reclaim (W1) and quarantine.~~ Done as item 9.
   Still open: publish every window to every node, as one registration per
   node (P1). Until then only window 0 is usable by a fetch.
4. ~~The production `ChunkPlacer` on top of the lease.~~ The destination
   half is done as item 10. The node half is Track C's planner change (N1).
5. Retire or internalize `begin_pipelined_fetch` / `is_pipelined_layer_ready`
   (M4).
6. Later: concurrent fetches (W3).

**Track C:** retrieve wiring per M4 and W4. One handler covers the placer and
the pump, and the fallback goes into fresh general-L1 objects.

**Together:** C9 over Soft-RoCE.

**Suggested PR order:**

1. Track C's build and CI fixes.
2. Track A's small native fixes (generation `0`, `pipelined_unservable_layers`).
3. The adapter and native slot-level issue.
4. The allocator reservation.
5. Lease and placer.
