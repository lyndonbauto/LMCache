# Aerospike RDMA reception into L1

Status: **prototype, data path executed.** The verbs foundation, the build
profile, the adapter plumbing, the per-node registration fanout, the mock RDMA
writer, and the byte-equivalence and layer-pipelining harnesses are implemented
and passing over Soft-RoCE (`rxe0` on `lo`, GID index 1). Real RDMA writes land
in L1 byte-identically, and a layer is provably consumable while later layers
are still absent. What remains unproven is EFA/SRD and a real Aerospike server;
see [What is not proven yet](#what-is-not-proven-yet).

This document covers the `kv-sink` wire protocol, the handshake ordering, the
opt-in build profile, the registration-scope decision, and how to reproduce a
Soft-RoCE test environment from scratch.

## Why

In MP mode an L2 adapter fetches opaque bytes from a remote store into a
caller-provided pinned host buffer ("L1"), which is then copied to GPU paged KV
blocks. Today those bytes travel Aerospike server → Aerospike client library
buffer → L1, so every byte is copied once more than it needs to be. If the
Aerospike server can RDMA-WRITE straight into L1, that copy disappears and the
client library leaves the data path entirely.

**Be precise about what that is worth, because the obvious answer is wrong.**
The M0 baseline measured the existing TCP path at 98% of 100 GbE line rate, so
there is essentially no bulk-throughput headroom for RDMA to capture. The case
for RDMA is the other two axes:

- **CPU offload.** The copy that disappears is CPU work on the serving host,
  competing with the very process that needs those cycles for prefill.
- **Per-operation latency.** Scattered small reads pay the round-trip and the
  library's per-record cost repeatedly. The M0 sweep saw a single object reach
  only 1.88 GB/s against the 12.2 GB/s NIC ceiling — that gap, not the ceiling,
  is the target. CacheBlend's sparse leg is the extreme case and is the
  benchmark most likely to show RDMA's value.

A gate written against aggregate GB/s will therefore show RDMA achieving
nothing while being perfectly correct. See AIE-90.

## Protocol

The control plane is an **Aerospike info command**, not the batch-read path.
Both operations are `asinfo` commands with `key=value;`-delimited requests and
replies. `csrc/storage_backends/aerospike/connector.cpp` already speaks this
idiom in `discover_record_cap()`, and the RDMA path follows it.

### Register (once, per node)

```text
kv-sink-register:transport=verbs;gid=<32 hex>;qpn=<u32>;psn=<u32>;
                 rkey=<u32>;addr=<u64>;size=<usize>
```

Reply:

```text
region=4;transport=verbs;qp=srd;qpn=49152;psn=..;gid=..;max_sinks=256
```

`max_sinks` is the maximum number of sinks this node accepts in one
`kv-sink-fetch-pipelined` command. It is **per node**: a cluster mid-upgrade
may advertise different values, and the client chunks each node's fanout
against that node's own limit, never a cluster-wide minimum. When the token is
absent — servers built before the field existed — the client assumes
**256**, the historical fixed parse buffer on the server, rather than treating
omission as unlimited. An unlimited default would rebuild the single oversized
command every node already rejects.

The reply hands back a `region` handle **scoped to the node that answered**, so
registration is inherently per-node. `aerospike_info_any()` sends to an
arbitrary node and is therefore the wrong call here: the code must fan out with
the node-specific info call and hold **one region handle per node**. The server
PoC is single-node, but this is exactly the seam where the design becomes a
cluster, and retrofitting it later is painful.

`region` is connection-lifetime state. It must be invalidated when the slab is
re-registered or a node restarts; a stale region handle points at memory the
server believes it may still write.

### Fetch (hot path)

```text
kv-sink-fetch:namespace=<ns>;region=<id>;sinks=<digest>@<off>:<len>,...
```

Reply:

```text
n=16;ok=16;bytes=16777216;in-place=16;results=ok,ok,...
```

Four properties of this protocol drive the whole design:

1. **The client chooses the destination offsets.** `<off>` is picked by
   LMCache and never by the server. The server needs to know nothing about
   transformer layers.
2. **The info reply *is* the completion.** The server polls its own send
   completion queue until every RDMA write has landed, and only then replies.
   LMCache posts no work request and polls no CQ. The operation is synchronous
   and all-or-nothing.
3. **LMCache is a pure target.** It never calls `ibv_post_send`.
4. **There is no per-layer signal**, which is why layer-by-layer pipelining
   cannot be built on this protocol as it stands.

## Handshake ordering

The ordering is forced by the protocol and is easy to get wrong, because the
register command must carry our `qpn`/`psn` while the peer's `gid`/`qpn` only
arrive in its reply:

```text
1. ibv_open_device / ibv_alloc_pd / ibv_query_gid      -> our gid
2. ibv_reg_mr over each L1 window (LOCAL_WRITE|REMOTE_WRITE) -> our rkeys
3. ibv_create_qp (RC) or efadv_create_qp_ex (SRD)      -> our qpn
   ibv_modify_qp -> INIT                                  (qkey on SRD)
4. send "kv-sink-register" with gid, qpn, psn, rkey, addr, size
5. parse reply -> region id, peer gid, peer qpn, peer psn
6. ibv_modify_qp -> RTR, then -> RTS
7. ibv_create_ah for the server peer                   <-- do not skip
```

Step 7 looks like dead code: LMCache only ever receives, so why build an
address handle for the sender? Because the peer relationship is bidirectional
at the device level. Without an AH for the server, **the server's write fails
with `UNKNOWN_PEER`** — and that error surfaces on the *server*, which makes it
close to invisible from the receiving side. This is called out explicitly in
`rdma_context.cpp` for the same reason.

`RdmaContext` splits `create_queue_pair()` (steps 3) from `connect_peer()`
(steps 6–7) precisely so the register command can be sent in between.

## Registration scope: bounded windows

**Decision: a pool of bounded memory-region windows, pre-registered at init,
one leased per in-flight request.** Implemented as `RdmaWindowPlan` in
`lmcache/v1/distributed/l2_adapters/rdma_registration.py` and
`RdmaContext::register_l1` in `csrc/storage_backends/aerospike/rdma_context.cpp`.

`get_l1_memory_desc()` describes the *entire* L1 slab, so the obvious
implementation registers it once and publishes one slab-wide rkey. That was
rejected. A slab-wide rkey lets any Aerospike node write anywhere in the KV
cache, so a bad destination offset, or a late write from a request that was
already abandoned, silently overwrites an unrelated request's KV. Corrupted KV
**does not crash** — it produces confidently wrong tokens, which is the hardest
possible failure to attribute and would likely be blamed on the model.

Bounded windows do not make misdirected writes impossible; they bound the blast
radius to one request's own buffer, which keeps the failure attributable. The
cost is a fixed `window_bytes` that must be large enough for the largest single
fetch, and `window_count` bounds how many retrieves' data can stay resident.

**The window range is one registration, and the client keeps each request in
its window.** An Aerospike server holds one `(rkey, addr, size)` per
`kv-sink-register`, and each registration costs it a queue pair. It also
allows only 16 registrations in total, across all clients (`MAX_REGIONS` in
`as/src/base/kv_sink.c` on the server branch). A registration per window would
use half of a node's 16 with 8 windows. So `register_l1` registers
`[0, window_count × window_bytes)` of the slab as one memory region, and every
node gets that range once.

The bound moves to the client and to L1:

- L1 allocates nothing but window objects in the range, so the server's own
  bounds check keeps every write out of general L1.
- `PipelinedFetchSession` refuses a request whose slots don't all fall inside
  one window: the window of its first slot. Plan offsets are slab offsets,
  which equal `memory_obj.meta.address`.
- A late write from an abandoned fetch still targets that fetch's own window,
  which the leaser quarantines.

Compared with a registration per window, what's lost is protection against a
*server* bug that writes outside the offsets it was sent. A per-window server
check (`kv-sink-add-window` plus `window=<i>` on fetch) can restore that
without a queue pair per window.

Either way, registration happens **exactly once, at initialization**.
`ibv_reg_mr` is expensive enough to erase the entire benefit of the RDMA path,
so `register_l1()` refuses a second call rather than silently re-registering
and invalidating rkeys already published to nodes.

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
write-locked window. No new lock and no separate window lifetime are needed for
the non-pipelined path. Sriram holding the *record* lock across the DMA is the
mirror of the same guarantee at the other end.

**But `write_lock` is a `TTLLock`, not a plain lock.** It is constructed as
`TTLLock(self._write_ttl_seconds)`, and `write_ttl_seconds` defaults to **600s**
(`lmcache/v1/distributed/config.py`), operator-settable via
`--l1-write-ttl-seconds`. If a fetch outlives that TTL the write lock expires
**silently** and the buffer becomes readable and evictable while a remote node
may still be writing into it. L1Manager only warns — *"potential inconsistent
data might be read"* / *"potential inconsistent data might be written"*.

Hence the invariant:

> **The RDMA fetch timeout must be strictly less than `write_ttl_seconds`, and a
> window lease must be released or generation-bumped no later than write-lock
> expiry.**

This is checked **at startup**, not per fetch, by
`validate_fetch_timeout_against_write_ttl` in `rdma_registration.py`, called
from `StorageManager._build_l2_adapter` — the one place where both the adapter
config and `L1ManagerConfig` are in scope. It raises `ValueError` naming both
knobs and both values. Two config values in two different files having to agree
is exactly the kind of footgun that should fail loudly on boot.

`rdma.fetch_timeout_seconds` defaults to 30s, comfortably under the 600s TTL.

### Allocator constraint

A slab that grows or moves after `ibv_reg_mr` leaves the remote writer holding
an rkey for memory LMCache no longer owns. Rather than have the adapter
introspect allocator internals, `L1MemoryDesc` now carries a
`MemoryGrowthPolicy`: `FIXED` for `MixedMemoryAllocator`, `GROWABLE` for
`LazyMemoryAllocator`, which can expand its slab. Enabling RDMA with a
`GROWABLE` slab raises `ValueError` naming the hazard and the fix.

This replaces the standing `TODO(ApostaC)` in `l1_memory_manager.py` with an
enforced contract instead of an untested assumption.

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
  `fetch_timeout_seconds`, because writes already on the wire can still land.
  `FINISHED` means every layer became resident, so the window can be reused
  at once.
- **One lease at a time.** The native session runs one fetch at a time (W3),
  so a second `lease` raises instead of queueing.
- **Refusals** follow W2. A request larger than a window raises
  `PlanTooLargeError`, since the caller can split it. No lease available
  raises `LayerwiseContractError`, and the caller falls back.

### Placing a retrieve's objects

`RdmaWindowPlacer` (`rdma_window_placer.py`) is the destination half of the
layerwise `ChunkPlacer`. It leases a window and reserves every object of the
retrieve in it, all or nothing:

```python
placement = placer.place(
    [ObjectToPlace(chunk_id, group_id, key), ...],
    layouts,                                      # {group_id: MemoryLayoutDesc}
)
placement.dest_offset(chunk_id, group_id)         # slab offset for the plan
...                                               # fetch, pump
placement.complete()   # finish_write + release FINISHED
# or
placement.abandon()    # abort_write + release ABANDONED (quarantine), then
                       # the fallback reserves fresh objects in general L1
```

- **Size check.** Each object is rounded up to the L1 alignment, as the
  window allocator does, and a total over `window_bytes` raises
  `PlanTooLargeError` before anything is leased.
- **All or nothing.** If L1 refuses any key, for example because it's
  already cached, every reservation is aborted and the lease is released
  as `FINISHED`, since no fetch was issued. It then raises
  `LayerwiseContractError`.
- **Offsets are slab offsets** (`memory_obj.meta.address`), because every
  window is published as one registration starting at slab offset 0. See
  "P1" in the
  [questions doc](../../layerwise/track-a-questions-for-track-c.md#p1-every-window-is-published-as-one-registration-decided).

The node half isn't here. An object's records are spread over the nodes by
their own digests, so the node is chosen per record in the planner (N1 in
the questions doc).

Every window is reachable: each node's `kv-sink-register` publishes the whole
window range, and the session keeps each request inside its window. See
[Registration scope](#registration-scope-bounded-windows).

## Transport-agnostic registration handle

`L1MemoryDesc` gained a `registration: MemoryRegistration` field.
`MemoryRegistration` carries an opaque `handle` plus a
`MemoryRegistrationTransport` discriminator (`UNREGISTERED`, `IB_VERBS`,
`MOONCAKE`, `NIXL`), deliberately **not** named `ib_rkey`, because the same
struct is published to the Mooncake and NIXL paths. Consumers must check
`transport` before interpreting `handle`.

`UNREGISTERED_MEMORY` is an explicit sentinel rather than `Optional`/`None`,
per `docs/coding_standards.md`, so callers never branch on `None`.

Note the direction of travel: LMCache registers memory **locally** and
publishes the resulting rkey **outward** to the Aerospike server. This is the
opposite of the Mooncake/NIXL flow, where LMCache hands a base and size to a
transfer engine that does its own registration.

## Build profile

Two independent opt-ins, both **default off**, modelled on the Mooncake gating
in `setup_extensions/storage_backend_profiles/`:

```bash
# Portable RC path. Needs rdma-core / libibverbs-dev headers.
BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1 \
  pip install -e . --no-build-isolation

# Additionally compile the EFA/SRD path (AWS EFA only; needs libefa).
BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_EFA=1 \
  pip install -e . --no-build-isolation
```

`BUILD_WITH_AEROSPIKE_EFA=1` implies RDMA. Optional overrides
`RDMA_CORE_INCLUDE_DIR` / `RDMA_CORE_LIBRARY_DIR` point at an out-of-tree
rdma-core.

A default `pip install -e . --no-build-isolation` adds no `libibverbs`
dependency, compiles no verbs source, and defines no RDMA macro, so a machine
with no RDMA hardware is unaffected. `L1RdmaRegistration` is still exported to
Python on such a build, but with **none of its fields bound** — which is how
`_build_native_rdma_registration` detects a non-RDMA build and raises a
`RuntimeError` naming the rebuild flag, instead of failing with an opaque
`ImportError`.

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
        "gid_index": 0,
        "window_count": 8,      # max concurrent RDMA fetches
        "window_bytes": 8388608,
        "fetch_timeout_seconds": 30.0,  # must be < --l1-write-ttl-seconds
    },
}
```

`gid_index` defaults to 0, which is correct on EFA but **not** on Soft-RoCE
bound to `lo`; see [the GID index trap](#the-gid-index-trap).

`RC` is the portable transport and is the only one Soft-RoCE supports. `SRD`
exists only on AWS EFA. `fetch_timeout_seconds` is checked against the L1
write-lock TTL at startup.

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

## Per-node registration fanout

`register_all_nodes` in `kv_sink_fanout.{h,cpp}` registers LMCache on every
cluster node and records each node's own `region` handle in a `NodeRegistry`.

**Multi-node queue pairs.** A pipelined request pulls chunks from several
Aerospike nodes, but every `RDMA_WRITE_WITH_IMM` must land in one readiness
table on the client. That requires a **shared completion queue** and **one RC
(or SRD) queue pair per node**, each with its own `qpn`/`psn` published in
that node's `kv-sink-register` command. Broadcasting a single `qpn` to every
node leaves at most one remote writer connected; the others fail at the fabric
with missing immediates and the request hangs. The RdmaContext overload that
takes `RdmaContext*` creates a dedicated queue pair per node before issuing
`aerospike_info_node` with that node's endpoint.

**One range per node.** Registration publishes the whole window range, not a
window, so the register command takes no window index. Which window a request
uses is decided by its slot offsets, which the session checks against one
window.

The legacy overload that accepts a single `LocalEndpoint` remains for baseline
tests that exercise one mock node with `create_queue_pair()` /
`connect_peer()`.

It is a separate translation unit from `kv_sink_client.{h,cpp}` so the codec
stays free of any Aerospike SDK dependency and remains trivially unit-testable;
only the fanout needs `libaerospike`.

Two things the fanout callback forces, both easy to get wrong:

- **The reply string must not be freed.** The SDK documents that for
  `aerospike_info_foreach` "the caller should not free this string", which is
  the *opposite* of `aerospike_info_node()` / `aerospike_info_any()`, whose
  responses the caller does free — as `discover_record_cap()` does. Copying the
  existing idiom verbatim would have introduced a double free.
- **The callback crosses a C boundary**, so no exception may escape it. The
  callback catches everything and reports failures through its `udata`.

Partial success is a real state and is reported rather than thrown: a single
unreachable node should not disable RDMA cluster-wide, so
`ClusterRegistrationResult` carries a per-node failure list and the caller
decides. A cluster-wide failure (for example, a disconnected client) still
throws.

## Pipelined fetch: signaling and chunking

The baseline protocol above is all-or-nothing. This section covers the
pipelined variant, which is **prototyped and passing over Soft-RoCE** but has
no server-side counterpart yet.

### EFA actually supports the primitive we need

This was the open question, and the answer is better than expected. Verified
against `rdma-core`'s EFA provider and the `efadv_query_device` man page:

| Capability | Flag | State |
|---|---|---|
| RDMA write | `EFADV_DEVICE_ATTR_CAPS_RDMA_WRITE` (1<<3) | Supported |
| Write **with immediate** | `ibv_wr_rdma_write_imm`, `IBV_QP_EX_WITH_RDMA_WRITE_WITH_IMM` | Supported |
| Unsolicited write recv | `EFADV_DEVICE_ATTR_CAPS_UNSOLICITED_WRITE_RECV` (1<<4) | Supported |
| Atomics | — | **Not** supported |
| In-order delivery | — | **Not** provided |

Two consequences worth calling out.

**Unsolicited receive is a gift for this design.** Normally each incoming
write-with-immediate consumes a receive work request, so a pure target has to
pre-post one per expected notification. EFA can create a queue pair with
`EFA_CREATE_QP_WITH_UNSOLICITED_WRITE_RECV`, where notifications consume no
receive work request and the completion is flagged unsolicited. That removes
the need to size a receive queue against the fetch's slot count. It requires
an extended CQ, and **peers must negotiate the same QP feature set**, so it is
a joint decision with the server.

**EFA exposes write-with-immediate only through the extended verbs API**
(`ibv_qp_ex` / `ibv_wr_rdma_write_imm`), not through `ibv_post_send` with
`IBV_WR_RDMA_WRITE_WITH_IMM`. The prototype here uses the legacy path because
that is what `rxe` supports; the EFA path needs the extended API. This is a
real porting step, not a flag change.

### Why arrival order cannot be trusted

SRD provides reliable but **out-of-order** delivery. AWS's own `SRD.txt` is
explicit — "SRD QPs provide out-of-order delivery without segmentation
support" — and there is no ordering guarantee between any two operations even
on a single queue pair, because packets are sprayed across up to 64 paths to
cut tail latency.

So the two obvious tricks are both unsafe on EFA:

- **Write data, then write a completion flag** — the flag can land first.
- **Write data, then SEND a notification** — the SEND can overtake the writes.

Both work perfectly on Soft-RoCE, which is RC and therefore ordered. That is
the trap: an ordering-dependent design passes every local test and corrupts
data on EFA.

The design consequence is that **readiness is a set, not a high-water mark**.
Layer 5 can complete before layer 2. `LayerReadiness` in
`csrc/storage_backends/aerospike/layer_pipeline.h` models it that way, and
`rdma_pipeline_test` asserts it by pushing layers 3, 2, 1 in that order and
requiring that layer 3 reports ready while layer 1 does not. vLLM consumes
layers in order and asks "is layer *i* ready?", which a set answers directly.

### The two levels of chunking

"How do we chunk keys for the layers" is really two questions with different
owners.

**Level 1: layers to Aerospike records.** This is Aerospike-side sharding
policy. Today a chunk is one logical payload split into `|s|<i>` segments
sized to the record cap, and those boundaries are *arbitrary* with respect to
layers. For pipelining, segment boundaries should **align to layer
boundaries**, so a record holds whole layers or a layer spans an integral
number of records. Otherwise one record straddles two layers, neither layer
completes until it lands, and the first layer is gated on data belonging to
the second.

This is where the layout derivation from `lmcache/v1/kv_layer_groups.py`
(`group_layers_by_identity`, `_detect_object_groups`) is genuinely needed —
not as an addition to the L2 interface, which is why AIE-89 was deferred, but
as input to sharding and offset computation.

**Implemented**, as plane-aligned sharding: a record is sized from the K/V
plane rather than from the record cap, so it belongs to exactly one layer. See
*The alignment hazard* in
[`layerwise_transfer_data_model.md`](layerwise_transfer_data_model.md). One
consequence is worth noting here, because it retires a tuning exercise: since
the record size is now derived from the plane, the record cap stops being a
knob for any payload whose planes already fit under it, so the M0 crossover
around 8 MiB no longer needs to be traded off against alignment. The two are
not in tension after all — and M0 measured smaller records as *faster* at a
fixed object size, so the extra records this rule produces may be a small gain
rather than a cost.

**Level 2: a layer to RDMA writes ("slots").** A layer can exceed one RDMA
write. `efadv_query_device` reports `max_rdma_size`, and the leased window
bounds it further, so a layer becomes N writes. Each write is a *slot* — one
piece of one layer — and each carries its own immediate. A layer is ready only
when every one of its slots has landed, which the harness checks with
two-piece layers rather than the trivial one-piece case.

### What the 32 bits carry

RDMA immediate data is exactly 32 bits, split as:

```text
 31            16 15             0
+----------------+----------------+
|   generation   |   slot index   |
+----------------+----------------+
```

The immediate names a **slot index into a plan LMCache built itself**, not a
layer id. Because LMCache chooses every destination offset, a slot index
recovers the layer, offset, and length without the server knowing anything
about transformer layers — which preserves the property that makes this
protocol pleasant: the server never reasons about model structure.

The **generation** exists because a write from a fetch that already timed out
can land after its window has been leased to a different request. The rkey is
still valid, so the NIC will perform that write. Tagging each fetch and
rejecting mismatched immediates stops LMCache from *acting* on a late writer.
It does not prevent the stray write itself — only re-registration does that —
and that gap is still open. See `ArrivalStatus::kStaleGeneration`.

Generation `0` is reserved and never allocated to a live request
(`kNoGeneration` in `pipelined_fetch_session.h`, matching `NO_GENERATION` in
`lmcache/v1/layerwise/contract.py`). A zero-initialised or defaulted
generation therefore fails to match any fetch instead of aliasing one — and
`is_layer_ready`'s `request_generation = 0` "skip the check" default stays
unambiguous, which it would not be if the first request of a process were
handed generation 0. The counter skips 0 on wrap for the same reason.

16 bits of slot index caps a request at 65536 slots. That is per *request* and
not per fetch command, since a request spans every participating chunk:
`chunks × layers × kv_size × pieces_per_plane`. An 80-layer model over 100
chunks at `kv_size = 2` and one piece per plane is 16,000 — comfortable, but a
4× margin rather than a 4000× one.

### Wire format for a pipelined fetch

**Status: the client side is implemented; the server side is proposed.** The
command builder, reply parser and declined-write handling live in
`kv_sink_client.h` and `layer_pipeline.h`. `PipelinedFetchSession` in
`pipelined_fetch_session.{h,cpp}` is the **implemented** driver that ties
them together: it allocates a per-request generation, builds a `RequestPlan`
via `SlotPlanner`, fans out one or more `kv-sink-fetch-pipelined` commands per
node (each carrying at most that node's advertised `max_sinks`), feeds declined
slots from each reply into `LayerReadiness::note_unservable`, and drains
`RdmaContext::poll_notifications` into the same tracker. It performs no I/O
itself — the connector issues the info calls and forwards reply strings. `AerospikeNativeConnector` exposes this path only when
`BUILD_WITH_AEROSPIKE_RDMA` is enabled and `kv-sink-register` has succeeded;
the TCP get/set path is unchanged otherwise. Python reaches this path only
through `StorageManager.layer_arrival_source()`: it returns the native
adapter's one `AerospikeLayerArrivalSource`, and retrieve runs
`LayerArrivalPump(source, sink).run(plan)` over it. The pump begins, polls
and releases the fetch, so the storage manager and the adapters have no
begin or readiness methods of their own. When no adapter has the pipelined
path, the accessor raises `LayerwiseContractError`, and retrieve falls back
to a whole-object load as it does for a failed fetch. The test mock implements a server-shaped
`handle_pipelined_fetch` so the codec is exercised over a real fabric; the
format below is still the contract to agree with the Aerospike server team.

The existing `kv-sink-fetch` cannot express this, because it has no per-write
identifier and its reply *is* the completion.

A new command rather than a flag on the old one, so a server that does not
implement it fails the command outright instead of silently performing an
all-or-nothing fetch that LMCache would then wait on forever:

```text
kv-sink-fetch-pipelined:namespace=<ns>;region=<id>;gen=<generation>;
  sinks=<digest>@<off>:<len>#<slot>,...
```

The only addition per sink is `#<slot>`. The generation is sent **once for the
command**, not per sink, and the server forms the immediate itself:

```text
immediate = (gen << 16) | slot
```

That is deliberate. Every fetch command issued for one request carries the
same generation, so making it a single field means a server cannot get it
wrong for an individual write, and it makes the request-scoped invariant
visible on the wire.

Reply:

```text
n=64;accepted=62;failed=17,42;bytes=32505856
```

`accepted` is a count of writes the server has undertaken to perform. **It is
not a completion.** Completion arrives only as immediates on LMCache's receive
queue.

Nine requirements, each of which exists because violating it produces a hang
or bad data rather than an error:

1. **One write per sink, one immediate per write.** The server must not
   coalesce adjacent sinks into a single larger write, however tempting when
   their offsets happen to abut. A coalesced write raises one immediate, the
   other slot never completes, and its layer hangs forever.
2. **`failed` must name every slot the server will not write**, by slot index.
   A missing record or a read error that is merely *omitted* from the reply is
   indistinguishable from a write still in flight, so the layer never reaches
   its expected count and the request hangs until the deadline. With the slot
   named, LMCache calls `LayerReadiness::note_unservable`, which marks the slot
   so its layer can never be reported ready — the layer becomes a recompute,
   and every other layer of the request still completes and is still
   pipelined. Note the layer is *not* served from the pieces that did arrive:
   the rest of its buffer holds whatever the window's previous tenant left
   there.
3. **Do not fence.** The point of the command is to reply before the writes
   complete. A server that polls its send queue to completion first has
   implemented the old command with extra steps.
4. **Serve in the order given, best effort.** The schedule is layer-major, so
   the sinks arrive ordered layer 0 first. This is a hint and not a
   correctness requirement — SRD reorders in flight and independent nodes
   interleave regardless — but the entire benefit of pipelining is that the
   earliest layers land first, so a server that reorders freely (by record
   locality, say) can erase the gain while still being correct.
5. **Release the record lock per piece**, not across the whole transfer, or
   concurrent requests for a popular chunk serialise behind each other.
6. **Never write outside `<off>:<len>`.** LMCache chose every destination and
   guarantees no two slots of a request overlap; the server must not round,
   coalesce, or pad. A write past the end lands in another slot's bytes, or
   outside the registered window.
7. **Duplicate immediates are safe; missing ones are not.** LMCache
   distinguishes a duplicate and ignores it, so a retransmit costs nothing. It
   cannot recover a dropped immediate for a write that did land.
8. **Do not exceed the slot count in immediates.** LMCache sizes its receive
   queue from the schedule it built. Extra notifications can exhaust it.
9. **One sink is one record is one write.** A sink names a record digest, a
   destination and a length, and nothing else — there is no record-relative
   source offset — so a sink can only mean "this whole record, there". LMCache
   therefore sizes slots from the record, not from the device's write limit:
   the piece size is `plane_segment_bytes(plane, record_cap)`, the same
   arithmetic the store path used to cut the plane up. A server should never
   need to split or combine a sink; if a record will not fit in one RDMA
   write, LMCache refuses to plan the request rather than emitting sinks no
   server could serve. (The device limit binds only in a pathological config:
   Aerospike caps a record at 8 MiB, and EFA's `max_rdma_size` is three orders
   of magnitude above that.)

   Should the format ever need sub-record writes, the extension is a
   record-relative source offset — `<digest>+<src>@<dst>:<len>#<slot>` — and
   not silent splitting on either side.

On the LMCache side the schedule comes from `SlotPlanner` in
`slot_planner.h` — it walks (chunk, layer, K/V plane, record) and produces
exactly these sinks, layer-major, with the offsets already resolved against
the leased window. `RequestPlan::slots_for_chunk` then partitions that
schedule by chunk, so each node receives commands naming only its own
chunks while the slot indices stay in the request's numbering. Sinks for one
node are sorted by slot (layer-major order from the plan) and sliced into
segments of at most `max_sinks_per_command`; the first command holds the
earliest slots so the server still sees low layers first across the sequence.
`build_pipelined_fetch_command` refuses a sink list with a repeated slot index,
which is the mistake that per-fetch numbering would produce.

#### Multiple commands per node vs splitting a request

**Status: implemented in the client.**

When a node owns more sinks than its advertised `max_sinks`, the session emits
`ceil(count / max_sinks)` pipelined commands for that node alone. This is safe
in a way that splitting the **request** would not be: slot indices and the
generation in each immediate are scoped to one request, so every command for
that request carries the **same** `gen`, and `LayerReadiness` cannot tell — and
does not need to know — how many commands delivered the slots that filled a
layer. Splitting a request would require several generations, several readiness
tables, and a cross-request rule before a layer may be consumed; none of that
exists on the wire or in the vLLM integration, and reporting a layer ready while
part of it is still in flight on another sub-request is exactly the failure this
design exists to prevent.

Reply accounting is **per command**: each acknowledgement is checked against the
sinks that specific command carried (`accepted + failed == n`, and `n` matches
the command), not against every sink the node owns in the request. A partial
failure across commands for one node — the first command accepted, the second
rejected outright — marks the second command's slots via `note_unservable` so
the request reaches a defined state instead of waiting forever on writes nobody
will perform.

#### Receive queue depth and device limits

**Status: implemented in the client; not verified on EFA/SRD fabric.**

On the RC path each `RDMA_WRITE_WITH_IMM` consumes one posted receive work
request, so the queue pair's `max_recv_wr` must be at least the slot count of
the largest request LMCache will plan. `RdmaContext` queries `ibv_query_device`
when the device is opened and records `max_qp_wr` and `max_cq`. At init,
`enable_layer_notifications()` clamps the depth derived from the leased window
and record cap to those limits, logs the device-reported numbers next to the
requested and effective depths, and sizes both the completion queue and the
receive queue to the effective value. `PipelinedFetchSession::begin_request`
rejects a plan whose `slot_count()` exceeds that effective depth with an error
that names both counts and tells the operator to use fewer chunks per request,
raise the record cap so each plane needs fewer pieces, or choose hardware with a
higher `max_recv_wr`.

A request whose plan exceeds the device-derived limit is **not** split into
several smaller requests. Slot indices and the generation in each immediate are
scoped to one request; `LayerReadiness` counts arrivals against a single plan.
Splitting would require several generations, several readiness tables, and a
rule that a layer is consumable only when every sub-request's pieces have
landed — none of which exists in the wire contract or the vLLM integration
today. Reporting a layer ready while part of it is still in flight on another
sub-request is exactly the failure this design exists to prevent, so oversize
plans fail loudly at `begin_request` instead.

**Still unknown without EFA hardware:** whether SRD actually charges a receive
work request per immediate when the unsolicited-write-receive queue-pair feature
is negotiated. The `efadv.h` available in this tree exposes no runtime knob to
enable that feature on the client side; if EFA turns out not to consume receive
work requests on that path, the `max_recv_wr` clamp documented here becomes a
conservative no-op rather than a binding limit. That has not been measured on a
real EFA instance.

**Device-free verification:** `notification_depth_test` (via `make -C
tests/v1/distributed/rdma logic-test`) injects device caps and checks clamping,
the derived slot limit, and `begin_request` acceptance/rejection — without
libibverbs.

**Device-free verification:** `pipelined_fetch_session_test` and
`pipelined_fetch_issue_test` (via `make -C tests/v1/distributed/rdma
logic-test`) cover multi-node slot numbering, declined-slot handling, stale
generations, abandon, transport-level node failures, and layout conversion —
without libibverbs or a cluster. That is **implemented** logic, not fabric
proof.

#### Two ways to begin a request

`begin_request(placements, chunk_nodes, slot_digests)` plans slots itself with
`SlotPlanner` and binds each chunk to one node. That binding is wrong in
general: Aerospike places every record by its own digest, so one chunk's
records usually sit on several nodes.

`begin_request_from_slots(slots)` takes a caller-planned list instead.
`slots[i]` is notification slot `i` and names its own node, digest, layer and
window range:

```text
slot 0: node-a  <digest k-0>  layer 0  offset 0    length 64
slot 1: node-b  <digest k-1>  layer 0  offset 64   length 64
slot 2: node-a  <digest k-2>  layer 1  offset 128  length 64
-> node-a: gen=G;sinks=<k-0>@0:64#0,<k-2>@128:64#2
   node-b: gen=G;sinks=<k-1>@64:64#1
```

Nothing is re-ordered, so the slot numbers on the wire are the caller's. This
is what the layerwise `LayerFetchPlan` uses (Python:
`NativePlanIssuer` → `issue_pipelined_fetch_by_slots`). The caller learns
each record's node from `record_node(user_key)`, which reads the client's
partition map. Both entry points enforce the same device slot cap. They throw
`PlanTooLargeError` (pybind: `PipelinedPlanTooLargeError`) when a plan exceeds
the cap, so callers can tell that case apart from other failures.

Two things this format does **not** yet settle, both needing the server
team's input:

- **Whether a node can report progress it has not been asked for.** If a
  server reads a record covering more than the requested slot, may it write
  and signal the extra? Currently no: an unknown slot index is a protocol
  violation (`ArrivalStatus::kUnknownSlot`).
- **What happens to in-flight writes when a request is abandoned.** The
  generation stops LMCache acting on them, but the writes still land in a
  window that may have been re-leased. Only re-registration truly prevents
  that, and its cost is unmeasured. See the open question on registration
  lifecycle.

### What the prototype proves, and what it does not

Passing (`make -C tests/v1/distributed/rdma test`):

- a layer is reported ready only once *every* piece has landed,
- its bytes are correct at the moment it is reported ready,
- **the destination regions of unsent layers are still untouched**, which is
  what makes early consumption meaningful rather than a race,
- a later layer can be ready while an earlier one is not,
- stale generations, unknown slots, and duplicate immediates are each
  distinguished rather than silently counted,
- the whole sequence again driven through the wire format rather than by
  staged calls: the client builds `kv-sink-fetch-pipelined` from its plan, the
  mock parses it and pushes without fencing, and a sink naming a record the
  server does not hold comes back in `failed` so its layer becomes a recompute
  while every other layer still lands and is still pipelined.

Not proven:

- **Any of it on EFA/SRD.** Soft-RoCE is RC-only and ordered, so the very
  hazard this design guards against cannot be reproduced locally. The extended
  verbs port and the unsolicited-receive negotiation are both untested.
- **A server that can do this.** The `handle_pipelined_fetch` above is the
  test mock, which is a demonstration that the contract is implementable and
  not evidence that Aerospike implements it: the real server fences and replies
  once. Per-slot signaling needs it to issue write-with-immediate per piece and
  *not* fence, plus release its record lock per piece rather than holding it
  across the whole transfer.
- **That storage-level pipelining is possible at all.** Slicing an
  already-read record into signaled pieces buys compute overlap. Overlapping
  the *disk read* of layer 1 with the network send of layer 0 needs
  independently readable layers, which is level-1 chunking above and a much
  larger change.
- **Anything about real layers *over the fabric*.** The fabric harness still
  moves synthetic pieces at fabricated offsets. The mapping from actual KV
  layout onto slots is implemented in `slot_planner.h` and covered by
  `test_slot_planner.py`; `PipelinedFetchSession` is covered by
  `pipelined_fetch_session_test` without a device. What remains **unverified
  over a fabric** is an end-to-end load that builds digests and placements from
  a real prefetch, issues commands through `AerospikeNativeConnector`, and
  consumes layers while bytes are still landing. See
  [`layerwise_transfer_data_model.md`](layerwise_transfer_data_model.md).

## Reproducing the Soft-RoCE test setup

Soft-RoCE (`rdma_rxe`) presents a functional RDMA device over ordinary
Ethernet or loopback, so the data path can be exercised without RDMA NICs. It
supports RC but **not** SRD, which is why RC is the portable path.

On a Windows workstation this needs a Linux VM, because `rdma_rxe` is a kernel
module and neither WSL2 nor Docker Desktop ships one that has it. See
[`rdma_testing_on_windows.md`](rdma_testing_on_windows.md).

All of the following needs **root**.

If you have Docker but not an interactive `sudo` — a common state on a shared
or managed workstation — a privileged container is enough, because both steps
act on the host kernel rather than on the container. `rdma_rxe` is a kernel
module, so loading it inside the container loads it for the machine, and with
`--network host` the RDMA link is created in the host's namespace and shows up
in `ibv_devinfo` outside the container:

```bash
docker run --rm --privileged -v /lib/modules:/lib/modules:ro ubuntu:22.04 \
  sh -c "apt-get update -qq && apt-get install -y -qq kmod && modprobe rdma_rxe"

docker run --rm --privileged --network host ubuntu:22.04 \
  sh -c "apt-get update -qq && apt-get install -y -qq rdma-core && \
         rdma link add rxe0 type rxe netdev lo"
```

The device does not survive a reboot, so expect to redo this. Note that being
able to run privileged containers is equivalent to root on the host; this is a
convenience on a machine you already administer, not a way around a restriction
someone else imposed.

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

`ibv_reg_mr` fails if the pinned slab exceeds `RLIMIT_MEMLOCK`. Check it with
`ulimit -l`. A plain `ulimit -l unlimited` only works if the *hard* limit
allows it; otherwise set `memlock` in `/etc/security/limits.conf` and log in
again. Many desktop distributions already ship a multi-gigabyte limit, which is
ample for a test slab.

Then build with RDMA enabled and point the adapter at `device_name: "rxe0"` and
the `gid_index` chosen in step 5.

### The GID index trap

**`gid_index` is not always 0**, and getting it wrong fails late and
confusingly: `ibv_modify_qp` returns `ENETUNREACH` ("Network is unreachable")
at the **RTR** step, after registration has already succeeded.

`rxe` derives GID 0 from the netdev's MAC as an `fe80::` link-local address.
Loopback's MAC is all zeros, so GID 0 becomes
`fe80:0000:0000:0000:0200:00ff:fe00:0000`, which has no route — the kernel
cannot resolve a path from it, so the QP never reaches RTR. The table on `lo`
looks like this:

| Index | GID | Usable |
|---|---|---|
| 0 | `fe80::200:ff:fe00:0000` | No — link-local from an all-zero MAC |
| 1 | `::ffff:7f00:0001` (IPv4-mapped `127.0.0.1`) | **Yes** |
| 2 | `::1` | Yes |

So Soft-RoCE on `lo` wants **index 1**. EFA wants index 0, which is why the
reference client snippet uses `ibv_query_gid(ctx, IB_PORT, 0, &gid)` — correct
there, wrong here. This is exactly the class of divergence that makes "it
passes on Soft-RoCE" a weak signal for EFA.

### Equivalence test

The deliverable that matters is that a payload delivered by RDMA write into L1
is **byte-identical** to the same payload fetched through the normal non-RDMA
Aerospike path.

The harness lives at `tests/v1/distributed/rdma/`. `KvSinkMockWriter` stands in
for the Aerospike server: it accepts the same `kv-sink-register` /
`kv-sink-fetch` command strings, performs real `ibv_post_send` RDMA writes into
the registered windows at the requested offsets, fences on its own send CQ
before replying, and replies in the same `key=value;` format.

The sink side uses the **production** `RdmaContext` and the production codec
(`build_register_command`, `parse_register_reply`, `parse_fetch_reply`) rather
than reimplementing them, so a regression in either fails this test. Only the
Aerospike info-command control plane is faked; the data path is a genuine RDMA
write across the fabric.

Once a device exists, it is one command:

```bash
make -C tests/v1/distributed/rdma test RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
```

or through pytest, which builds it for you:

```bash
RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  pytest -xvs tests/v1/distributed/rdma/test_rdma_equivalence.py
```

Both default to GID index 0, which is right for EFA and wrong for Soft-RoCE on
`lo`; see [the GID index trap](#the-gid-index-trap). The device and index are
positional, so `RDMA_GID_INDEX` requires `RDMA_DEVICE`.

Both skip cleanly with no device — the binary exits 77 (the automake "skip"
convention) and the wrapper turns that into a pytest skip. If your rdma-core
lives outside the default prefix, set `RDMA_CORE_INCLUDE_DIR` and
`RDMA_CORE_LIBRARY_DIR`.

The harness asserts five things, not just the memcmp:

1. both chunks landed byte-identical to the source payload,
2. the fetch reply reports every chunk ok, with the expected byte count,
3. **nothing landed outside the requested offsets** (the rest of the slab is
   still zero),
4. a sink targeting an offset past the registered window is **refused** rather
   than written, which is the bounded-window guarantee actually being exercised,
5. the region handle is held per node, via `NodeRegistry`.

## What is not proven yet

Being precise about this, because the gap matters:

| Piece | State |
|---|---|
| Build profile, default-off | **Verified.** Default native build compiles and links with no libibverbs. |
| `rdma_context.{h,cpp}` | **Verified on RC.** QP reaches RTS and the data path executes over Soft-RoCE. The EFA/SRD path compiles but has never run. |
| `kv_sink_client.{h,cpp}` codec | **Verified.** Register and fetch commands round-trip against the mock writer. |
| Per-node `kv-sink-register` fanout | **Implemented and compiling** via `aerospike_info_foreach`. Never run against a cluster. |
| `PipelinedFetchSession` driver | **Implemented** and covered by `pipelined_fetch_session_test` (no device). |
| Connector + Python pipelined path | **Implemented (device-free).** `AerospikeNativeConnector::issue_pipelined_fetch` performs begin, per-node `aerospike_info_node`, and reply feeding without holding the driver lock across I/O; `set_object_group_layouts` converts registered `MemoryLayoutDesc` shapes in C++; `finish_pipelined_fetch` / `abandon_pipelined_fetch` and `pipelined_fetch_init_error` are bound through pybind and threaded via `StorageManager` and `NativeConnectorL2Adapter`. Python hands over record user keys through `issue_pipelined_fetch_by_keys`, and the connector derives each sink's digest with `record_digest_hex` (verified against the reference client on a real server). Covered by `pipelined_fetch_issue_test` and Python adapter tests. **Not verified over a fabric** with real digests from a prefetch load. |
| Adapter plumbing, descriptor, window plan | **Implemented and unit-tested.** |
| Write-lock TTL invariant | **Implemented and unit-tested** (startup check). |
| Windows reserved outside general L1; `delete_if_none_locked`, `abort_write` | **Implemented and unit-tested** on pinned CPU memory. No lease yet, so nothing allocates in a window in production. |
| Mock RDMA writer | **Verified.** Posts real `ibv_post_send` writes and fences on its own send CQ. |
| Real RDMA write landing in L1 | **Verified** over Soft-RoCE (`rxe0` on `lo`, GID index 1). |
| Byte-equivalence assertion | **Verified.** Both chunks byte-identical, nothing outside the requested offsets, out-of-window write refused. |
| Same over EFA/SRD | **Not achieved — needs an EFA instance.** |
| Against a real Aerospike server | **Not achieved — needs Sriram's server branch deployed.** |

The remaining two rows are the ones that matter now, and neither is a local
environment problem.

**EFA/SRD is a genuine gap, not a formality.** Soft-RoCE only supports RC, so
the SRD path — `efadv_create_qp_ex`, the qkey at INIT, and the `ibv_create_ah`
for the server without which its write fails `UNKNOWN_PEER` — is still
untested. The GID index divergence documented above is a concrete instance of
the same hazard: the correct value differs between the two fabrics, and the
failure surfaces several calls later as `ENETUNREACH`. Re-verify the handshake
on a real EFA instance before treating the M2 gate as passable.

**The mock writer is a mock of a protocol, not of an implementation.** It
implements the `kv-sink-*` command strings as specified, so it proves our side
of the contract. It cannot catch a divergence between that specification and
what the server actually does.

## Open questions

### Resolved: L1 locking across the DMA

The existing write lock already provides the needed guarantee — no new lock,
and no separate window lifetime for the non-pipelined path. See
[The write-lock TTL invariant](#the-write-lock-ttl-invariant-enforced-at-startup)
for the mechanism, the TTL hazard it creates, and the startup check that now
enforces it.

### Still open: registration lifecycle on node restart

`region` is per-node and connection-lifetime. Detecting a node restart and
re-registering means re-publishing rkeys while fetches may be in flight against
the old region.

**This is intentionally left conservative**: `register_l1()` refuses a second
call and `NodeRegistry::invalidate_all()` drops every handle at once. Proper
drain-then-reregister semantics need the Aerospike side to specify whether
in-flight fetches against a stale `region` are **dropped or completed**, and
that is not knowable from the LMCache side. **Blocked on a protocol answer from
Aerospike**; no policy has been invented here.

## Where the protocol and LMCache fight each other

- **No per-layer signal.** Property 2 above (the info reply is the completion)
  makes a fetch all-or-nothing. Layer-by-layer pipelining needs per-layer
  completion, which this protocol cannot express. The LMCache side of the fix
  is now prototyped and passing — see
  [Pipelined fetch](#pipelined-fetch-signaling-and-chunking) — so the blocker
  is entirely server-side: the server must signal each piece with
  write-with-immediate and stop fencing.
- **Synchronous completion vs. an async adapter.** The fetch blocks a thread
  for the entire round trip including the DMA. LMCache's MP request path is
  future-based and expects to poll, so the blocking info call has to be run on
  a worker thread and bridged back, which is what the existing
  `ConnectorBase` worker pool already does for the non-RDMA path.
- **Registration is per-node, but `L1MemoryDesc` is per-process.** One slab
  must be registered with, and have its rkeys published to, every node
  independently. The descriptor has no notion of a per-peer handle, which is
  why `MemoryRegistration` is carried as a value rather than being assumed
  unique.
- **`aerospike_info_any` is the wrong primitive** for a per-node control
  plane, despite being the idiom already present in the connector.

## Related

- [`layerwise_transfer_data_model.md`](layerwise_transfer_data_model.md) — how
  real KV layout maps onto the slots this document signals about, with an
  interactive walkthrough in
  [`layerwise-transfer-data-model.html`](layerwise-transfer-data-model.html).
- [`rdma_testing_on_windows.md`](rdma_testing_on_windows.md) — standing up an
  Ubuntu VM on a Windows workstation and running these suites in it.
- `docs/design/v1/multiprocess/transport/request_transport.md` — the MP request
  transport, relevant to any future per-layer delivery.
- `lmcache/v1/distributed/l2_adapters/mooncake_store_l2_adapter.py` — the
  reference L1-registration factory this adapter mirrors.
