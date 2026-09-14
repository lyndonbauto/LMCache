# Aerospike RDMA reception into L1

Status: **prototype**. The verbs foundation, the build profile, and the adapter
plumbing are implemented. The end-to-end data path has **not** been executed
against real hardware; see [What is not proven yet](#what-is-not-proven-yet).

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
region=4;transport=verbs;qp=srd;qpn=49152;psn=..;gid=..
```

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
cost is a cap on concurrency (`window_count` is the maximum number of
concurrently outstanding RDMA fetches) and a fixed `window_bytes` that must be
large enough for the largest single fetch.

Either way, registration happens **exactly once, at initialization**.
`ibv_reg_mr` is expensive enough to erase the entire benefit of the RDMA path,
so `register_l1()` refuses a second call rather than silently re-registering
and invalidating rkeys already published to nodes.

### Allocator constraint

A slab that grows or moves after `ibv_reg_mr` leaves the remote writer holding
an rkey for memory LMCache no longer owns. Rather than have the adapter
introspect allocator internals, `L1MemoryDesc` now carries a
`MemoryGrowthPolicy`: `FIXED` for `MixedMemoryAllocator`, `GROWABLE` for
`LazyMemoryAllocator`, which can expand its slab. Enabling RDMA with a
`GROWABLE` slab raises `ValueError` naming the hazard and the fix.

This replaces the standing `TODO(ApostaC)` in `l1_memory_manager.py` with an
enforced contract instead of an untested assumption.

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
    },
}
```

`RC` is the portable transport and is the only one Soft-RoCE supports. `SRD`
exists only on AWS EFA.

## Reproducing the Soft-RoCE test setup

Soft-RoCE (`rdma_rxe`) presents a functional RDMA device over ordinary
Ethernet or loopback, so the data path can be exercised without RDMA NICs. It
supports RC but **not** SRD, which is why RC is the portable path.

All of the following needs **root**.

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

# 5. Prove the fabric works independently of LMCache.
ibv_rc_pingpong -d rxe0 -g 0 &   # server
ibv_rc_pingpong -d rxe0 -g 0 localhost
```

`ibv_reg_mr` fails if the pinned slab exceeds `RLIMIT_MEMLOCK`. Raise it before
registering a large L1:

```bash
ulimit -l unlimited      # or set memlock in /etc/security/limits.conf
```

Then build with RDMA enabled and point the adapter at `device_name: "rxe0"`,
`gid_index: 0`.

### Equivalence test

The deliverable that matters is that a payload delivered by RDMA write into L1
is **byte-identical** to the same payload fetched through the normal non-RDMA
Aerospike path. Because the Aerospike server side is not generally available, a
mock RDMA writer stands in for it: a local process that accepts the same
`kv-sink-register` / `kv-sink-fetch` command strings, performs real
`ibv_post_send` RDMA writes into the registered windows at the requested
offsets, fences on its own send CQ, and replies in the same `key=value;`
format.

## What is not proven yet

Being precise about this, because the gap matters:

| Piece | State |
|---|---|
| Build profile, default-off | **Verified.** Default build compiles with no libibverbs. |
| `rdma_context.{h,cpp}` | **Compiles and links** cleanly (`-Wall -Wextra`, RC and EFA paths), against real `libibverbs`. Never executed. |
| Adapter plumbing, descriptor, window plan | **Implemented and unit-tested.** |
| Per-node `kv-sink-register` fanout | **Not implemented.** Shape is designed; see below. |
| Mock RDMA writer | **Not implemented.** |
| Real RDMA write landing in L1 | **Not achieved.** |
| Byte-equivalence vs. the normal path | **Not achieved.** |

The blocker for everything in the bottom half of that table is environmental:
creating a Soft-RoCE device requires `root` (`modprobe rdma_rxe` and
`rdma link add` both return `Operation not permitted` without it), and no RDMA
device of any kind is present. The verbs code was therefore verified by
compilation and linkage only.

## Open questions

Two things are genuinely ambiguous and should be settled before this goes
further:

1. **L1 locking across the DMA.** The info reply is the completion, so the
   destination window must stay valid and unread for the whole server-side
   round trip. It is not yet clear which existing L1 lock, if any, already
   provides that for a prefetch-into-L1, or whether the window lease needs its
   own lifetime independent of the L1 object lock. The engineer's own PoC gap
   list notes the server holds the *record* lock across the DMA, which is the
   mirror image of this question.
2. **Registration lifecycle on node restart.** `region` is per-node and
   connection-lifetime. Detecting a node restart and re-registering means
   re-publishing rkeys while fetches may be in flight against the old region.
   The safe ordering, and whether in-flight fetches must be drained first, is
   not specified by the protocol.

## Where the protocol and LMCache fight each other

- **No per-layer signal.** Property 2 above (the info reply is the completion)
  makes a fetch all-or-nothing. Layer-by-layer pipelining needs per-layer
  completion, which this protocol cannot express. Out of scope here by
  instruction, but it is the main thing blocking the later tickets.
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

- `docs/design/v1/multiprocess/transport/request_transport.md` — the MP request
  transport, relevant to any future per-layer delivery.
- `lmcache/v1/distributed/l2_adapters/mooncake_store_l2_adapter.py` — the
  reference L1-registration factory this adapter mirrors.
