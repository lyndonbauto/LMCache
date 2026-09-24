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
  generally spread across nodes. The native session currently binds a whole
  chunk to one node (`ChunkNodeBinding`), and `pipelined_fetch_arguments`
  refuses a plan that would need otherwise.
- **Destination offsets.** Chosen by the L1 allocator when it places a
  chunk's object in the registered window, so they are an input to planning
  rather than something planning derives.

Note the record cap `RecordKeys` is built with must be the cap the object
was *written* under, not today's. It decides how many records exist, so a
cap that changed between write and read renames every record.

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
   inconsistently. `FetchPlanner.plan` enforces the ceiling as
   `MAX_SLOTS_PER_REQUEST`, which must stay equal to `kMaxSlotsPerRequest` in
   `csrc/storage_backends/aerospike/layer_pipeline.h`.
7. A node accepts at most `max_sinks` sinks per command, advertised in the
   `kv-sink-register` reply and defaulting to 256. Commands are chunked to fit.
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
