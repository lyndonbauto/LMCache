# Aerospike RDMA reception into L1

Status: **reworked onto kv-sink batch reads, not yet run against a server.**
LMCache now reads layerwise fetches through the kv-sink fork of the Aerospike
C client (`sriram/kv-sink-batch-prio` in `aerospike-client-c-kvsink`) against
the matching server branch (`sriram/kv-sink-batch-prio` in
`aerospike-server`). The client owns the RDMA endpoint; LMCache registers its
L1 windows once as a sink and issues ordinary batch reads whose rows name a
destination in them. The bookkeeping (`SinkFetchTable`) and every Python
layer above it are covered without a device. The end-to-end suite (A8) has
been updated for this protocol but has not been re-run yet; see
[What is proven](#what-is-proven-and-what-is-not).

This document covers the kv-sink protocol as LMCache uses it, the
registration-scope decision, the L1 invariants that keep RDMA writes safe,
the opt-in build, and how to reproduce a test setup.

## Why

In MP mode an L2 adapter fetches opaque bytes from a remote store into a
caller-provided pinned host buffer ("L1"), which is then copied to GPU paged KV
blocks. Without RDMA those bytes travel Aerospike server → Aerospike client
library buffer → L1, so every byte is copied once more than it needs to be. If
the Aerospike server can RDMA-write straight into L1, that copy disappears and
the client library leaves the data path entirely.

**Be precise about what that is worth, because the obvious answer is wrong.**
The M0 baseline measured the existing TCP path at 98% of 100 GbE line rate, so
there is essentially no bulk-throughput headroom for RDMA to capture. The case
for RDMA is the other two axes:

- **CPU offload.** The copy that disappears is CPU work on the serving host,
  competing with the very process that needs those cycles for prefill.
- **Per-operation latency.** Scattered small reads pay the round-trip and the
  library's per-record cost repeatedly. The M0 sweep saw a single object reach
  only 1.88 GB/s against the 12.2 GB/s NIC ceiling — that gap, not the ceiling,
  is the target.

A gate written against aggregate GB/s will therefore show RDMA achieving
nothing while being perfectly correct. See AIE-90.

The layerwise loader adds a third reason: a fetch that reports each layer as
it lands lets vLLM start attention on layer 0 while later layers are still on
the wire.

## Protocol

Two client calls, both in the kv-sink client (`aerospike/as_sink.h`,
`aerospike/aerospike_batch.h`):

```c
// Once, at startup: register [buf, buf + size) with every node.
as_sink_config cfg;
as_sink_config_init(&cfg);
cfg.transport = "rc";          // or "srd"; LMCache never uses "local"
cfg.device = "rxe0";           // NULL: first device
cfg.gid_index = 1;
cfg.queue_pairs = 8;           // RC: queue pairs per node (1-16)
aerospike_sink_create(&as, &err, l1_base, window_count * window_bytes,
                      &cfg, &sink);

// Per layer: a batch read whose rows carry a destination.
as_batch_read_record* row = as_batch_read_reserve(records);
as_key_init_str(&row->key, ns, set, "<cache key>|s|<wid>|3");
row->read_all_bins = true;
row->sink = sink;
row->sink_offset = slab_offset;   // relative to l1_base
row->sink_length = record_bytes;  // must equal the value's size
row->sink_priority = layer_ordinal;
row->result = AEROSPIKE_NO_RESPONSE;
aerospike_batch_read(&as, &err, &policy, records);
```

Four properties drive the design:

1. **The client chooses the destination offsets.** LMCache's plan picks every
   `sink_offset`; the server knows nothing about transformer layers.
2. **A row's result is its completion.** The server RDMA-writes the value into
   the sink and answers the row only once that write has completed. So
   `AEROSPIKE_OK` on a row means its bytes are in L1, and a batch per layer
   gives a per-layer signal with no immediates, no receive queue, and no
   completion polling on the client.
3. **Rows are routed like any batch read.** The client sends each row to its
   record's partition master, so a cluster of any size is served, and the
   node a plan names is nominal.
4. **Priority orders placement per sink.** Each node queues sink writes per
   region by `sink_priority` (lower first) and round-robins across regions.
   LMCache sets it to the layer's ordinal in the plan, so every layer is in
   flight at once but layer 0 is placed first.

The sink wire format (field 46 on each batch row: region, offset, length,
priority) and the per-node registration (`kv-sink-register`,
`kv-sink-touch`, `kv-sink-deregister`) are the client's business; LMCache
never builds an info command.

**Failure modes a row can report**, all of which fail the slot and make its
layer unservable:

| Row result | Meaning |
|---|---|
| `AEROSPIKE_ERR_RECORD_NOT_FOUND` | The record is gone (evicted, expired, never written). |
| `AEROSPIKE_ERR_INCOMPATIBLE_TYPE` | Not a single-blob-bin record of at most 2 MiB (`KV_SINK_MAX_VALUE_SZ`); see [Records a sink can read](#records-a-sink-can-read). |
| a length mismatch error | `sink_length` differs from the stored value's size; nothing is written. |
| `AEROSPIKE_ERR_TIMEOUT` / `AEROSPIKE_NO_RESPONSE` | The batch did not answer the row by `fetch_timeout_seconds`, or the server dropped the queued write at that deadline. |
| `AEROSPIKE_ERR_UNSUPPORTED_FEATURE` | Strong-consistency namespace or a filter expression; the server refuses sink reads there. |
| `AEROSPIKE_ERR_SINK_UNKNOWN_REGION` (220) | That node no longer knows the sink; see [Sink refresh](#sink-refresh). |

## Registration scope: bounded windows

**Decision: a pool of bounded windows, pre-registered at init, one leased per
in-flight request.** Implemented as `RdmaWindowPlan` in
`lmcache/v1/distributed/l2_adapters/rdma_registration.py` and
`AerospikeSinkFetchDriver::initialize` in
`csrc/storage_backends/aerospike/connector_sink_fetch.cpp`.

`get_l1_memory_desc()` describes the *entire* L1 slab, so the obvious
implementation registers it once. That was rejected. A slab-wide registration
lets any Aerospike node write anywhere in the KV cache, so a bad destination
offset, or a late write from a request that was already abandoned, silently
overwrites an unrelated request's KV. Corrupted KV **does not crash** — it
produces confidently wrong tokens, which is the hardest possible failure to
attribute and would likely be blamed on the model.

Bounded windows do not make misdirected writes impossible; they bound the blast
radius to one request's own buffer, which keeps the failure attributable. The
cost is a fixed `window_bytes` that must be large enough for the largest single
fetch, and `window_count` bounds how many retrieves' data can stay resident.

**The window range is one sink, and the client keeps each request in its
window.** `initialize` registers `[0, window_count × window_bytes)` of the
slab as one sink, so every node gets that range once. A sink per window would
cost each node a registration and, on RC, a queue pair per window.

The bound moves to the client and to L1:

- L1 allocates nothing but window objects in the range, and the server
  bounds-checks every row against the sink's size, so no write reaches
  general L1.
- `SinkFetchTable::begin` refuses a request whose slots don't all fall inside
  one window: the window of its first slot. Plan offsets are slab offsets,
  which equal `memory_obj.meta.address`.
- A late write from an abandoned fetch still targets that fetch's own window,
  which the leaser quarantines.

What's lost against a sink per window is protection against a *server* bug
that writes outside the offsets it was sent.

Registration happens **exactly once, at initialization**: `ibv_reg_mr` is
expensive enough to erase the benefit of the RDMA path.

## The write-lock TTL invariant (enforced at startup)

**An RDMA fetch is only safe because the destination L1 object is write-locked
for the whole round trip.** From `lmcache/v1/distributed/l1_manager.py`:

1. The load path creates the `L1ObjectState` and immediately calls
   `write_lock.lock()` — the object is write-locked from the moment it exists.
2. While write-locked it **cannot be read**: `available_for_read()` is
   `not write_lock.is_locked()`, so readers get `L1Error.KEY_NOT_READABLE`.
3. While write-locked it **cannot be evicted**: `delete` returns
   `L1Error.KEY_IS_LOCKED` unless `force`, and the bulk clear loop skips
   locked entries.
4. Release happens only after the L2 load returns, in
   `storage_controllers/prefetch_controller.py` — `finish_write` for
   `PrefetchMode.WARM`, otherwise
   `finish_write_and_reserve_read(read_locks=...)`.

So the whole Aerospike round trip, DMA included, already sits inside the
write-locked window. The server holding the *record* lock while it copies the
value into its staging slot is the mirror of the same guarantee at the other
end.

**But `write_lock` is a `TTLLock`, not a plain lock.** It is constructed as
`TTLLock(self._write_ttl_seconds)`, and `write_ttl_seconds` defaults to **600s**
(`lmcache/v1/distributed/config.py`), operator-settable via
`--l1-write-ttl-seconds`. If a fetch outlives that TTL the write lock expires
**silently** and the buffer becomes readable and evictable while a remote node
may still be writing into it. L1Manager only warns.

Hence the invariant:

> **The RDMA fetch timeout must be strictly less than `write_ttl_seconds`, and a
> window lease must be released or quarantined no later than write-lock
> expiry.**

This is checked **at startup**, not per fetch, by
`validate_fetch_timeout_against_write_ttl` in `rdma_registration.py`, called
from `StorageManager._build_l2_adapter` — the one place where both the adapter
config and `L1ManagerConfig` are in scope. It raises `ValueError` naming both
knobs and both values.

`rdma.fetch_timeout_seconds` defaults to 30s, comfortably under the 600s TTL.
It is also every layer batch's total and socket timeout, with no retries, so
LMCache gives up on a row no later than the quarantine assumes.

### Allocator constraint

A slab that grows or moves after `ibv_reg_mr` leaves the remote writer holding
an rkey for memory LMCache no longer owns. `L1MemoryDesc` carries a
`MemoryGrowthPolicy`: `FIXED` for `MixedMemoryAllocator`, `GROWABLE` for
`LazyMemoryAllocator`, which can expand its slab. Enabling RDMA with a
`GROWABLE` slab raises `ValueError` naming the hazard and the fix.

### The windows are reserved outside the general allocator

A window only bounds the blast radius if nothing else lives in it. So when an
adapter enables RDMA, L1 splits its slab at startup:

```text
slab offset 0                 W = window_bytes        N = window_count
|  window 0  |  window 1  | ... | window N-1 |  general L1 ...................|
|<------------ N * W, one allocator each --->|<- MixedMemoryAllocator heap -->|
```

- `normalize_storage_manager_config` copies the adapter's `RdmaWindowPlan`
  into `L1MemoryManagerConfig.rdma_window_count` / `rdma_window_bytes`, because
  L1 is built before any adapter. More than one RDMA adapter, or RDMA with a
  GDS or Device-DAX L1, raises `ValueError`.
- `MixedMemoryAllocator(reserved_prefix_bytes=N*W)` allocates the prefix once
  at construction and never frees it. The general heap keeps its whole-slab
  address space, so `meta.address` stays a slab offset for every object and
  the Nixl adapters and `gpu_ops`, which rely on that, are unaffected.
- Each window gets a `RangeMemoryAllocator` over its own range, with
  slab-absolute addresses. `L1MemoryManager.free` routes each object back to
  its allocator by data pointer.
- `get_memory_usage` reports the general pool only, and
  `L1Manager.is_key_evictable` is false for window objects, so memory-pressure
  eviction never touches the windows.
- An RDMA adapter added at runtime is refused by `validate_windows_reserved`
  unless L1 reserved exactly its plan at startup.

The caller picks the pool per reservation. General L1 is the default, so no
existing caller changes:

```python
l1.reserve_write(keys, temps, layout, mode="new", pool=L1Pool.rdma_window(i))
```

These `L1Manager` calls support the window lifecycle
([W1 and W4](../../layerwise/track-a-questions-for-track-c.md#window-lifecycle-w1-to-w4-agreed-after-the-meeting)):

| Call | Does | Used by |
|---|---|---|
| `reclaim_rdma_window(i)` | Deletes every object in window `i`, or none if any is read- or write-locked, in one step under the L1 lock. | The leaser, reclaiming a whole idle window. |
| `get_rdma_window_object_count(i)` | Counts the objects in window `i`. | The leaser, preferring an empty window to a reclaim. |
| `delete_if_none_locked(keys)` | The same all-or-nothing delete, over a caller's key list. | Callers that hold their own key list. |
| `abort_write(keys)` | Deletes write-locked keys without making them readable and without a write-finished event. Leaves other keys alone. | The fallback after a failed fetch, before it reserves fresh general-L1 objects. |

The deletes emit the same eviction events as `delete`. L1 keeps its own record
of which keys live in each window. A caller-side list could go stale: a key
deleted from a window may be re-created in general L1, and reclaiming by that
list would delete the wrong object.

### Leasing a window

`RdmaWindowLeaser` (`rdma_window_leaser.py`) hands the windows to pipelined
retrieves:

```python
lease = leaser.lease(request_bytes)   # WindowLease(window_index, base_offset, ...)
l1.reserve_write(keys, temps, layout, mode="new", pool=lease.pool())
...                                   # fetch, pump
leaser.release(lease, FetchOutcome.FINISHED)   # or ABANDONED on any other exit
```

- **Choosing a window.** An empty window first. Otherwise the window released
  longest ago whose objects are all unlocked, reclaimed with
  `reclaim_rdma_window`. Reads after the fetch don't refresh that order.
- **Quarantine.** A window released as `ABANDONED` isn't leased again for
  `fetch_timeout_seconds`. This is what makes an abandoned fetch safe: the
  server fails a queued sink write once the row's deadline (the batch's
  `total_timeout`, set to the fetch timeout) has passed, but a write already
  posted to the NIC by then still lands, shortly after LMCache gave up on it.
  `FINISHED` means every layer became resident, so the window can be reused
  at once.
- **One lease per window.** The native client runs one fetch per window (W3),
  so up to `window_count` leases are outstanding at once. A leased window is
  never a candidate, even for reclaiming. When every window is leased or
  quarantined, `lease` raises instead of queueing.
- **Refusals** follow W2. A request larger than a window raises
  `PlanTooLargeError`, since the caller can split it. No lease available
  raises `LayerwiseContractError`, and the caller falls back.

### Placing a retrieve's objects

`RdmaWindowPlacer` (`rdma_window_placer.py`) is the layerwise `ChunkPlacer`,
and the `WindowPlacement` it returns is the `WindowLease` (both protocols
are in `lmcache/v1/layerwise/request_fetch.py`). It leases a window and
reserves every object of the retrieve in it, all or nothing:

```python
# At registration, once the model's layouts are known:
placer = storage_manager.pipelined_window_placer(layouts, fetch_model,
                                                 max_pipelined_chunks)
#   layouts: {group_id: MemoryLayoutDesc} of the registered model
#   raises ValueError if one window cannot hold max_pipelined_chunks
# At retrieve, for the keys L1 does not already hold:
lease = placer.lease(objects_to_place(model, obj_keys))
lease.locate(chunk_id, group_id)   # ChunkLocation(node_name, slab offset)
...                                # build_request_fetch, pump, sink copies
lease.release(LeaseOutcome.FINISHED)       # retained objects readable, the
                                           # rest freed; window reusable
lease.release(LeaseOutcome.NEVER_FETCHED)  # abort_write; window reusable
lease.release(LeaseOutcome.ABANDONED)      # abort_write; window quarantined,
                                           # fallback uses general L1 (W4)
```

- **Built by the storage manager.** `StorageManager.pipelined_window_placer`
  builds an `RdmaWindowPlacer` over its own L1, retaining objects as the
  prefetch policy's `select_l1_retentions` decides, after running
  `check_window_holds_request`. It raises `LayerwiseContractError` when no
  adapter enables RDMA reception or none has a ready pipelined path, and
  retrieve then loads whole objects. Every placer shares the storage
  manager's one `RdmaWindowLeaser`, because the windows belong to L1.
- **Retention, and no write-back.** Fetched objects follow the prefetch
  policy, as whole-object loads do. `FINISHED` ends the writes with
  `finish_write_and_reserve_read` and then `finish_read`, never plain
  `finish_write`, which would store the object back to L2. Release only after
  the reader's copies out of the window have completed.
- **Size check.** Each object is rounded up to the L1 alignment, as the
  window allocator does, and a total over `window_bytes` raises
  `PlanTooLargeError` before anything is leased.
- **Window size at registration.** `check_window_holds_request(window_bytes,
  model, max_pipelined_chunks, align_bytes)` raises `ValueError` stating the
  size needed (L4). The native driver's own check, that one chunk fits (see
  [Sizing `window_bytes`](#sizing-window_bytes)), still decides whether the
  pipelined path is ready at all.
- **All or nothing.** If L1 refuses any key, every reservation is aborted,
  the lease is released as `FINISHED` (no fetch was issued), and it raises
  `LayerwiseContractError`.
- **Offsets are slab offsets** (`memory_obj.meta.address`), because the sink
  starts at slab offset 0 ("P1" in the
  [questions doc](../../layerwise/track-a-questions-for-track-c.md#p1-every-window-is-published-as-one-registration-decided)).
- **The node name is nominal.** `locate` returns the node the placer was built
  with, `StorageManager.pipelined_fetch_node_name()`: the first node of the
  cluster. The client routes each row by its own record's digest, so an
  object whose records are spread over several nodes is still served (N1 in
  the questions doc is resolved by the transport, not the planner).
- **The slot limit is known before the fetch.**
  `StorageManager.pipelined_max_slots_per_request()` returns the most slots
  `begin_fetch` accepts (`kMaxSlotsPerRequest`, 65 536) before it raises
  `PlanTooLargeError`, so a lookup-time eligibility check can skip the
  pipelined path up front (F4). It never returns 0: the native client's 0
  means "not ready", not "no limit".

## Pipelined fetch

`AerospikeNativeConnector::issue_pipelined_fetch_by_slots` takes the plan's
slots as given — `(node_index, record_key, dest_offset, length, layer_id)` —
and hands them to `AerospikeSinkFetchDriver::issue`. The split between the two
native classes is deliberate:

| Class | File | Owns |
|---|---|---|
| `SinkFetchTable` | `sink_fetch_table.{h,cpp}` | Generations, one fetch per window, per-layer batches, per-slot results, stale-result rejection. No I/O, so it runs in the logic harness. |
| `AerospikeSinkFetchDriver` | `connector_sink_fetch.{h,cpp}` | The sink, the batch policy, a worker pool issuing the batches, sink refresh, layout validation. |

```text
issue(slots)
  └─ table.begin(slots)            validate, pick window, allocate generation
       -> one LayerBatch per layer, in order of first appearance:
          {generation, token, layer_id, priority = ordinal, slot_indices}
  └─ queue the batches, return the generation     (no I/O on this thread)

worker (16 threads)
  └─ skip the batch if table.is_active(generation, token) is false
  └─ aerospike_batch_read(rows of that layer, sink fields set)
  └─ table.on_slot_result(generation, slot, row == AEROSPIKE_OK, token)

poll_layer -> table.is_layer_ready / unservable_layers
```

- **One batch per layer.** A layer's readiness is decided by its own batch,
  so the loader can consume layer 0 as soon as its batch returns. Batches run
  concurrently on the worker pool, and the server places them by priority.
- **Generations and tokens.** The table allocates a non-zero 16-bit
  generation per fetch, skipping any still active in another window, and a
  64-bit token per batch that never repeats. A result must name an active
  generation *and* a token of that fetch, so a batch still running after its
  fetch was abandoned can't be credited to the window's next fetch, even
  after the generation wraps.
- **Abandon.** `abandon_pipelined_fetch` drops the fetch from the table at
  once. Its queued batches are skipped; batches already sent run to their
  timeout and their results are dropped. The server may still write their
  rows, which is what the leaser's quarantine covers.
- **Settled windows end the quarantine early.** The table counts the
  batches outstanding in each window, and each worker reports how its batch
  ended. A row answered `AEROSPIKE_OK` was written before the answer; a row
  whose record or region was missing was never written. Any other result,
  above all a timeout, may still be written. `rdma_window_settled(window)`
  is true once nothing is outstanding and no batch ended with a possible
  late write, and the leaser then reuses the abandoned window at once.
  Without this, the pump's 1.5 s layer timeout would quarantine a window
  for the full 30 s `fetch_timeout_seconds` even when the slow batch
  answered a second later, and a few stalls would leave all 8 windows
  quarantined and every retrieve loading whole objects.
- **Rows are never retried** (`max_retries = 0`), except once after a sink
  refresh. A retried row could be written after LMCache gave up on it.
- **Row results are pre-set to `AEROSPIKE_NO_RESPONSE`.** Reserved rows are
  zeroed and zero is `AEROSPIKE_OK`, so a batch that fails before the client
  resets its rows must not read as landed.

### Sink refresh

A node that restarts, reclaims a sink unused for its idle timeout
(`KV_SINK_IDLE_SEC`, default 600 s), never registered it, or dropped it after
one of its RDMA writes failed, answers that node's rows with
`AEROSPIKE_ERR_SINK_UNKNOWN_REGION`. Those rows' bytes are not complete, and a
resend rewrites the same destination. The worker calls `aerospike_sink_refresh`, which re-registers only where the sink
is missing, and resends those rows once. Concurrent workers share one refresh
through an epoch counter. A row that fails with 220 again fails its slot.

### Records a sink can read

The server places only single-blob-bin records of at most 2 MiB. LMCache's
segment records (`<key>|s|<wid>|<i>`, or `<key>|s|<i>` for objects
stored before D-14; one bin `b`) qualify, and layer-aligned
writes cut them per plane under the record cap, so every slot of a sharded
object is readable. An object small enough to be stored inline in its meta
record (`<key>|m`, 8+ bins) is refused with `AEROSPIKE_ERR_INCOMPATIBLE_TYPE`,
its layer is unservable, and the retrieve falls back to a whole-object load.
With a layout registered this happens only when a whole object group fits
in one record.

The server applies read-touch to sink rows as to any read. The driver sends
them with `read_touch_ttl_percent = -1`: a sink fetch reads segments without
their meta record, and extending only the segments would leave them alive
after the meta record that indexes them expires.

### Readiness

`is_ready()` requires the sink to exist, layouts to be set, one chunk of each
layout to fit a window, and the connector not to be closed. When it is false,
`pipelined_fetch_init_error` says why, and every retrieve loads whole
objects.

### Close

`AerospikeNativeConnector::close()` calls `shutdown()` before the workers
stop, because deregistering needs the connected client:

```text
close()
  └─ shutdown()
       ├─ stop the batch workers (queued batches dropped, in-flight ones finish)
       └─ aerospike_sink_destroy(sink)      deregisters from every node
  └─ ConnectorBase::close()                 workers stop, client closes
```

## Transport-agnostic registration handle

`L1MemoryDesc` has a `registration: MemoryRegistration` field.
`MemoryRegistration` carries an opaque `handle` plus a
`MemoryRegistrationTransport` discriminator (`UNREGISTERED`, `IB_VERBS`,
`MOONCAKE`, `NIXL`), deliberately **not** named `ib_rkey`, because the same
struct is published to the Mooncake and NIXL paths. Consumers must check
`transport` before interpreting `handle`.

`UNREGISTERED_MEMORY` is an explicit sentinel rather than `Optional`/`None`,
per `docs/coding_standards.md`, so callers never branch on `None`.

## Build profile

One opt-in, **default off**, on top of the Aerospike backend:

```bash
# Build the kv-sink client into .deps/ (needs rdma-core headers, including
# infiniband/efadv.h) and point the build at it.
.deps/build_aerospike_client_kvsink.sh
source .deps/aerospike-client-c.env

BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1 \
  pip install -e . --no-build-isolation
```

The script clones `sriram588/aerospike-client-c-kvsink` at
`sriram/kv-sink-batch-prio` (override with `AEROSPIKE_CLIENT_REPO` /
`AEROSPIKE_CLIENT_REF`, or pass an existing checkout), builds it without an
event library, since the connector uses only the synchronous API, and writes
`AEROSPIKE_INCLUDE_DIR`, `AEROSPIKE_LIBRARY_DIR`,
`AEROSPIKE_EVENT_LIB=none` and `LD_LIBRARY_PATH` into the env file.

- The build profile refuses `BUILD_WITH_AEROSPIKE_RDMA=1` when no
  `aerospike/as_sink.h` is under `AEROSPIKE_INCLUDE_DIR`, naming the script.
- The extension links `ibverbs` and `efa`, because the client's verbs
  transport calls both and `libaerospike` does not declare them. The client
  compiles that transport only when `efadv.h` is present; without it only the
  same-host `local` transport exists, which LMCache refuses.
- RC and SRD are chosen at run time by `rdma.transport`, so the legacy
  `BUILD_WITH_AEROSPIKE_EFA=1` is only a synonym for the RDMA flag.
- `RDMA_CORE_INCLUDE_DIR` / `RDMA_CORE_LIBRARY_DIR` point at an out-of-tree
  rdma-core.

A default `pip install -e . --no-build-isolation` adds no `libibverbs`
dependency, compiles no sink source, defines no RDMA macro, and works against
the stock client. `L1RdmaRegistration` is still exported to Python on such a
build, but with **none of its fields bound** — which is how
`_build_native_rdma_registration` detects a non-RDMA build and raises a
`RuntimeError` naming the rebuild flag.

## Configuration

RDMA is off unless the adapter config asks for it:

```python
{
    "type": "aerospike",
    "hosts": "127.0.0.1:3000",
    "namespace": "lmcache",
    "rdma": {
        "transport": "RC",      # DISABLED (default) | RC | SRD
        "device_name": "rxe0",  # empty selects the first device
        "gid_index": 1,         # RC: the GID the node addresses us by
        "queue_pairs": 1,       # RC: queue pairs per node, 1-16 (default 1)
        "window_count": 8,      # max concurrent RDMA fetches
        "window_bytes": 8388608,
        "fetch_timeout_seconds": 30.0,  # must be < --l1-write-ttl-seconds
    },
}
```

`queue_pairs` asks the client to open that many RC queue pairs to each node;
the server spreads a fetch's writes over them round-robin. On a hardware NIC one
queue pair already reaches line rate, so leave it at 1. Soft-RoCE runs each
queue pair's work on one core at a time, which capped one sink at about
2.2 GiB/s on the MI300X test box (8.6 GiB/s raw at 8 queue pairs), so raise it
there. It needs a kv-sink client with `as_sink_config.queue_pairs` and a server
that accepts several queue pairs per region; LMCache does not build against
older clients.

The server must run the same transport, on a device configured with
`KV_SINK_RDMA_DEVICE` and, where the automatic choice is wrong,
`KV_SINK_GID_INDEX`. `gid_index` defaults to 0, which is **not** right on
Soft-RoCE bound to `lo`; see [the GID index trap](#the-gid-index-trap).

`RC` is the portable transport and is the only one Soft-RoCE supports. `SRD`
exists only on AWS EFA.

### Sizing `window_bytes`

A window must hold at least one whole chunk: one object per object group,
each rounded up to the L1 alignment. The windows are carved out of L1 at
startup, before any worker has registered its KV cache, so the size comes from
config rather than from the model. It is checked when the layout arrives: if
one chunk does not fit, the pipelined path stays off, every retrieve loads
whole objects, and the adapter logs a warning naming the size needed:

```text
aerospike: pipelined fetch unavailable, retrieves will load whole objects:
RDMA window_bytes is 8388608 but one chunk of this model needs 33554432 bytes,
so no retrieve can be pipelined; set rdma.window_bytes to at least 33554432
```

One chunk is `2 x layers x chunk_tokens x kv_heads x head_dim x dtype_bytes`
(for a standard, non-MLA attention layout):

| Model | Per 256-token chunk |
|---|---|
| Llama-3-8B (32 layers, 8 KV heads x 128, fp16) | 32 MiB |
| Llama-2-7B (32 layers, 32 KV heads x 128, fp16) | 128 MiB |

The 8 MiB default is too small for either. The windows come out of L1
(`window_count x window_bytes`), so eight 32 MiB windows take 256 MiB of the
slab away from general L1.

**TODO: size windows from the model.** An unset `window_bytes` should become
one chunk of the first model that registers. Today the windows are carved
out of L1 and registered with every node when the server starts, before any
model is known. So sizing them from the model means deferring both steps to
the first `REGISTER_KV_CACHE`, and refusing a later model whose chunk is
larger. Until then, set `window_bytes` explicitly.

### Keeping fetched chunks in L1

By default, chunks loaded from Aerospike are dropped from L1 once the GPU has
copied them, so a repeat of the same prompt fetches from Aerospike again. To
keep them, start the server with the `retain` prefetch policy:

```bash
--l2-prefetch-policy retain
```

On the pipelined path, a retained chunk stays readable inside its RDMA window
until the window is leased to a later retrieve. So `retain` serves L1 hits
for roughly the last `window_count` pipelined retrieves, not for as long as
general L1 would keep them. Chunks loaded on the whole-object path go to
general L1 and follow its normal eviction.

## Testing

### Without a device

| Test | Covers |
|---|---|
| `tests/v1/distributed/rdma/test_sink_fetch_table.py` | `SinkFetchTable`: batches per layer in plan order, readiness, unservable layers, one fetch per window, malformed plans, stale results after generation wrap. |
| `tests/v1/distributed/rdma/test_slot_plan_parity.py` | The Python planner and `slot_planner.h` produce the same slots in the same order. |
| `tests/v1/layerwise/test_arrival_source_conformance.py` and the other `aerospike_harness` users | `AerospikeLayerArrivalSource` and `NativePlanIssuer` over the real table, through the fabric-free `FabricFreeConnector` (`make -C tests/v1/distributed/rdma pyharness`). |

```bash
make -C tests/v1/distributed/rdma logic-test
pytest -xvs tests/v1/distributed/rdma tests/v1/layerwise
```

### Reproducing the Soft-RoCE test setup

Soft-RoCE (`rdma_rxe`) presents a functional RDMA device over ordinary
Ethernet or loopback, so the data path can be exercised without RDMA NICs. It
supports RC but **not** SRD, which is why RC is the portable path.

On a Windows workstation this needs a Linux VM, because `rdma_rxe` is a kernel
module and neither WSL2 nor Docker Desktop ships one that has it. See
[`rdma_testing_on_windows.md`](rdma_testing_on_windows.md). All of the
following needs **root**.

```bash
# 1. Install userspace tooling and headers.
sudo apt-get install -y rdma-core libibverbs-dev ibverbs-utils \
                        ibverbs-providers perftest

# 2. Load the software driver.
sudo modprobe rdma_rxe

# 3. Attach a device to an existing interface. Loopback works for a
#    single-host test; use a real NIC for two hosts.
sudo rdma link add rxe0 type rxe netdev lo

# 4. Verify. Both must succeed before debugging anything in LMCache.
ibv_devinfo -d rxe0          # expect PORT_ACTIVE
rdma link show

# 5. Pick the GID index -- see the trap below. On `lo` this is 1, not 0.
cat /sys/class/infiniband/rxe0/ports/1/gids/*

# 6. Prove the fabric works independently of LMCache.
ibv_rc_pingpong -d rxe0 -g 1 &   # server
ibv_rc_pingpong -d rxe0 -g 1 localhost
```

If you have Docker but not an interactive `sudo`, a privileged container can
load the module and create the link, because both act on the host kernel:

```bash
docker run --rm --privileged -v /lib/modules:/lib/modules:ro ubuntu:22.04 \
  sh -c "apt-get update -qq && apt-get install -y -qq kmod && modprobe rdma_rxe"

docker run --rm --privileged --network host ubuntu:22.04 \
  sh -c "apt-get update -qq && apt-get install -y -qq rdma-core && \
         rdma link add rxe0 type rxe netdev lo"
```

The device does not survive a reboot. Being able to run privileged containers
is equivalent to root on the host.

`ibv_reg_mr` fails if the pinned slab exceeds `RLIMIT_MEMLOCK`. Check it with
`ulimit -l`, and raise `memlock` in `/etc/security/limits.conf` if the hard
limit is too low.

### The GID index trap

**`gid_index` is not always 0**, and getting it wrong fails late: the RC
queue pair never reaches RTR (`ENETUNREACH`), so `aerospike_sink_create`
fails and pipelined fetch reports it through `pipelined_fetch_init_error`.

`rxe` derives GID 0 from the netdev's MAC as an `fe80::` link-local address.
Loopback's MAC is all zeros, so GID 0 has no route. The table on `lo`:

| Index | GID | Usable |
|---|---|---|
| 0 | `fe80::200:ff:fe00:0000` | No — link-local from an all-zero MAC |
| 1 | `::ffff:7f00:0001` (IPv4-mapped `127.0.0.1`) | **Yes** |
| 2 | `::1` | Yes |

So Soft-RoCE on `lo` wants **index 1**, on both sides: `rdma.gid_index` for
LMCache and `KV_SINK_GID_INDEX=1` for the server. SRD ignores the GID for
routing, so EFA works with the default.

### Running A8 against a real server

`tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py` runs a
layerwise retrieve through a real `StorageManager` whose Aerospike adapter
has RDMA reception on. Nothing is mocked. It checks that:

1. a clean fetch completes `PIPELINED`, every layer reaches the sink, and
   every object left in L1 holds exactly the bytes that were stored;
2. with one segment record deleted, that row fails, the retrieve falls back,
   and a real whole reload returns every other object byte-exact;
3. three client lifetimes in a row each create a sink, fetch byte-exact, and
   deregister on close.

Build the server from `sriram/kv-sink-batch-prio` and its submodules
(`bin/install-dependencies.sh`, then `make -j2`; the build reads its version
from `git describe --tags`). Run it as a normal user with a small namespace:

```text
namespace lmcache {
    replication-factor 1
    nsup-period 120
    max-record-size 1048576
    stop-writes-sys-memory-pct 100
    storage-engine memory { data-size 512M }
}
```

```bash
KV_SINK_RDMA_DEVICE=rxe0 KV_SINK_GID_INDEX=1 asd --config-file ...
sudo prlimit --pid "$(pgrep -x asd)" --memlock=unlimited:unlimited

RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 \
AEROSPIKE_TEST_PORT=3000 AEROSPIKE_TEST_NAMESPACE=lmcache \
RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  pytest tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py
```

On EFA, run with `RDMA_TRANSPORT=SRD RDMA_DEVICE=<efa device>`. Stock CE has
no kv-sink, so CI's Docker server does not run this suite.

## What is proven, and what is not

| Piece | State |
|---|---|
| Build profile, default-off | Unchanged for a default build: no libibverbs, no kv-sink client needed. The RDMA build compiles and links against the fork (Ubuntu 24.04, 2026-09-30). |
| `SinkFetchTable` | **Covered** by `sink_fetch_table_test` and, through the fabric-free module, by the layerwise conformance suite. |
| `AerospikeSinkFetchDriver` | **Proven on Soft-RoCE (RC) and EFA v2 (SRD)**: A8 passes against `sriram/kv-sink-batch-prio` (3/3 on each). Needs a client built by the script; see [client issue 11](aerospike_server_issues.md#11-libaerospikeso-is-not-linked-against-libibverbs). |
| Python layerwise path | **Unchanged and covered** device-free; only docstrings moved with the protocol. |
| Windows, leaser, placer, TTL invariant | **Implemented and unit-tested**, independent of the transport. |
| Byte-exact landing against a real server | **Proven on Soft-RoCE and on EFA v2** (`g6.8xlarge`, `us-west-2`, 2026-10-01) on this protocol. |
| Overlap: layer 0 consumed while later layers land | **Not proven.** Possible now, because each layer's batch returns on its own; A8 does not measure it. |
| Multi-node cluster | **Not run.** Allowed now, since rows are routed by partition. |

## Open questions

- **Late writes after abandon.** The server now fails a queued write past its
  row's deadline, so only writes already on the wire at the deadline can land
  late, within one network round trip. The leaser's quarantine of
  `fetch_timeout_seconds` covers that with a wide margin; it has not been
  measured under heavy queuing. A window whose batches were all answered
  skips the quarantine; only a timed-out row keeps the full one.
- **Native worker held by a slow batch.** A batch read blocks its worker
  (one of 16) for up to `fetch_timeout_seconds`, even after the pump gave
  up on the fetch. Lowering `rdma.fetch_timeout_seconds` shortens both that
  and the quarantine after a timeout; the default has not been revisited.
- **Sink ownership.** The server trusts the region id a row carries
  ([server issue 4](aerospike_server_issues.md)), so the window bound is only
  as strong as the ids are unguessable.
- **Partial registration.** `aerospike_sink_create` succeeds when at least
  one node registers. The driver logs how many did; rows for the others fail
  with 220, are retried once after a refresh, and otherwise reload whole
  objects. Not yet exercised on a multi-node cluster.

## Related

- [`aerospike_concurrent_writes.md`](aerospike_concurrent_writes.md) — D-14
  decision record: per-write segment keys and create-only metadata, and what
  that means for the pipelined fetch's record keys.
- [`aerospike_server_issues.md`](aerospike_server_issues.md) — server- and
  client-side defects and contract gaps on the kv-sink branches.
- [`layerwise_transfer_data_model.md`](layerwise_transfer_data_model.md) — how
  real KV layout maps onto the slots this document fetches.
- [`rdma_testing_on_windows.md`](rdma_testing_on_windows.md) — standing up an
  Ubuntu VM on a Windows workstation and running these suites in it.
- `lmcache/v1/distributed/l2_adapters/mooncake_store_l2_adapter.py` — the
  reference L1-registration factory this adapter mirrors.
