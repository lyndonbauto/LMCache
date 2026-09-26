# Layerwise KV cache loading: system design and contracts

Audience: the three people building this. Read
[exercise-goal.md](exercise-goal.md) first for why the system exists; this
document is what it is made of, who owns which part, and exactly where the
parts meet.

## 1. End-to-end picture

One remote cache hit, from the engine asking for a prompt's KV cache to
attention computing on it:

```
 vLLM scheduler
   | get_num_new_matched_tokens -> "this prompt is a remote hit"
   v
 LMCache MP connector                                       [Track B]
   |
   |  (1) what do we need, and where does it live?
   v
 Slot planner + shard plan                                  [Track C]
   |     csrc/storage_backends/aerospike/{slot_planner,shard_plan}.*
   |     produces a LayerFetchPlan: every byte range, per layer, per node
   v
 PipelinedFetchSession -> kv-sink wire -> Aerospike nodes    [Track A]
   |     csrc/storage_backends/aerospike/{pipelined_fetch_session,kv_sink_client}.*
   |     one RDMA_WRITE_WITH_IMM per slot, immediate = (generation << 16) | slot
   v
 L1 pinned host buffer
   |  (2) "layer N is complete in host memory"               <-- CONTRACT 1
   v
 LayerArrivalPump                                           [Track C]
   |     lmcache/v1/layerwise/pump.py
   |  (3) "copy layer N to the GPU now"                      <-- CONTRACT 2
   v
 Per-layer H2D + CUDA IPC events                            [Track B]
   |     lmcache/v1/multiprocess/{layerwise_schedule,layer_progress}.py
   |     lmcache/v1/multiprocess/object_group_transfer.py::transfer_kv_layerwise_h2d
   v
 vLLM attention, layer N
         wait_for_layer_load(layer_name) returns
```

Steps (2) and (3) are the two frozen contracts. Everything above (2) is Track
A, everything below (3) is Track B, and Track C owns (1), the pump, and both
contracts.

## 2. Why the boundary is where it is

The split follows the hardware, because that is what actually blocks people:

- Track A needs an RDMA fabric and an Aerospike cluster. It needs no GPU.
- Track B needs a GPU. It needs no RDMA.
- Track C needs neither and can run entirely on CPU.

Any other cut would leave someone unable to test their own work. With this
cut, each track runs its full test suite on the hardware it has, substituting
a fake for the other side. That is the property to protect: **if a change
makes one track's tests require the other track's hardware, the change is
wrong.**

## 3. Contract 1 -- layer arrival

`lmcache/v1/layerwise/contract.py::LayerArrivalSource`

Track A implements it. Track C consumes it. It answers exactly one question:
has every byte of layer N of this fetch landed in host memory?

```python
def begin_fetch(self, plan: LayerFetchPlan) -> int: ...
def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus: ...
def finish_fetch(self, generation: int) -> None: ...
def abandon_fetch(self, generation: int) -> None: ...
```

### Arrival is three-valued

`LayerArrivalStatus` is `PENDING`, `RESIDENT`, or `UNSERVABLE` -- not a
boolean. The prototype used `is_pipelined_layer_ready() -> bool`, which
returned `False` both for "not yet" and for "this backend cannot do pipelined
fetch at all". Those demand opposite responses from the caller: keep waiting,
versus give up and do a whole-request load. A boolean gives the caller no way
to tell, so the caller either polls forever on an impossible fetch or bails
out of a merely slow one.

`UNSERVABLE` is terminal for the whole fetch, not just that layer. A declined
slot means the plan cannot be satisfied, so the pump abandons both sides and
the caller falls back.

### Generations are explicit and non-zero

Every call naming an in-flight fetch carries its generation. The transport
rejects a stale one with `StaleGenerationError` rather than ignoring it.

This is not ceremony. The RDMA immediate is `(generation << 16) | slot`, so a
write belonging to an abandoned fetch can still land in the buffer after a new
fetch has started. Without a generation check, that late write is credited to
the new fetch and a layer is reported resident before its data arrived --
which surfaces as silently wrong model output, the worst possible failure
mode. `0` is reserved for "no fetch" so that a zero-initialised or defaulted
generation fails loudly instead of aliasing a real one.

### Thread safety

`poll_layer` may be called from a different thread than `begin_fetch`.
Implementations must be safe for concurrent polls.

## 4. Contract 2 -- layer load

`lmcache/v1/layerwise/contract.py::LayerLoadSink`

Track B implements it. Track C drives it.

```python
def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None: ...
def load_layer(self, layer_id: int) -> None: ...
def finish_load(self, generation: int) -> None: ...
def abandon_load(self, generation: int) -> None: ...
```

### Load order is mandatory, not advisory

`load_layer` must be called in the order given to `begin_load`, which is
ascending layer order. Transfers share a CUDA stream, so a consumer waiting on
layer N implicitly waits on everything queued before it. Issue layer 5 before
layer 3 and layer 3 appears ready the moment layer 5's copy completes,
regardless of whether layer 3's data was correct. Nothing crashes; the model
just produces wrong tokens.

Calls come from a single thread, in the sequence `begin_load`, then
`load_layer` once per layer, then `finish_load` or `abandon_load`.

### Abandoning must wake waiters

`abandon_load` has to fail every parked waiter, not just drop the load. It is
the path taken when the transport declines a layer, and a silent abandon turns
a recoverable cache miss into a hang in vLLM's attention.

`finish_load` rejects a load where some expected layer was never issued, for
the same reason.

## 5. The junction

`lmcache/v1/layerwise/pump.py::LayerArrivalPump` is the only component that
depends on both contracts. It polls the source for each layer in plan order
and hands each resident layer to the sink.

It owns the policy neither side should: how long to wait, and what to do when
a layer never arrives. On any failure it abandons **both** sides before
raising, so a caller that catches the exception can fall back to a
whole-request load without first working out how far the fetch got.

One ordering detail is deliberate: the timeout deadline is checked *after*
polling, so a layer that is already resident on the first poll is never
rejected for lateness. Reversing those two lines makes the pump fail spuriously
under load, exactly when it matters.

## 6. Testing without the other half

`lmcache/v1/layerwise/fakes.py` ships three substitutes as library code, not
test helpers, so that a change to a protocol breaks its fake immediately:

- `ScriptedLayerArrivalSource` -- a transport whose arrivals the test drives
  by hand. Nothing arrives on its own, so a forgotten delivery hangs the pump
  rather than passing by accident.
- `RecordingLayerLoadSink` -- a loader that records copies and enforces the
  ordering rule, so out-of-order issue fails on a laptop instead of producing
  wrong tokens on a GPU.
- `UnservableLayerArrivalSource` -- declines everything, so fallback paths are
  actually reached. Code tested only against the scripted source can pass with
  an unreachable fallback.

`tests/v1/layerwise/` is the conformance suite. Both real implementations are
expected to pass it. It is also the worked example of how to test against the
contract without touching private state -- copy its style.

## 7. What each track owns

### Track A -- Transport

`csrc/storage_backends/aerospike/`: `rdma_context`, `kv_sink_client`,
`kv_sink_fanout`, `pipelined_fetch_session`, `pipelined_fetch_issue`,
`notification_depth`, `connector_pipelined_rdma`.
Plus `lmcache/v1/distributed/l2_adapters/` where the adapter surfaces it.

Implements `LayerArrivalSource`. Tests on Soft-RoCE plus a real Aerospike
server, no GPU. See [aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md).

### Track B -- Consumption

`lmcache/v1/multiprocess/`: `layerwise_schedule`, `layer_progress`,
`object_group_transfer::transfer_kv_layerwise_h2d`,
`transfer_context/worker_transfer`, and
`lmcache/integration/vllm/lmcache_mp_connector.py`.

Implements `LayerLoadSink`. Tests on a GPU box, no RDMA. See
[layerwise-load.md](../multiprocess/layerwise-load.md).

### Track C -- Planning and junction

`csrc/storage_backends/aerospike/`: `slot_planner`, `shard_plan`,
`layer_pipeline`, `memory_layout_conversion`.
Plus all of `lmcache/v1/layerwise/`.

Produces `LayerFetchPlan`, owns `LayerArrivalPump`, and owns both contracts.
Tests as pure CPU logic. Track C is the integrator: when A and B disagree,
Track C adjudicates and the contract changes by agreement.

The slot arithmetic exists twice on purpose, and the duplication is the price
of a hardware-free test suite. `FetchPlanner` in `lmcache/v1/layerwise/` lays
out slots in Python so Track C's tests need no native build; the C++
`SlotPlanner` does the same for the production fetch path, driven by the
registered `MemoryLayoutDesc`. Binding the C++ planner into Python instead
would give one implementation but would make every planning test depend on an
extension built with `BUILD_AEROSPIKE=1`, which breaks C10.

Planning is two stages, because the two kinds of input arrive at different
times and change at different rates:

- `ModelLayout` is built once per model, via `ModelLayout.from_registration`,
  from the exact arguments `StorageManager.set_object_group_layouts` receives.
  It answers "where does global layer N sit inside its object group's
  payload". Geometry is held **per kernel group and never flattened**: the
  hazard in a hybrid model is not that strides are unpredictable but that one
  kernel group's stride gets applied to another group's layers, which yields a
  plausible tensor and no error.
- `FetchPlanner(layout).plan(request, record_keys)` runs per request.
  `PlanRequest` carries only what the request decides -- which chunks, on
  which nodes, at which destination offsets, under which record cap.

Record keys are looked up rather than passed in. A slot is exactly one stored
record, identified by `(chunk_id, layer_id, plane, piece)` -- the same key the
native `pipelined_fetch_session` joins records on. The caller cannot enumerate
those records before planning, because which pieces exist depends on how the
planner cuts planes, so the planner asks a `RecordKeySource` as it goes.
Note that the key has no object-group field: a layer belongs to exactly one
object group, which is why `ModelLayout` rejects a layer appearing in two
kernel groups.

The duplication is bounded to *layout*, not geometry: the published
`MemoryLayoutDesc` remains the only source of truth for shapes and strides.
What both sides implement independently is plane striding, record cutting and
layer-major ordering. The two must agree exactly, so
`plane_segment_bytes` is mirrored from `shard_plan.h` with the formula stated
in both, and the Python tests pin concrete byte values rather than relying on
the formula being re-derived correctly. If these drift, a slot will name a
record the write side never produced.

Each side's own tests cannot see a drift, so there is a direct guard:
`tests/v1/distributed/rdma/fixtures/slot_plans.txt` holds planning cases that
both read. `csrc/slot_plan_dump.cpp` prints the production `SlotPlanner`'s
slots for each case, and `test_slot_plan_parity.py` plans the same cases with
`FetchPlanner` and diffs them slot for slot -- including order, since a slot's
index is its position. It lives beside the other C++ harnesses because it
needs a compiler, which keeps `tests/v1/layerwise/` free of even that
dependency; it skips rather than fails where no compiler exists.

Add a case to the fixture whenever either planner grows a shape it did not
handle before. A case only the harness runs is not a guard.

The fixture also checks something the planners cannot check alone: that a
record holding a slot's bytes was actually stored.
`ModelLayout.record_index_for` names the record behind a slot, and the
harness independently searches the write side's own `choose_shard_plan`
output for a record covering exactly that range. Where they disagree, the
fetch would pull a real record into the right address and the model would
read the wrong bytes.

## 10. How the writer keeps records inside a layer

A layer can only be fetched on its own if no record it needs also holds
another layer's bytes. The writer is handed a key and a byte count, not a
model, so it has to be told the layout ahead of time.

**At registration**, `register_kv_cache` calls
`StorageManager.set_object_group_layouts`, and the native adapter forwards
each object group's *plane runs* -- one `(plane_bytes, planes)` pair per
kernel group, in payload order, computed by `record_plane_runs` -- to the
connector's `set_record_layouts`. For a Mamba/GDN hybrid whose object group
holds 1024-byte attention planes and 10000-byte state planes:

```text
object group 0: [(1024, 4), (10000, 2)]      # 4 + 2*3 = 10 records at a 4096 cap
```

**On each write**, `choose_shard_plan` looks the payload size up and
`make_layered_shard_plan` cuts every plane of every run against that run's
own plane size, numbering records in payload order:

```text
records 0-3   first run, one per 1024-byte plane
records 4-6   second run, plane 0: 3334 / 3334 / 3332
records 7-9   second run, plane 1: 3334 / 3334 / 3332
```

The meta record stores the runs as a `runs` bin, e.g.
`"1024:1024:4,10000:3334:2"`, so a reader recovers every range without
knowing the model; `require_consistent_runs` rejects runs that do not cover
the object's size in its record count.

**On the read side**, `ModelLayout.record_index_for` applies the same
numbering from `ModelLayout.plane_runs`. That `record_plane_runs` and
`plane_runs` agree is pinned by `test_native_record_layouts.py`; that the
numbering matches the writer's records is pinned by the parity fixture, which
now includes three hybrid cases and maps every one of their slots to a stored
record.

Three properties are deliberate:

- **Uniform models are unchanged.** When every run shares one plane size,
  `make_layered_shard_plan` returns exactly the plan `make_shard_plan` builds
  from that size -- same records, same indices, no `runs` bin -- so existing
  objects stay readable and readers that predate runs can read new uniform
  objects. A reader that predates runs fails loudly on a *hybrid* object,
  because `read_payload_record` checks each record's size.
- **An ambiguous size is not guessed.** If two object groups have the same
  payload size but different runs, the writer cannot tell which layout a
  payload has, so `record_layouts_by_payload` drops that size and it is
  byte-count sharded. `record_index_for` refuses for those groups, and
  `test_slot_plan_parity.py` asserts both that it refuses and that the
  refusal is justified. The caller falls back to a whole-request load -- the
  path `LayerArrivalStatus.UNSERVABLE` exists for.
- **Layouts accumulate.** `set_record_layouts` adds to what earlier
  registrations published rather than replacing it, so a second model sharing
  a connector cannot re-cut the first model's records. The ambiguity rule is
  applied across everything registered, which the Python side cannot see: a
  reader for one model could, in principle, name records for a size another
  model made ambiguous. That fails loudly (the record is missing or the wrong
  size), not silently.

If storage rejects the layout, registration logs a warning and continues;
writes are then byte-count sharded as they were before layouts were
published, and only layer-at-a-time fetch is lost.

## 11. What still has to come from the native side

A plan names records by **user key**. `RecordKeys` resolves a slot to a
record index via `record_index_for` and forms the key the way `connector.cpp`
does (`<cache key>|m` for a one-record object, `<cache key>|s|<index>`
otherwise). Turning a key into a digest is the client's job:
`issue_pipelined_fetch_by_keys` calls `record_digest_hex` for each slot and
hands the session the digests it has always taken. Python never hashes --
most OpenSSL builds disable RIPEMD-160 -- and when the transport stops using
an info command, only the native side changes.

Two facts per chunk still cannot come from Track C, and it is worth being
precise about why, so nobody re-attempts them in Python:

- **Nodes.** Which node owns a record comes from the cluster's partition map,
  which only the C client has. It reaches the plan as a name in
  `LayerFetchPlan.node_names`. Note that ownership is per *record*: each
  `|s|<i>` segment hashes to its own partition, so one chunk's records are
  generally spread across nodes. The chunk-level native call binds a whole
  chunk to one node (`ChunkNodeBinding`), and `chunk_fetch_arguments` refuses
  a plan that would need otherwise.

  **Interim: single-node clusters only.** The planner still takes one node
  per object from the placer and sends every record of that object there.
  That is correct when the cluster has one node, as on the Soft-RoCE setup.
  On more nodes most slots would reach a node that does not hold their
  record and be declined, so the pipelined path must not be enabled there;
  such a request takes the whole-object path. Routing each record to the
  node that holds it belongs to the client-server interface and is future
  work (see [track-c-status.md](track-c-status.md), "Future work").
- **Destination offsets.** Chosen when a chunk's object is placed in the
  request's leased RDMA window (see "Window ownership" below), so they are an
  input to planning rather than something planning derives.

Note the record cap `RecordKeys` is built with must be the cap the object
was *written* under, not today's. It decides how many records exist, so a
cap that changed between write and read renames every record. The connector
reports the cap it writes under as `max_record_bytes()`.

### Slot-level issue: the plan is the only source of truth

The chunk-level call has the native `SlotPlanner` re-expand chunk
placements into slots, so there are two planners that must agree, and it
cannot express per-record node ownership. It is being replaced by a
slot-level call that takes the plan as given.
`pipelined_fetch_arguments(plan)` produces its input:

```text
node_names: ["node-a", "node-b"]
slots:      [(node_index, record_key,                 dest_offset, length, layer_id), ...]
            [(1,          "m@00000000@0@9f2c|s|0",    0,           4096,   0       ), ...]
```

A slot's position in `slots` is its slot number on the wire. The native side
hashes each key with `record_digest_hex`, groups slots by
`node_names[node_index]`, splits each node's sinks to its `max_sinks`, and
counts readiness per layer from `layer_id`. It keeps the checks it does
today: the device receive-queue cap (raised as `PlanTooLargeError`), sinks
inside the leased window, declines, and stale generations.
`chunk_fetch_arguments` and `issue_pipelined_fetch_by_keys` stay until the
slot-level call replaces them.

### Planning from a real request

`lmcache/v1/layerwise/request_fetch.py` builds a plan from exactly what the
retrieve path already has, so a plan always names the objects a whole-object
retrieve would have read:

```text
register_kv_cache
  group_layout_descs, kernel layer indices  ─┬─> storage.set_object_group_layouts  (writer)
                                             └─> ModelLayout.from_registration
                                                 -> FetchModelRegistry[(model, world_size)]

retrieve request (IPCCacheServerKey from vLLM)
  resolve_obj_keys(key, range(num_object_groups))    one ObjectKey per chunk per group
  run_pipelined_retrieve(model, keys, cap, placer, pump)     on a worker thread
    -> objects_to_place(model, keys)                 aux groups skipped; window groups
                                                     keep chunks >= first_in_window_chunk
    -> ChunkPlacer.lease(objects) -> WindowLease     one window per request  <- Track A
    -> build_request_fetch(model, keys, cap, lease)  lease.locate() per object; each
                                                     checked inside the window, no overlap
    -> pump.run(fetch.plan)                          begin_fetch, then layer by layer
    -> lease.release(FINISHED | NEVER_FETCHED | ABANDONED)   exactly once
  except LayerwiseContractError (incl. PlanTooLargeError):
    whole-object load into fresh objects in general L1
```

`run_pipelined_retrieve` (`lmcache/v1/layerwise/pipelined_retrieve.py`) is
that whole flow. Any refusal before or during the fetch is raised as a
`LayerwiseContractError`: a request the layout cannot plan, a placement the
planner rejects, the placer's refusals, and the pump's timeout or unservable
layer. Only loader errors propagate unchanged. The lease is released exactly
once, and the outcome follows where the fetch stopped:

| Where it stopped | Released as | Why |
|---|---|---|
| Keys don't match the model | not leased | Checked before `lease()` |
| Placer refused | not leased | No lease was granted |
| Planning rejected the lease (or `locate` raised) | `NEVER_FETCHED` | Nothing was issued |
| Pump raised, including at `begin_fetch` | `ABANDONED` | Writes may still land |
| Every layer loaded | `FINISHED` | Reclaimable at once |

A `begin_fetch` refusal counts as abandoned too, because the transport may
have issued part of the plan before refusing it. Quarantining a window by
mistake costs one fetch timeout; reusing a window that is still being
written corrupts the next request. If `release()` itself fails on an error
path, the failure is logged and the fetch's error is raised in its place.

The builder validates the lease rather than trusting it. An offset past the
window, or two objects sharing bytes, would make RDMA writes land on the
wrong data with no error, so both raise `ValueError` before anything is
issued.

**Who begins the fetch (decided 2026-09-25, M4).** The pump does: `run(plan)`
calls `source.begin_fetch(plan)` and owns the generation from then on.
Retrieve only builds the plan and runs the pump over an
`AerospikeLayerArrivalSource`, which it gets from the storage manager through
a `layer_arrival_source()` accessor rather than from the L2 adapter directly.
The storage-manager `begin_pipelined_fetch` / `is_pipelined_layer_ready` pair
is retired (or becomes internal to the source): it returned `0` for
"unsupported" and a boolean for readiness, the two shapes the contract
replaced. The pump blocks until every layer is loaded, so it runs on its own
worker thread, never on the request handler. The placer runs before the pump
and can refuse too (see "Window ownership"), so one handler covers both.

On fallback the whole-object load goes into **fresh objects in general L1**,
never into the window objects of the failed fetch. An abandoned fetch's
window is quarantined because RDMA writes may still land in it; a fallback
written there could be overwritten after it completed. The failed fetch's
window objects are deleted and the window released as abandoned. Until Track B's
loader exists, the path sits behind a flag and is tested with
`RecordingLayerLoadSink`.

`LMCacheDrivenTransferModule.fetch_model(model, world_size)` returns the
registered `FetchModel`. It is released with the model's last registration,
and it is absent (`KeyError`) for a layout that cannot be planned; such a
request loads whole objects. `retrieve` and `request_cache_keys` share
`first_in_window_chunk`, so the two cannot disagree about which chunks a
sliding-window group reads.

### Window ownership (decided 2026-09-25, M2)

Every destination in a plan lies inside the request's **leased RDMA
window**, and Track A owns the lease. Offsets are measured from the start of
the registration (the L1 slab), not of the window: a node allows few
registered regions, so one registration covers every window and a window is
a range `[window_start, window_start + window_bytes)` inside it (Track A's
P1). The whole L1 region is *not* one window:
that reverses the bounded-window decision in
[aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md#registration-scope-bounded-windows),
because a late write from an abandoned fetch would land in whatever
unrelated object had since been allocated at that address.

The windows (`RdmaWindowPlan`) are already ranges of the L1 slab, registered
once at startup. What is decided:

1. **The window ranges are reserved.** Today the L1 allocator does not know
   about them and can place ordinary objects inside a window, so the
   blast-radius guarantee does not yet hold. The window ranges become a
   reserved region of the allocator, used only by pipelined retrieves.
2. **Data stays where it lands.** A window is already L1 memory, so a
   pipelined retrieve allocates its L1 objects inside its leased window and
   nothing is copied. The window returns to the pool when those objects are
   freed. The cost: `window_count` bounds how many pipelined retrieves' objects
   are held at once, not only how many fetches are in flight. If that proves
   too tight, copy out after the pump finishes -- off the TTFT path, since
   every layer has reached the GPU by then.
3. **A window released by an abandon is quarantined** until the fetch
   timeout has passed. Re-leasing it at once would let a late write from the
   abandoned fetch overwrite the next request's buffer; the generation check
   stops LMCache *counting* that write but not the NIC performing it. The L1
   write-lock TTL already bounds how late a write can be.
4. **Track A builds the lease API** and the production `ChunkPlacer` on top
   of it. The interface is defined in `request_fetch.py`:
   `ChunkPlacer.lease(objects) -> WindowLease`, where `WindowLease` has
   `window_start()`, `window_bytes()`, `locate(chunk, group) ->
   ChunkLocation` (a registration offset inside the window) and
   `release(LeaseOutcome)`. The lease is per request because the window is;
   `locate()` is per object. `build_request_fetch` refuses any location
   outside `[window_start, window_start + window_bytes)`.

**Where the code stands.** None of the above exists yet. `RdmaWindowPlan`
carves `window_count × window_bytes` from the start of the slab, but the L1
memory manager does not know about it, so ordinary L1 objects can be
allocated in those bytes. Only window 0 is published to the nodes, there is
no lease API, and the pipelined session runs one fetch at a time with a single
`window_bytes`. Until the reservation lands, the blast-radius argument above
does not hold.

`window_count > 1` will let that many retrieves' data stay *resident*; it will
not let fetches run *concurrently*. The session holds one active request and
all fetches share one receive queue, so `lease()` refuses while another fetch
is in flight, and a concurrent retrieve takes the whole-object path.
Concurrent fetches are a later Track A item: one session per leased window,
with immediates routed to a session by generation, which then has to be
unique across sessions.

**The allocator change (Track A).** A small PR reviewed by the `l1_manager`
maintainers: when RDMA is enabled, the L1 memory manager builds its general
allocator over the slab *minus* the window range, and the window pool gets
its own small sub-allocator. Nothing else in `l1_manager` changes. Track A
owns it because only the transport's safety depends on it.

**Window size: one request, one window, checked against the model.** The 8 MiB
default is a test-harness constant. KV per token is `2 × layers × kv_heads ×
head_dim × bytes`, so at fp16:

| Model | KV per token | One 256-token chunk | 4k tokens |
|---|---|---|---|
| Llama-2-7B (32 KV heads) | 512 KiB | 128 MiB | 2 GiB |
| Llama-3-8B / Mistral-7B (8 KV heads) | 128 KiB | 32 MiB | 512 MiB |

Not one chunk fits in 8 MiB. But `window_bytes` cannot be derived from the
model: the windows are carved out when L1 is built, and the KV layout only
arrives later, when a worker registers its KV cache. So `window_bytes` stays
in config, and the connector checks it at registration instead.
`FetchModel.request_bytes(num_chunks, align_bytes)` gives the bytes one
retrieve of `num_chunks` chunks places in its window. It counts only the
chunks a sliding-window group reads, skips aux groups, and rounds each
object up to `align_bytes`. At registration the connector compares
`request_bytes(max_pipelined_chunks, slab_alignment)` with `window_bytes`
and, if the window is too small, reports the size needed. Other limits stay comfortable: a
512 MiB window of 960 KiB records is ~550 slots, far under 65536 and
Soft-RoCE's receive queue (EFA's limit is still A7). A request larger than a
window is not spread over several windows; the placer refuses it and retrieve
falls back to a whole-object load. Spanning windows needs no wire change but
does need every window registered with every node, per-window regions in the
node registry, records that never straddle a boundary and a multi-window
lease, so it is deferred until the memory budget forces it.

**The cost is memory.** Windows are carved out of general L1: 4 × 512 MiB is
2 GiB. That is why data stays in place (point 2) rather than being copied out.

**Reclaiming a full window (decided with Track A).** With data staying in
place, a window is only free once every object in it is gone, so windows
would fill with long-lived cache entries and starve the pipelined path.
`lease()` therefore reclaims a window by evicting its objects. That is safe
because they are clean copies that can be read again from Aerospike; if
Aerospike has expired them, it is an ordinary miss and the KV is recomputed.
A delete is metadata only, so reclaiming inside `lease()` adds no network
time. The rules:

1. **Whole window or nothing.** Evicting half a window discards entries and
   frees nothing. "No object in this window is pinned" and the deletes are one
   step under the L1 lock. `L1Manager.delete` returns `KEY_IS_LOCKED` per key
   after earlier keys are already gone, so the allocator PR adds a narrow
   all-or-nothing delete ("delete these keys only if none is locked").
2. **Least recently used unpinned window.** A pin is a read lock from a load
   in progress, a read reservation from a lookup awaiting its retrieve, or the
   write lock of a fetch still running. If every window is pinned or
   quarantined, `lease()` refuses immediately rather than waiting.
3. **Through L1's normal delete path**, so the usual cache events fire and
   the key directory and coordinator stop listing the evicted keys.
4. **The window pool's only eviction policy.** The windows sit outside the
   general allocator, so memory-pressure eviction never sees them.
5. **Only a cleanly finished fetch leaves its window reclaimable at once.** If
   any slot neither landed nor was declined, the fetch counts as abandoned and
   the window is quarantined. `WindowLease.release(LeaseOutcome)` states this
   rule rather than leaving it to the placer to infer. The pump finishes a fetch only after
   every layer is resident, so a pump-finished fetch is clean by
   construction; every other exit abandons.

Copy-out into general L1 after the pump finishes stays the fallback if
reclaim turns out to evict too often.

**What the placer raises.** A request too big for any window raises
`PlanTooLargeError`: splitting it into smaller requests would work. No window
free (all pinned, quarantined, or a fetch already in flight) raises a plain
`LayerwiseContractError`: splitting would not help, so the caller falls back.

The `ChunkPlacer` is the only stand-in (`tests/v1/layerwise/placers.py`,
which packs a request's objects into one window and records each release).
`tests/v1/layerwise/test_request_fetch.py` drives the builder from a
vLLM-shaped request (a hybrid model with a full-attention group, a two-chunk
window group and an aux group, and a prompt that is not chunk-aligned), with
keys from the production hasher. `test_pipelined_retrieve.py` runs
`run_pipelined_retrieve` with the real pump through every exit in the table
above. The real-server test stores those objects and reads every slot back
by its key.

## 8. Invariants that are not negotiable

Each of these was a real bug. Losing one reintroduces it.

1. Per-layer launches follow `LayerwiseSchedule` order, global layer-major.
2. The per-ordinal CUDA event is recorded **before** the progress watermark is
   bumped. A test asserts this.
3. CUDA IPC events are created and recorded by the daemon, and only imported
   and waited on by the worker. Never record an imported event.
4. The worker holds the `SharedMemory` object for the whole lifetime of the
   progress record and closes and unlinks it on teardown. Dropping the handle
   early caused a use-after-free of the memoryview mid-retrieve.
5. Per-batch setup in the layerwise H2D path stays hoisted: once per batch, not
   once per layer per batch.
6. Slot indices are request-scoped and unique across the whole request, not
   per-node. The immediate encodes `(generation << 16) | slot`, giving 65536
   slots and 16 bits of generation.

   A slot index is carried positionally: it is the slot's position in
   `LayerFetchPlan.slots`. Nothing stores it as a field, so **the order of
   that tuple is load-bearing** -- a producer must be deterministic and a
   consumer must never reorder. Layer order is exposed separately by
   `layer_ids()`, so no consumer needs to infer it from slot order. The
   alternative, an explicit `slot_index` field, was rejected because it makes
   the same fact representable twice and therefore representable
   inconsistently. `LayerFetchPlan` enforces the ceiling at construction as
   `MAX_SLOTS_PER_REQUEST`, so a hand-built plan is held to it as well as
   planner output. It must stay equal to `kMaxSlotsPerRequest` in
   `csrc/storage_backends/aerospike/layer_pipeline.h`.
7. A node accepts at most `max_sinks` sinks per command, advertised in the
   `kv-sink-register` reply and defaulting to 256. The transport chunks
   commands to fit; the planner does not know the cap and does not need to.
   The server hard-refuses more, so an unchunked command fails the whole fetch.
8. A slot's record is looked up per `(chunk_id, layer_id, plane, piece)`, never
   per chunk. Reusing one chunk's record across its slots is not a type error
   and not a coverage gap -- the transport fetches a record that really exists
   into an address that is really in the window, and the model reads another
   piece's bytes. `SlotPlacement` therefore carries `plane` and `piece`, so a
   plan can be checked against the records it names.
9. `SlotPlacement.node_index` indexes `LayerFetchPlan.node_names`, which the
   plan carries so the transport can resolve a slot to a node without holding
   Track C's `PlanRequest`. Both the plan and the request reject an index
   outside their node list; an out-of-range index would otherwise address the
   fetch to whichever node happened to be at that position.

## 9. Known unknowns

These are open questions, not tasks anyone has been assigned to wish away.

**Does EFA consume a receive work request per `RDMA_WRITE_WITH_IMM`?**
If it does, the client must size its receive queue to the number of immediates
the server will send, and on RC with `rnr_retry = 7` a shortfall is infinite
retry -- a permanently wedged region rather than an error. The client can size
from its own plan, since it knows how many slots it asked for, and
`notification_depth.cpp` now clamps against the device's `max_qp_wr`. What is
not known is whether EFA needs this at all:
`EFADV_DEVICE_ATTR_CAPS_UNSOLICITED_WRITE_RECV` exists precisely to avoid
consuming a receive buffer, but it is absent from the `efadv.h` available here.
A small `ibv_wr_rdma_write_imm` loopback test on a real EFA instance settles
it. Track A owns this.

**What is the actual TTFT saving?**
Unmeasured. Track B can produce a first number without any RDMA work by
driving the load path from a fake source. Nobody should assume the answer.

**Does polling scale?**
The pump polls per layer. At 60+ layers and a 100 microsecond interval this is
fine, but it has not been measured against a real fetch. If it costs, the
contract can grow a blocking wait without changing its shape.
