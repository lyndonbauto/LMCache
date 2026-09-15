# Aerospike RDMA reception into L1

Status: **prototype**. The verbs foundation, the build profile, the adapter
plumbing, the per-node registration fanout, the mock RDMA writer, and the
byte-equivalence harness are all implemented and compiling. The data path has
**not** yet been executed, because no RDMA device is available; see
[What is not proven yet](#what-is-not-proven-yet).

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

## Per-node registration fanout

`register_all_nodes` in `kv_sink_fanout.{h,cpp}` sends the register command to
every node with `aerospike_info_foreach` and records each node's own `region`
handle in a `NodeRegistry`. The command text is identical for every node — it
describes *our* endpoint — but each reply carries that node's own region id,
peer GID, and peer QPN.

It is a separate translation unit from `kv_sink_client.{h,cpp}` so the codec
stays free of any Aerospike SDK dependency and remains trivially unit-testable;
only the fanout needs `libaerospike`.

Two things `aerospike_info_foreach` forces, both easy to get wrong:

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
as input to sharding and offset computation. Note the interaction with the M0
finding that record size has a **crossover** around 8 MiB rather than "bigger
is better": layer-aligned sharding constrains the record size, so the two
have to be tuned together rather than independently.

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

16 bits of slot index caps a fetch at 65536 slots, far above a 60-layer model
at a few writes per layer.

### What the prototype proves, and what it does not

Passing (`make -C tests/v1/distributed/rdma test`, 24 checks):

- a layer is reported ready only once *every* piece has landed,
- its bytes are correct at the moment it is reported ready,
- **the destination regions of unsent layers are still untouched**, which is
  what makes early consumption meaningful rather than a race,
- a later layer can be ready while an earlier one is not,
- stale generations, unknown slots, and duplicate immediates are each
  distinguished rather than silently counted.

Not proven:

- **Any of it on EFA/SRD.** Soft-RoCE is RC-only and ordered, so the very
  hazard this design guards against cannot be reproduced locally. The extended
  verbs port and the unsolicited-receive negotiation are both untested.
- **A server that can do this.** Aerospike currently fences and replies once.
  Per-slot signaling needs the server to issue write-with-immediate per piece
  and *not* fence, plus release its record lock per piece rather than holding
  it across the whole transfer.
- **That storage-level pipelining is possible at all.** Slicing an
  already-read record into signaled pieces buys compute overlap. Overlapping
  the *disk read* of layer 1 with the network send of layer 0 needs
  independently readable layers, which is level-1 chunking above and a much
  larger change.
- **Anything about real layers.** The harness moves synthetic pieces at
  fabricated offsets. Mapping actual KV layout onto slots — and the fact that
  a layer is *not* one contiguous range once `kv_size > 1` — is
  [`layerwise_transfer_data_model.md`](layerwise_transfer_data_model.md).

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
| Adapter plumbing, descriptor, window plan | **Implemented and unit-tested.** |
| Write-lock TTL invariant | **Implemented and unit-tested** (startup check). |
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
  real KV layout maps onto the slots this document signals about.
- `docs/design/v1/multiprocess/transport/request_transport.md` — the MP request
  transport, relevant to any future per-layer delivery.
- `lmcache/v1/distributed/l2_adapters/mooncake_store_l2_adapter.py` — the
  reference L1-registration factory this adapter mirrors.
