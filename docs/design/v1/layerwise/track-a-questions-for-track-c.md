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
| Q1 / M2: offsets and window ownership | **Decided 2026-09-25: bounded windows, data stays in place** | Track A: reservation, lease API, placer |
| Q2: digest encoding | Done: slots carry `record_key`; native hashes it | - |
| Q3: planner and `max_sinks` | Done: C5 reworded | - |
| Q4: oversized-plan error | Done in `contract.py` | Track A: raise it from the adapter |
| S1: conformance driver | Done: `ArrivalDriver` and the parametrized suite | Track A: register the Aerospike source |
| S2: shape test | Done | - |
| M3: native numbering docstring | Resolved by Option 1 | - |
| M4: retrieve wiring vs the pump | **Decided 2026-09-25: the pump begins the fetch** | Track C: retrieve wiring; Track A: retire the storage-manager pair |
| W1: windows run out when data stays in place | **Open** (Track A agrees, with conditions) | Both |
| W2: which error the placer raises | **Proposed by Track A** | Track C to confirm |
| W3: one fetch at a time | Known limit, not yet scheduled | Track A |

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
subclasses the existing error, current handlers still catch it. Native
already throws a distinct `rdma::PlanTooLargeError` (pybind:
`PipelinedPlanTooLargeError`), and `NativePlanIssuer` checks `len(plan.slots)`
against `pipelined_max_slots_per_request()` before sending. Both still surface
as plain `LayerwiseContractError`; switching them to `PlanTooLargeError` is on
Track A's list.

### S1: conformance driver

`ArrivalDriver` in `fakes.py` has `land_slot(slot_index, generation)` and
`decline_slot(slot_index, generation)`. `test_arrival_source_conformance.py`
runs once per entry in `SOURCE_HARNESS_FACTORIES`. Track A registers the
Aerospike source there with a fabric-free driver that feeds encoded
immediates and declined replies into the real native session.

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

**Where the code stands.** None of this exists yet:

- nothing reserves the windows, so ordinary L1 objects can be allocated inside
  them;
- only window 0 is published to the nodes;
- there is no lease API.

Until the reservation lands, the blast-radius argument does not hold.

### M4: the pump begins the fetch

Retrieve runs `LayerArrivalPump(source, sink).run(plan)` over an
`AerospikeLayerArrivalSource`, and falls back to a whole-object load on
`LayerwiseContractError`. The storage-manager `begin_pipelined_fetch` /
`is_pipelined_layer_ready` pair is retired or made internal to the source.

The production `ChunkPlacer` runs *before* the pump, so retrieve catches a
placer refusal and falls back there too. Track C builds that into the
retrieve wiring.

## Open points

### W1. Windows run out when data stays in place

**Track C's proposal:** a window is free again only once every object in it
has been evicted. Cache entries live a long time, so after `window_count`
retrieves every window could be full and the pipelined path would stop being
used. When `lease()` finds no free window, it may evict an idle window's
objects to reclaim it. Those objects were just read from Aerospike, so they
are unmodified and can be read again from L2. Only objects currently being
read hold a window.

**Track A agrees, with five conditions:**

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
   decide it.

**Cost:** a delete changes metadata only, with no I/O, so reclaiming inside
`lease()` adds no network time to retrieve. Evicted entries become L2 hits
again. If Aerospike has since expired them, it's an ordinary miss and the KV
is recomputed.

**Fallback:** if reclaim evicts too often in practice, copy objects out to
general L1 after the pump finishes. That's off the TTFT path, since every
layer has reached the GPU by then. It's not needed unless measurements say
so.

### W2. Which error the placer raises

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

Until that exists, `lease()` also refuses while another fetch is in flight.

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
4. A distinct native `PlanTooLargeError` and a pre-send slot-count check.

These were verified on the Soft-RoCE VM
([rdma_testing_on_windows.md](../distributed/l2_adapters/rdma_testing_on_windows.md)):

- `lmcache_aerospike` builds with `BUILD_WITH_AEROSPIKE_RDMA=1` against
  C client 7.3.0;
- device-free logic harness: 298 checks pass;
- fabric harness over `rxe0`: 341 checks pass;
- `tests/v1/layerwise/` and `tests/v1/distributed/rdma/` pass under pytest.

`record_node` and `issue_pipelined_fetch_by_slots` have not run end to end.
That needs an Aerospike server, and for the slot path, one built from the
`kv-sink` branch (A8).

**Track A, next:**

1. Rebase onto Track C's branch (`PlanTooLargeError`, `ArrivalDriver`,
   `chunk_fetch_arguments`).
2. Raise `PlanTooLargeError` from `NativePlanIssuer` for both the pre-check
   and the native error.
3. A fabric-free `ArrivalDriver` over the real session, registered in
   `SOURCE_HARNESS_FACTORIES`. It needs a native test hook to feed immediates
   and declined replies from Python.
4. The allocator reservation PR, including the all-or-nothing delete (W1.1).
5. Size `window_bytes` at init from the KV layout.
6. The lease API with reclaim (W1) and quarantine. Publish every window to
   every node.
7. The production `ChunkPlacer` on top of the lease, raising per W2.
8. Retire or internalize `begin_pipelined_fetch` / `is_pipelined_layer_ready`
   (M4).
9. Later: concurrent fetches (W3).

**Track C:** retrieve wiring per M4, with the fallback around the placer too,
and confirmation of W1 and W2.

**Together:** C9 over Soft-RoCE.

**Suggested PR order:**

1. Track C's build and CI fixes.
2. Track A's small native fixes (generation `0`, `pipelined_unservable_layers`).
3. The adapter and native slot-level issue.
4. The allocator reservation.
5. Lease and placer.
