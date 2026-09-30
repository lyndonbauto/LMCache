# Aerospike server issues found from the LMCache side

Defects and contract gaps in the aerospike-server branch
`feat/kv-sink-fetch-pipelined` (checked at `512b0c20`), found while running
the LMCache RDMA client against it. The protocol they are measured against is
in [`aerospike_rdma.md`](aerospike_rdma.md); the test setups are in
[`rdma_testing_on_windows.md`](rdma_testing_on_windows.md) and
[`rdma_testing_on_efa.md`](rdma_testing_on_efa.md).

This is a hand-off list for the server team. Each entry says what is wrong,
what LMCache sees, and what fix we expect. Client bugs are not listed here;
they are fixed in LMCache and described in `aerospike_rdma.md`.

| # | Issue | Severity | Seen on | Status |
|---|---|---|---|---|
| 1 | [Record released while its bytes are still being sent](#1-record-released-while-its-bytes-are-still-being-sent) | Data corruption | Code reading | Open |
| 2 | [EFA write support is never detected](#2-efa-write-support-is-never-detected) | EFA unusable | EFA v2 | Local patch only |
| 3 | [Crash on the register after a refused device](#3-crash-on-the-register-after-a-refused-device) | Server crash | EFA v2 | Open |
| 4 | [Pipelined fetch replies only after every write completes](#4-pipelined-fetch-replies-only-after-every-write-completes) | No overlap | Soft-RoCE, EFA v2 | Open |
| 5 | [Stripes are registered lazily, on the fetch path](#5-stripes-are-registered-lazily-on-the-fetch-path) | Spurious fallback | Soft-RoCE, EFA v2 | Open |
| 6 | [Device and GID index are not configurable](#6-device-and-gid-index-are-not-configurable) | Wrong fabric | EFA setup | Open |
| 7 | [Concurrent fetches on one region corrupt each other](#7-concurrent-fetches-on-one-region-corrupt-each-other) | Use-after-free | Code reading | Open |
| 8 | [One failed write disables the region for good](#8-one-failed-write-disables-the-region-for-good) | Silent permanent fallback | Code reading | Open |
| 9 | [A dead client's region is never reclaimed](#9-a-dead-clients-region-is-never-reclaimed) | Registration refused | Code reading | Open |
| 10 | [The reap loop busy-spins an info thread](#10-the-reap-loop-busy-spins-an-info-thread) | CPU and info-thread starvation | Code reading | Open |
| 11 | [kv-sink fetches never apply read-touch](#11-kv-sink-fetches-never-apply-read-touch) | Hot entries expire | Code reading | Open |

Two smaller items are under [Minor](#minor).

## 1. Record released while its bytes are still being sent

**Where:** `fetch_one_common` in `as/src/base/kv_sink.c`, with
`verbs_write_imm` and `verbs_write` in `as/src/base/kv_sink_verbs.c`.

`fetch_one_common` opens the record, calls the transport to post the write,
and then releases the record, the storage handle and the partition
(`as_storage_record_close`, `as_record_done`, `as_partition_release`). The
write is zero-copy: its scatter entry points at the record's value inside the
namespace stripe. So the NIC is still reading those bytes after the server
has stopped protecting them:

```text
fetch_one_common
  as_record_get                  record held
  deliver -> ibv_post_send       NIC starts reading the value in place
  as_record_done                 record released
                                 ... NIC still reading ...
imm_finish / fence               send completion reaped
```

If the record is overwritten or deleted in that window, `drv_mem.c` frees
its old blocks (`block_free`), defrag can recycle the write block, and a new
write can land there. The NIC then sends whatever those bytes hold by then.

**What LMCache sees:** nothing. The immediate arrives and the byte count
matches, so the slot is counted as landed with the wrong contents. The client
cannot detect this without checksumming every landed piece.

**Why it is rare today:** LMCache keys chunks by content hash, so an
overwrite normally carries identical bytes; corruption needs a delete or
eviction, defrag, and a new write into the same block during one transfer.
EFA widens the window, because a stalled write is retried by the NIC for
seconds.

The drain in `imm_finish` (issue 4) does not protect against this: every
record has already been released by then. The non-pipelined `kv-sink-fetch`
has the same hole, since `fetch_one` also releases before `fence()`.

**Expected fix:** keep the source bytes alive until *that write's* send
completion, either by holding the record reference in the work-request token
and releasing it when the completion is reaped, or by copying each piece into
a registered staging buffer before posting. Requirement 5 in
`aerospike_rdma.md` ("release the record lock per piece") means release at
the piece's completion, not at its post.

## 2. EFA write support is never detected

**Where:** `kv_sink_verbs.c`, lines 81 to 92, and the capability check in
`dev_get`.

```c
#ifndef EFADV_DEVICE_ATTR_CAPS_RDMA_WRITE
#define EFADV_DEVICE_ATTR_CAPS_RDMA_WRITE 0
#endif
```

The guard is meant for rdma-core older than v46. But
`EFADV_DEVICE_ATTR_CAPS_RDMA_WRITE` is an enum constant in `efadv.h`, not a
macro, so `#ifndef` is always true and the bit is always 0. Every EFA device
logs `rdma write no` and `dev_get` refuses it with `device has no RDMA write -
pull direction not implemented`.

**What LMCache sees:** `kv-sink-register` fails on every EFA node, so every
retrieve loads whole objects. On the next register the server crashes
(issue 3).

**Expected fix:** detect the header version at build time (for example, a
configure check that compiles a use of the enum) instead of `#ifndef`.
Deleting the three lines fixes it on rdma-core 46 and later, which is the
local patch used for A8 on EFA.

## 3. Crash on the register after a refused device

**Where:** `dev_get` in `kv_sink_verbs.c`.

`dev_get` sets `g_dev.tried = true` and `g_dev.ctx` before it checks the
device. Two exits return an error while leaving `ctx` set:

- the RDMA-write refusal (issue 2), which returns before `ibv_alloc_pd`, so
  `g_dev.pd` is NULL;
- the `ibv_alloc_pd`, `ibv_query_port` or `ibv_query_gid` failure path.

The next call sees `tried && ctx != NULL` and returns `&g_dev` as a usable
device. The following `kv-sink-register` then creates a queue pair on a NULL
protection domain and crashes in `efadv_create_qp_ex` (`make_qp`).

**What LMCache sees:** the first register fails, the second kills the node.

**Expected fix:** on any failure after `ibv_open_device`, release what was
acquired (`ibv_dealloc_pd`, `ibv_close_device`) and leave `g_dev.ctx` NULL,
so later calls report `no RDMA device`. Or record the failure in a separate
field and check it on the fast path.

## 4. Pipelined fetch replies only after every write completes

**Where:** `as_kv_sink_fetch_pipelined_cmd` in `kv_sink.c` and
`verbs_imm_finish` in `kv_sink_verbs.c`.

The command posts one signaled write-with-immediate per sink, which is
correct. It then calls `imm_finish`, which polls the send completion queue
until every write of the command completes (bounded by `FENCE_DEADLINE_NS`,
30 s), and only then builds the reply. That breaks requirement 3 ("do not
fence") in `aerospike_rdma.md`.

**What LMCache sees:** correct data, but no overlap. The client sends a
node's commands one at a time and `begin_fetch` returns after the last reply,
so every byte has landed before the first layer is loaded. A pipelined
retrieve behaves like an all-or-nothing fetch with per-slot failure
reporting. It also makes issue 5 worse, because the info call now lasts as
long as the whole transfer.

**Expected fix:** reply once every sink has been posted or declined. Reap
send completions afterwards, on the region's completion queue, and report a
write that fails after the reply only through the missing immediate, which
the client already treats as a timeout.

## 5. Stripes are registered lazily, on the fetch path

**Where:** `stripe_mr_get` in `kv_sink_verbs.c`.

The first write from each namespace stripe registers the whole stripe with
`ibv_reg_mr`, inside the fetch, while holding `g_dev_lock`. Two problems
follow:

- **Latency on a cold server.** Eight 256 MiB stripes took about a second on
  EFA. The client sends `kv-sink-fetch-pipelined` with the C client's default
  info timeout, 1000 ms, and (issue 4) the reply waits for the writes, so the
  first fetch after a restart can time out. A timeout declines every slot of
  the command, and the retrieve falls back.
- **No retry.** A freshly started server on Soft-RoCE can fail the first
  registration with `cannot register {lmcache} stripe N ... Cannot allocate
  memory` even with `memlock` raised, then succeed on the next fetch. The
  failed stripe's records are declined for that fetch.

**What LMCache sees:** the first fetch after a restart falls back
(`test_a_pipelined_fetch_lands_every_stored_byte_in_l1` fails with
`FELL_BACK` only on that run).

**Expected fix:** register every stripe of the namespace at
`kv-sink-register` time (or at startup when kv-sink is enabled), and fail the
register if that fails, so the fetch path never calls `ibv_reg_mr`. On the
LMCache side, a longer info timeout for pipelined commands is a mitigation,
not a fix.

## 6. Device and GID index are not configurable

**Where:** `dev_get` in `kv_sink_verbs.c`.

`dev_get` opens `list[0]`, the first RDMA device, and uses GID index
`is_srd ? 0 : 1`. Both are right for the test setups and wrong in general:

- a host with a Soft-RoCE device alongside EFA may pick `rxe0`;
- on RoCE, GID index 1 is the IPv4-mapped entry only on `lo`; on a real NIC
  the right index depends on the interface and RoCE version. A wrong GID
  fails later as `ENETUNREACH` at the RTR transition, as the client saw on
  Soft-RoCE (see *The GID index trap* in `aerospike_rdma.md`).

**Expected fix:** config items for the device name and GID index, with the
current values as defaults, and the chosen device and GID logged at startup.

## 7. Concurrent fetches on one region corrupt each other

**Where:** `verbs_imm_finish`, `verbs_wr_complete` and `verbs_write_imm` in
`kv_sink_verbs.c`.

Nothing serializes two `kv-sink-fetch-pipelined` commands on the same region,
and three pieces of per-region state assume one command at a time:

- **`vr->imm_reap_closed` is per region, not per command.** Each command's
  `imm_finish` clears it on entry and sets it on exit. If command A finishes
  while command B is still in flight, B's completions are treated as late:
  `verbs_wr_complete` frees B's tokens while B's `tracked` array still points
  at them, and sets `xfer_failed` (issue 8).
- **The completion queue and queue pair have no lock.** Both commands'
  info threads poll `vr->cq` and build work requests on `vr->qpx`
  (`ibv_wr_start` to `ibv_wr_complete`) concurrently.
- **The send queue is shared and exactly one command deep.** `max_send_wr`
  is `CQ_DEPTH` (256), and the register reply advertises `max_sinks=256`
  (`MAX_PIPELINED_SINKS`). Two full commands cannot both be posted, so the
  second one's posts fail and its slots are declined.

**What LMCache sees:** the client's concurrent fetches (W3) send several
generations to one node's region, one per window. Expect declined slots, a
disabled region, or a server crash. The A8 tests run one fetch at a time, so
none of this has been observed yet.

**Expected fix:** track reap state per command (in the command's own token
list), take a per-region lock around posting and polling, and size the send
queue for the number of concurrent fetches the region accepts. Advertise that
number in the register reply, or refuse a second concurrent command
explicitly.

## 8. One failed write disables the region for good

**Where:** `verbs_wr_complete`, `verbs_imm_finish` and `verbs_region_ready`
in `kv_sink_verbs.c`.

A late completion, or a slot still in flight when the reap deadline passes,
sets `vr->xfer_failed`. Nothing clears it, and `verbs_region_ready` then
refuses every write on that region with `region N in error state`.

**What LMCache sees:** after one slow or failed transfer, every later fetch
on that registration declines all its slots, and the retrieve falls back
whole, with no error that tells the client to register again. It lasts until
the client process restarts.

**Expected fix:** report the state, either in the fetch reply or by failing
the command with an error the client can act on by re-registering. If the
queue pair itself is still usable, reset the flag once the region's
outstanding writes have drained.

## 9. A dead client's region is never reclaimed

**Where:** `g_regions` and `MAX_REGIONS` in `kv_sink.c`.

A node holds at most 16 regions (`MAX_REGIONS`). A region is freed only by
`kv-sink-deregister`; there is no idle timeout and no dead-peer detection.

**What LMCache sees:** a client killed without deregistering (OOM, `kill
-9`, a host crash) keeps its slot until the server restarts. After 16 such
exits, every `kv-sink-register` on that node fails, and every client falls
back to whole-object loads. Even without leaks, 16 is tight: each client
process registers one region per node, so two vLLM instances with 8-way
tensor parallelism use all of them.

**Expected fix:** expire regions that see no command for a configurable
period (the client can send a cheap keepalive), or detect a dead peer
through the queue pair. Make `MAX_REGIONS` configurable.

## 10. The reap loop busy-spins an info thread

**Where:** `verbs_imm_finish` in `kv_sink_verbs.c`.

When `ibv_poll_cq` returns nothing, the loop checks the deadline and polls
again, with no sleep and no completion channel. A fetch whose writes stall
spins one core for up to `FENCE_DEADLINE_NS` (30 s).

**What LMCache sees:** during the EFA run, where the client's receives never
matched, each fetch held an info thread for about 4 s. Info threads are a
fixed pool, so a few stalled fetches can starve the node's other info
commands, including the C client's cluster-tend requests.

**Expected fix:** fixing issue 4 removes the wait from the info thread. Until
then, back off between empty polls or wait on a completion channel.

## 11. kv-sink fetches never apply read-touch

**Where:** `fetch_one_common` in `as/src/base/kv_sink.c`; compare
`as_read_touch_check` in `as/src/transaction/read_touch.c`, whose only caller
is the client read path in `as/src/transaction/read.c`.

With `default-read-touch-ttl-pct` set, a client read near a record's end of
life resets its TTL. A `kv-sink-fetch` or `kv-sink-fetch-pipelined` reads the
record through its own path and never makes that check, so a record served
only by RDMA is never extended.

**What LMCache sees:** the entries served most often, which are the ones
fetched layer by layer, expire on their write TTL while whole-object loads
keep colder entries alive. After expiry the lookup misses and the prefix is
recomputed and stored again. Nothing is served wrong. LMCache's lookups
deliberately never touch (see *Keeping frequently used entries* in the
Aerospike L2 page), so they cannot make up for it.

**Expected fix:** apply the namespace's read-touch rule to each record a
fetch sends, the same way a client read does, with the command able to opt
out.

## Minor

- **SRD does not set an RNR retry count.** `to_rts` sets the finite
  `rnr_retry` of 6 only on the RC path, so SRD uses the device default. On
  EFA a write to a queue pair with no matching receive took about 4 s to
  fail, which also widens the window in issue 1. Set it explicitly and
  document the value.
- **Failed writes flood the log.** Each failed completion logs `kv-sink:
  write failed - ...` with no region ID, which came to 28 identical lines per
  failed fetch on EFA. Log once per command with the region ID and a count.
