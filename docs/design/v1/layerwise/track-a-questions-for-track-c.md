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
| Q1 / M2: offsets and window ownership | **Decided 2026-09-25: bounded windows, data stays in place** | Track A: reservation, lease, destination placer and publishing done |
| Q2: digest encoding | Done: slots carry `record_key`; native hashes it | - |
| Q3: planner and `max_sinks` | Done: C5 reworded | - |
| Q4: oversized-plan error | Done: in `contract.py`, raised by the adapter and the native session | - |
| S1: conformance driver | Done: the Aerospike source passes the suite over the real native session | - |
| S2: shape test | Done | - |
| M3: native numbering docstring | Resolved by Option 1 | - |
| M4: retrieve wiring vs the pump | **Decided 2026-09-25: the pump begins the fetch.** Track A's part done: `StorageManager.layer_arrival_source()` | Track C: retrieve wiring |
| W1: windows run out when data stays in place | **Decided:** `lease()` reclaims a whole idle window | Track A: done (`RdmaWindowLeaser`, `reclaim_rdma_window`) |
| W2: which error the placer raises | **Decided** as proposed | Track A: placer; Track C: one handler in retrieve |
| W3: one fetch at a time | **Done: one fetch per window**, up to `window_count` at once | Track C: one source per retrieve |
| W4: fallback after a failed fetch | **Decided:** reload into fresh general-L1 objects | Track A: `abort_write` and pool choice done; Track C: retrieve wiring |
| N1: an object's records are not on one node | **Deferred:** single-node only for now; the connector refuses pipelined fetches on a larger cluster | Owner of the client-server interaction (future work) |
| P1: publishing every window | **Done: one registration covering all windows** | - |
| L1: lease offsets are window-relative, the wire needs slab offsets | **Done:** `dest_offset` is a slab offset, `WindowLease.window_start()` (Track C, `2a3c104d`) | - |
| L2: `ObjectToPlace` has no object key | **Done:** Track C added `key`; Track A's copy is gone | - |
| L3: releasing a lease as `NEVER_FETCHED` | **Done:** aborts the writes without quarantine | - |
| L4: sizing `window_bytes` from `request_bytes` | **Done:** `check_window_holds_request` | Track C: call it at registration |
| F1-F5: review of `fetch-start-proposal.md` | **Option A agreed**; F1 (a finished lease stored its objects back to L2) fixed | Track A: F4's limit if wanted; Track C: F2, F3, F4 |

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
`PipelinedFetchPool`, with no fabric, device, or cluster. The connector is
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
- Every window is published, as one registration per node (done item 11).
  Plan offsets are slab offsets, not window-relative, so the
  "offsets are window-relative" point above no longer holds.

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

### W3. One pipelined fetch per window (done)

Fetches used to run one at a time: the session held one active request, and
`lease()` refused while another fetch was in flight. Now the native client
holds one session per window, so up to `window_count` retrieves fetch at
once. Done as item 13.

- **One source per retrieve.** `StorageManager.layer_arrival_source()`
  returns a new source on every call. A source still holds one fetch, as the
  contract says; several sources share the native client.
- **Routing by generation.** Window `w` uses only generations `g` with
  `(g - 1) % window_count == w`, so an immediate names its window. Every
  native call after the begin takes the generation.
- **Static split of the notification depth.** All windows share one
  completion queue, whose overflow is fatal. Each fetch may use at most
  `depth / window_count` slots. The client asks the device for
  `window_count` times one window's slots, so the share only shrinks when
  the device caps the depth; then a plan that fit before can raise
  `PlanTooLargeError`. A quarantined window keeps its share, so its late
  writes always fit.

A retrieve still falls back when every window is leased or quarantined. Any
early benchmark should report the share of retrieves that went pipelined.

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
can follow as hardening. **Done** as item 11.

## Track C's lease interface (`track/c-planning` at `1280926c`)

Track A checked the seven commits after `4c634404`. They merge into
`track/a-transport` without conflicts. With them merged, `tests/v1/layerwise/`
and `tests/v1/distributed/` pass (1243 passed), and the Aerospike source
passes all 18 source conformance tests, including the three that pin "the
first reply for a slot is final".

Track A will implement `ChunkPlacer` and `WindowLease` on top of
`RdmaWindowPlacer`. Four points first.

### L1. Lease offsets are window-relative; the wire needs slab offsets (open)

`ChunkLocation.dest_offset` is window-relative, and `build_request_fetch`
rejects an object outside `[0, window_bytes)`. Since P1, the native side
expects slab offsets. All windows are one registration, so a slot's offset is
its distance from the start of the slab. `PipelinedFetchPool` picks the
window as `offset / window_bytes` (W3), and the session refuses a slot
outside the first slot's window.

A window-relative plan would send every fetch to window 0 and write into it.
A slab-offset lease fails your check for every window but 0.

**Proposal:** keep `ChunkLocation.dest_offset` window-relative, so your check
stays as it is, and add the base when building the plan:

```python
class WindowLease(Protocol):
    def window_base(self) -> int:
        """Slab offset of the leased window's first byte."""

# in build_request_fetch, after the window check:
ChunkPlacement(..., dest_offset=lease.window_base() + location.dest_offset)
```

The plan then carries slab offsets, which is what the wire and
`memory_obj.meta.address` use. `system-design.md` section 11 ("offsets
are relative to the leased window") would change to match.

### L2. `ObjectToPlace` has no object key (open)

Placing an object means reserving it in L1, and `reserve_write` needs the
object's `ObjectKey`. `ObjectToPlace` has only `chunk_id`, `object_group_id`
and `object_bytes`, and `request_cache_keys` returns serialized strings.

**Proposal:** add `key: ObjectKey` to `ObjectToPlace`. `objects_to_place`
fills it from `obj_keys_per_obj_group[group][chunk]`. The layouts the placer
also needs come from the registered model, so the placer takes them at
construction. Track A's own `ObjectToPlace` in `rdma_window_placer.py` then
goes away, so the name isn't defined twice.

### L3. Releasing a lease as `NEVER_FETCHED` (Track A)

`WindowPlacement` has `complete()`, which keeps the objects, and `abandon()`,
which aborts the writes and quarantines the window. `NEVER_FETCHED` needs a
third path: abort the writes without quarantine, since nothing was issued.
Track A adds it. No change is needed from Track C.

### L4. Sizing `window_bytes` from `request_bytes` (open)

`system-design.md` says the connector sizes `window_bytes` as
`request_bytes(max_pipelined_chunks, slab_alignment)`. It can't, as done
item 8 explains: the windows are carved out when L1 is built, and the layout
only arrives when a worker registers its KV cache. So `window_bytes` stays in
config. Track A will use `request_bytes` for the check at registration and
report the size needed. Please change the doc to say "checks" rather than
"sizes".

### N1 still applies

`ChunkLocation` still has one `node_name` per object. An object's records
are usually on different nodes, so the node has to be per record, as N1
describes. Until the planner takes a node per record, Track A's lease can
place objects but can't give a correct node.

### Out of date in `track-c-status.md`

These are listed as blocked on Track A but are done:

- the fabric-free `ArrivalDriver` (S1);
- publishing every window (P1);
- `StorageManager.begin_pipelined_fetch`, removed in M4. The native
  `issue_pipelined_fetch_by_keys` is still there, and Track A removes it once
  `chunk_fetch_arguments` goes.

`fetch-start-proposal.md` open question 3 calls concurrent fetches "a later
Track A item". W3 is done, so one lease per (request, rank) is possible now.
Track A's review of the proposal is
[below](#track-as-review-of-fetch-start-proposalmd).

## Track C's replies on L1 to L4 (`track/c-planning` at `2a3c104d`)

Merged into `track/a-transport`. Track A's answers:

- **L1 and L2:** `RdmaWindowPlacer` is now the production `ChunkPlacer` and
  `WindowPlacement` the `WindowLease`, so `_RdmaPlacer` and `_RdmaLease`
  moved into production code without a separate adapter. The placer takes
  the layouts and the one node name at construction. Track A's
  `ObjectToPlace` is gone. The placer also refuses an `ObjectToPlace` whose
  `object_bytes` exceeds its group's L1 layout, since the writes would
  overrun the object.
- **`test_rdma_placer_end_to_end.py`:** Track A changed it, since it
  imported the removed `ObjectToPlace`. It now uses `RdmaWindowPlacer`
  through a small wrapper that records the leases. The `type: ignore` and
  the `importorskip` are gone. Please take these edits as yours.
- **L3:** `release(LeaseOutcome.NEVER_FETCHED)` aborts the writes and frees
  the window at once, without quarantine.
- **L4:** `check_window_holds_request(window_bytes, model,
  max_pipelined_chunks, align_bytes)` in `rdma_window_placer.py` raises
  `ValueError` with the size needed. Nothing defines `max_pipelined_chunks`
  yet, so Track C's registration wiring should decide where it comes from
  and call the check. The native one-chunk check (done item 8) stays as the
  readiness gate.
- **Single node:** the native driver refuses to initialize pipelined
  fetches unless the cluster has exactly one node. The error becomes
  `pipelined_fetch_init_error`, so every retrieve falls back, and nothing is
  registered with any node. The count is taken only at init; a node added
  later isn't detected.
- **A slot lands at its plan offset and nowhere else: confirmed** for
  everything on the client side, and for the mock server:
  - `NativePlanIssuer` passes `SlotPlacement.offset` through unchanged, and
    the native client sends it as `<digest>@<offset>:<length>`
    (`kv_sink_client.cpp`). No other destination exists in the request.
  - `validate_slots_in_one_window` refuses a plan whose slots leave the
    first slot's window before anything is sent.
  - The mock server writes at `client_addr + offset` and refuses anything
    outside the registered window. `rdma_equivalence_test` checks that no
    bytes land outside the requested offsets.
  - Caveat: the real server's side, that it writes exactly `length` bytes at
    `offset`, is unverified until A8.

## Track A's review of `fetch-start-proposal.md`

Track A agrees with option A: report the hit at lookup, lease and fetch at
retrieve. Windows are the scarce resource, and nothing is leased for a
request that never reaches retrieve. Points from the transport side, most
important first.

### F1. A finished lease writes its objects back to L2 (Track A bug, fixed)

`WindowPlacement.release(FINISHED)` calls `L1Manager.finish_write` on
objects reserved with `is_temporary=False`. For a permanent object,
`finish_write` notifies the listeners, and `StoreController` queues the key
for an L2 store. So every pipelined fetch would write its objects straight
back to Aerospike. Today's prefetch avoids both halves of this:

- it reserves with `is_temporary` from `select_l1_retentions`, so under the
  `default` policy the objects are temporary;
- it finishes with `finish_write_and_reserve_read`, which `StoreController`
  ignores on purpose.

Point 4 of the proposal assumes the placer already follows the retention
policy. **Fixed as done item 17:** the placer takes the retention choice at
construction as `select_retentions`, with the signature of the policy's
`select_l1_retentions`. It defaults to keeping nothing, like the `default`
policy. `FINISHED` ends with `finish_write_and_reserve_read` then
`finish_read`. Temporary objects are then freed at once, as the proposal
expects, and retained ones become readable without a store.

For Track C: `test_rdma_placer_end_to_end.py` now builds its placer with a
retain-everything choice, since it reads the landed data back afterwards.

### F2. Release `FINISHED` only after the sink's copies complete

Under `default` retention, `FINISHED` frees the window objects, and the
window can be leased again at once. The sink's per-layer copies run on a GPU
stream, so retrieve must release the lease only after the sink's last copy
has completed, not when it was enqueued. The same holds on the fallback
path: its release is `ABANDONED`, which quarantines the window, so it is
safe for `fetch_timeout_seconds` but shouldn't rely on that.

### F3. Two requests deferring the same keys

Shared prefixes make this common. When two requests defer the same chunk,
the second request's placer fails: its `reserve_write(mode="new")` is
refused because the first request holds the key write-reserved. The
proposed fallback, a whole-object load "into fresh L1 objects" under the
same keys, is refused for the same reason. Retrieve needs a rule for keys
another request is fetching or has just fetched. Options:

- re-check L1 at retrieve, and read-lock keys that are now resident instead
  of placing them;
- for keys still write-reserved by another fetch, wait for that fetch
  within the layer budget, or recompute (open question 1).

### F4. Eligibility should also check the slot count

A plan larger than the per-fetch notification share is refused at
`begin_fetch` with `PlanTooLargeError`. With W3's static split, the share is
`depth / window_count`, which `pipelined_max_slots_per_request()` reports.
At retrieve that refusal means the fallback. Checking only `request_bytes`
at lookup misses it. The slot count follows from the fetch model and
`max_record_bytes`, so it can be checked at lookup. Track A can expose the
limit through the storage manager if Track C wants it.

### F5. Eligibility can use readiness for the single-node rule

Pipelined fetch is refused on clusters of more than one node (N1
deferred), and the refusal shows up as not ready. So "the adapter has a
ready pipelined path" covers it. `StorageManager.pipelined_fetch_node_name()`
raises exactly when it doesn't.

### Open questions, from the transport side

- **Q2, fallback budget.** A whole-object fallback for a window-sized request
  (512 MiB in the proposal's example) must land in the 2.5 s left after the
  pump gives up, so about 200 MB/s end to end. That is plausible for one
  request but not under load. A byte cap below `window_bytes` at lookup is
  the safer default until it's measured.
- **Q3, more than one reader.** W3 is done, so one lease per (request, rank)
  is possible now. Each lease holds its own window, though. With
  `window_count` windows, a request with `world_size` ranks takes
  `world_size` of them, and `world_size > window_count` can never run.
  Keeping `world_size == 1` for the first version is right. After that,
  either the eligibility check counts windows, or one lease covers every
  rank of a request, as one fetch with one sink per rank.
- **Quarantine after failures.** Each abandoned fetch holds its window for
  `fetch_timeout_seconds` (30 s by default). With four windows, four
  failures inside 30 s send every pipelined request to the fallback until
  the quarantines end. That is correct but worth a metric.
- **A record gone at retrieve (Q1).** On the transport side, the server
  declines the slot, the layer becomes `UNSERVABLE`, and the pump gives up
  on it at once rather than after the timeout.

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
   - One lease per window (W3, item 13). Refusals follow W2.

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
11. Every window is published (P1). `RdmaContext::register_l1` registers the
    whole window range as one memory region, and each node's
    `kv-sink-register` publishes it, so the register command has no window
    index. `PipelinedFetchSession` refuses a request whose slots aren't all
    inside the window of its first slot. On the Soft-RoCE VM, a fetch into
    window 2 lands byte-identical, and a write past the range is refused.
12. The storage-manager pipelined pair is retired (M4).
    `StorageManager.layer_arrival_source()` returns an
    `AerospikeLayerArrivalSource` from the native adapter (a new one per
    call since item 13). With no pipelined adapter it raises
    `LayerwiseContractError`, so retrieve's one handler covers that case too.
    `begin_pipelined_fetch`, `finish_pipelined_fetch`,
    `abandon_pipelined_fetch` and `is_pipelined_layer_ready` are gone from
    the storage manager and the L2 adapters; only the source calls the
    native ones.
    - For Track C: nothing in production calls `chunk_fetch_arguments` in
      `native_fetch.py` any more. The source issues slot for slot through
      `issue_pipelined_fetch_by_slots`. `chunk_fetch_arguments` and the
      native `issue_pipelined_fetch_by_keys` can go when you're ready.
    - Retrieve should wrap the accessor call in the same
      `LayerwiseContractError` handler as the placer and the pump.
13. Concurrent fetches, one per window (W3). See
    [aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#concurrent-fetches).
    - Native: `PipelinedFetchPool` holds one `PipelinedFetchSession` per
      window and routes each immediate by its generation. The native
      `finish_pipelined_fetch`, `abandon_pipelined_fetch` and
      `pipelined_unservable_layers` now take the generation.
      `is_pipelined_layer_ready` has no default generation any more.
    - Python: `layer_arrival_source()` returns a new source per call, and
      `RdmaWindowLeaser` holds one lease per window.
    - Over Soft-RoCE, two fetches in windows 0 and 1 complete from one
      completion queue with the right bytes. After the second is abandoned,
      a late write carrying its generation is not credited to the window's
      next fetch.
    - For Track C, retrieve calls `layer_arrival_source()` once per retrieve
      and never shares a source between retrieves.
    - For Track C, on your two questions:
      - One plan flattener: `NativePlanIssuer` now calls your
        `pipelined_fetch_arguments` from `native_fetch.py`. Its own copy is
        gone.
      - A decline is final: agreed. The native side already works this way.
        `LayerReadiness::note_unservable` marks the slot as seen without
        landing it, so a later write for it counts as a duplicate and the
        layer stays unservable. Please also pin the reverse order in the
        conformance suite: a slot that lands and is then declined leaves its
        layer resident.
14. A7 is ready to run on hardware. `efa_imm_probe` checks whether a
    write-with-immediate consumes a posted receive: it starves the receive
    queue, then posts the missing receives. See
    [aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#receive-queue-depth-and-device-limits).
    - RC baseline on Soft-RoCE: yes. A starved write stalls and lands no
      bytes until a receive is posted; with `rnr_retry 0` the writer fails.
      `make test` pins this.
    - SRD: the probe builds with `EFA=1` against rdma-core 50, and also
      checks the unsolicited write-receive mode when `efadv.h` declares it.
      **Not run yet: it needs an EFA instance with RDMA write.** If
      it reports `data_without_notification=yes`, the wire contract may need
      a handshake, which would change the plan format.
15. The production `ChunkPlacer` (L1 to L4). `RdmaWindowPlacer.lease(objects)
    -> WindowPlacement` implements Track C's `ChunkPlacer` and
    `WindowLease`. It releases as `FINISHED`, `NEVER_FETCHED` (no
    quarantine) or `ABANDONED`. `check_window_holds_request` is the
    registration check. The native driver refuses pipelined fetches on a
    cluster of more than one node. See
    [Track C's replies on L1 to L4](#track-cs-replies-on-l1-to-l4-trackc-planning-at-2a3c104d).
16. The placer's node name: `StorageManager.pipelined_fetch_node_name()`,
    from the native `pipelined_fetch_node_name()` through the adapter.
    It raises `LayerwiseContractError` with the init error when pipelined
    fetch isn't ready, the same failure retrieve already falls back on.
    For Track C, the registration wiring can then build
    `RdmaWindowPlacer(l1_manager, leaser, layouts,
    storage_manager.pipelined_fetch_node_name())`.
17. The placer follows the prefetch retention policy and never stores its
    objects back to L2 (F1). `RdmaWindowPlacer` takes `select_retentions`,
    which defaults to keeping nothing. `FINISHED` ends with
    `finish_write_and_reserve_read` then `finish_read`, so objects that
    aren't retained are freed at once. A test listener checks that no
    write-finished notification, the store controller's trigger, is sent
    under either retention.

These were verified on the Soft-RoCE VM
([rdma_testing_on_windows.md](../distributed/l2_adapters/rdma_testing_on_windows.md)):

- `lmcache_aerospike` builds with `BUILD_WITH_AEROSPIKE_RDMA=1` against
  C client 7.3.0;
- device-free logic harness: 364 checks pass;
- fabric harness over `rxe0`: 415 checks pass;
- `tests/v1/layerwise/` and `tests/v1/distributed/` pass (1289 passed, 92
  skipped, with Track C's `2a3c104d` merged), including the conformance
  suite over both sources, concurrent fetches over the real native pool, and
  Track C's end-to-end retrieve over the production placer;
- the pytest wrappers in `tests/v1/distributed/rdma/` pass.

`record_node` and `issue_pipelined_fetch_by_slots` have not run end to end.
That needs an Aerospike server, and for the slot path, one built from the
`kv-sink` branch (A8).

**Track A, next:**

1. Get the allocator reservation (done item 7) through `l1_manager` review.
2. ~~Size `window_bytes` at init from the KV layout.~~ Done as item 8, as a
   check rather than sizing.
3. ~~The lease API with reclaim (W1) and quarantine.~~ Done as item 9.
   ~~Publish every window to every node.~~ Done as item 11.
4. ~~The production `ChunkPlacer` on top of the lease.~~ Done as items 10
   and 15, single-node only (N1 deferred).
5. ~~Retire or internalize `begin_pipelined_fetch` /
   `is_pipelined_layer_ready` (M4).~~ Done as item 12.
6. ~~Later: concurrent fetches (W3).~~ Done as item 13.

**Track C:** retrieve wiring per M4 and W4. One handler covers the placer and
the pump, and the fallback goes into fresh general-L1 objects.

**Together:** C9 over Soft-RoCE.

**Suggested PR order** into `dev`, following `track/a-transport`. Each PR
needs the ones before it unless noted.

| # | PR | Commits | Needs |
|---|---|---|---|
| 1 | Track C: planner, contract, conformance suite, build fixes | up to `4c634404` | - |
| 2 | Slot-level native issue, `record_node`, `NativePlanIssuer`, `PlanTooLargeError`, fabric-free harness (done items 1-6) | `d6d48818` (lyndon's shape test) through `83210bf1`, with the decision-doc commits between them | 1 |
| 3 | Retire the storage-manager pipelined pair (M4, item 12) | `a3b4fef3` | 2 only; can go before 4-6 |
| 4 | L1 window reservation, `delete_if_none_locked`, `abort_write`, `window_bytes` check (items 7-8) | `83f391d3`, `7b7cee99` | 1; needs `l1_manager` review |
| 5 | Lease and placer (items 9-10) | `0703d954`, `d4a5651b` | 4 |
| 6 | One registration covering every window (P1, item 11) | `1c33157a` | 2, 4 |
| 7 | Concurrent fetches, one per window (W3, item 13) | `6fd5fc4d` | 3, 5, 6 |
| 8 | Production `ChunkPlacer`, `NEVER_FETCHED`, registration check, single-node gate (item 15) | the commit after the merge of `2a3c104d` | 5, 7, and Track C's lease interface (`1280926c` to `2a3c104d`) |

Why this order: 2 and 3 touch only the transport and the adapters, so they
can merge while 4 waits on `l1_manager` review. PR 4 is the only one that
changes the general L1 allocator, so it stays small and separate. 7 changes
native signatures used by 2's source and 5's leaser, so it goes last. The
commit hashes change if the branch is rebased; the grouping does not.
