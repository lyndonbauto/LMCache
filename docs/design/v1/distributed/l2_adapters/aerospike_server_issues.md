# Aerospike server issues found from the LMCache side

Defects and contract gaps in the aerospike-server branch
`sriram/kv-sink-batch-prio` and the matching C client branch
`sriram/kv-sink-batch-prio` of `aerospike-client-c-kvsink` (based on 7.5.0),
found by reading both against the LMCache Aerospike connector and by running
LMCache's A8 suite against them on Soft-RoCE (RC, 2026-09-30) and EFA v2
(SRD, 2026-10-01), at server `24357d20d` and client `061f79b7`. Issues 3 and 11
were reproduced on Soft-RoCE; the rest are from review. On EFA, with the client
built by LMCache's script, the suite passed and the server logged no sink
errors.

**Status re-checked by review at server `046e8558d` and client `523d51ea`
(2026-10-01).** Issues 2, 3, 7, 8, 10 and 11 and most minor items are fixed;
issue 1 is partly fixed; 4, 5, 6 and 9 remain. These fixes have not been run
through the A8 suite yet. The test setups are in
[`rdma_testing_on_windows.md`](rdma_testing_on_windows.md) and
[`rdma_testing_on_efa.md`](rdma_testing_on_efa.md).

This is a hand-off list for the Aerospike server and client teams. Each entry
says what is wrong, what LMCache sees, and what fix we expect. Bugs in
LMCache's own code are fixed in LMCache and not listed here.

**What changed since the last list.** The branch replaces the info-command
data path (`kv-sink-fetch-pipelined`, write-with-immediate) with a sink field
on ordinary reads: `as_batch_read_record.sink` and `aerospike_key_get_into()`
send field 46 with `(region, offset, length, priority)`, the server queues the
placement per region by priority, shares the link across regions by deficit
round robin, and replies to the row once its RDMA write has completed. The
issues on the previous list (record released during the send, EFA write
detection, the crash after a refused device, fenced replies, lazy stripe
registration, fixed device and GID, concurrent fetches on one region, leaked
regions, missing read-touch) are fixed on this branch. Issues 3 and 9 below
are what remains of the old "failed write disables the region" and "busy-spin"
entries.

**This was a protocol change for LMCache.** `kv-sink-fetch-pipelined` no
longer exists and writes carry no immediate. LMCache now issues batch reads
with sink rows through the patched client; see
[`aerospike_rdma.md`](aerospike_rdma.md).

| # | Issue | Severity | Where | Status |
|---|---|---|---|---|
| 1 | [The local transport writes into any process on the server host](#1-the-local-transport-writes-into-any-process-on-the-server-host) | Security: remote memory write | Server and client | Partly fixed: opt-in, no peer check |
| 2 | [Queued writes have no deadline](#2-queued-writes-have-no-deadline) | Data corruption | Server | Fixed in `046e8558d` |
| 3 | [A failed RC write breaks the region with the wrong error](#3-a-failed-rc-write-breaks-the-region-with-the-wrong-error) | Silent permanent fallback | Server | Fixed in `046e8558d` |
| 4 | [Region ownership is self-declared](#4-region-ownership-is-self-declared) | Security: silent wrong data | Server | Open |
| 5 | [Only single-blob-bin records up to 2 MiB can be sink-read](#5-only-single-blob-bin-records-up-to-2-mib-can-be-sink-read) | Contract gap | Server | Open; limit now advertised |
| 6 | [Placement runs on the completion poller](#6-placement-runs-on-the-completion-poller) | Throughput ceiling | Server | Open |
| 7 | [Sink reads skip duplicate resolution, ping and filters](#7-sink-reads-skip-duplicate-resolution-ping-and-filters) | Stale reads under strong consistency | Server | Fixed in `046e8558d` (refused) |
| 8 | [RoCE hop limit is 1](#8-roce-hop-limit-is-1) | No routed RoCEv2 | Server and client | Fixed in `046e8558d` and `18b68325` |
| 9 | [The poller never sleeps when idle](#9-the-poller-never-sleeps-when-idle) | CPU | Server | Open |
| 10 | [One registration failure fails the whole sink](#10-one-registration-failure-fails-the-whole-sink) | Availability | Client | Fixed in `18b68325` |
| 11 | [`libaerospike.so` is not linked against libibverbs](#11-libaerospikeso-is-not-linked-against-libibverbs) | RDMA unusable through the shared library | Client | Fixed in `18b68325` |

Smaller items are under [Minor](#minor).

## 1. The local transport writes into any process on the server host

**Where:** `local_reg` and `write_to_peer` in `as/src/base/kv_sink_local.c`;
`choose_transport` in the client's `src/main/aerospike/as_sink.c`; the
client `Makefile`.

The `local` transport is always compiled in and always accepted by
`kv-sink-register`. The client supplies `pid` and `addr`, and the server later
calls `process_vm_writev(pid, ...)` with record bytes at that address. The
only check is that the process exists:

```c
if (kill((pid_t)pid, 0) != 0) {
	as_info_respond_error(db, AS_ERR_PARAMETER, "no such peer process");
	return NULL;
}
```

So any client allowed to run `kv-sink-register` (`PERM_RECORD_INFO`) can name
`asd`'s own PID, or any process the server's user can ptrace, and write
chosen bytes - it controls the record contents too - at a chosen address.
`asd` usually runs as root. The region's "peer" is the declared PID, which
proves nothing.

The client makes this reachable by accident:

- `choose_transport` falls back to `local` silently when no RDMA device is
  found. A client on another host then registers its own PID and address, and
  the server writes into whatever process on *its* host has that PID.
- The client `Makefile` defines `AS_SINK_VERBS` only when
  `infiniband/efadv.h` exists, so a RoCE host without the libefa headers
  builds with `local` as its only transport, and RC is never available there.

**What LMCache sees:** on a host without an RDMA device, or built without
`efadv.h`, reads report OK while nothing lands in L1, or land in another
process on the server host.

**Expected fix:**

- Server: accept `transport=local` only when explicitly enabled (build flag
  or config item, off by default), and when enabled, only from a loopback
  connection whose peer credentials match the PID.
- Client: no silent fallback. With no RDMA device, `aerospike_sink_create`
  fails unless the caller asked for `local`.
- Client: define the verbs transport when `infiniband/verbs.h` is present, and
  gate only the SRD code on `efadv.h`.

**Status: partly fixed.** The server refuses `transport=local` unless `asd`
runs with `KV_SINK_ENABLE_LOCAL=1` (`046e8558d`). The client no longer falls
back: with no RDMA device `aerospike_sink_create` fails unless the caller
asked for `local`, and the Makefile gates verbs on `verbs.h` and SRD on
`efadv.h` (`18b68325`). Still open: with the variable set, the server does not
check that the PID belongs to the connecting peer, so the switch must stay off
anywhere a client can reach `asd` from another host.

## 2. Queued writes have no deadline

**Where:** `kv_sink_op` in `as/include/base/kv_sink.h`; `sched_pick`, `drain`
and `place` in `as/src/base/kv_sink.c`; `read_sink` in
`as/src/transaction/read.c`.

A sink read is queued on its region and placed when the scheduler reaches it.
Nothing in the op records the transaction's deadline, and neither the
scheduler nor `place` checks `end_time`. A write waiting behind other regions
(the scheduler shares the link by bytes), or behind a stalled write, is
performed however late it is.

```text
client: batch row times out ──▶ LMCache marks the slot failed, reuses the L1 memory
server: op reaches the head ──▶ RDMA write into that memory ──▶ reply nobody reads
```

**What LMCache sees:** silent corruption of whatever now occupies the slot.
The only thing that stops a queued write today is deregistering or replacing
the region.

**Expected fix:** carry the transaction deadline in `kv_sink_op`, and fail an
op with `AS_ERR_TIMEOUT` instead of placing it once the deadline has passed.
Document that a row's destination may still be written until the server-side
timeout, so clients can keep their own timeout longer than it.

**Status: fixed in `046e8558d`.** Each op carries the transaction's
`end_time`; one past it fails with `AS_ERR_TIMEOUT` instead of being placed. A
write already posted to the NIC cannot be recalled and may land just after the
deadline. LMCache sets the batch `total_timeout` to its fetch timeout, so the
server deadline equals the client's, and the window leaser's quarantine of
`fetch_timeout_seconds` absorbs the in-flight tail.

## 3. A failed RC write breaks the region with the wrong error

**Where:** `run_poller` and `verbs_post` in `as/src/base/kv_sink_verbs.c`;
`drain` in `as/src/base/kv_sink.c`.

On RC, one failed write (retry exhaustion, a remote access error) moves the
region's queue pair to the error state. The poller reports the op as failed
and nothing else happens: the region stays registered, every later write on
it is flushed or refused, and every later read fails with the generic
`AS_ERR_UNKNOWN`.

The client cannot recover:

- the error is not `AEROSPIKE_ERR_SINK_UNKNOWN_REGION` (220), so the documented
  "refresh and retry" path is not taken;
- `aerospike_sink_refresh()` probes with `kv-sink-touch`, which still
  succeeds on the broken region;
- the idle reaper never reclaims it, because the failing reads count as use.

**What LMCache sees:** after one bad write, every sink read on that node fails
until the client process restarts, and every retrieve falls back to
whole-object loads with no error telling it to register again.

**Expected fix:** when a completion fails or a post fails on an RC region,
take the region out of the registry (`region_remove`). Queued ops then fail
with 220, the next read gets 220, and the client's refresh re-registers it.
`kv-sink-touch` should also fail on a region whose queue pair is not in RTS.

**Reproduced** on Soft-RoCE while chasing issue 11: one remote access error,
then every later write on the region `Work Request Flushed Error`, until the
client closed.

**Status: fixed in `046e8558d`.** A failed completion or a refused post calls
`region_failed()`, which removes the region; the row and every later row on
it fail with 220, and LMCache's existing refresh-and-retry re-registers it.
The server logs a failed write once per region.

## 4. Region ownership is self-declared

**Where:** `as_kv_sink_register_cmd` in `as/src/base/kv_sink.c`; `verbs_reg`
in `as/src/base/kv_sink_verbs.c`.

Re-registering an existing region ID replaces it, and the check that only its
owner may do so compares the stored "peer" with the new one. For verbs the
peer is the GID the client puts in the register command. The server does not
verify it, and every process on a host shares that host's GID.

Anyone who learns a region ID - it is sent in clear text in every read unless
TLS is on - can register again with the victim's GID and their own
`addr`/`rkey`. The victim's regions are marked dead and replaced; its next
reads succeed and land in the attacker's buffer.

**What LMCache sees:** reads report OK, L1 holds whatever was there before,
and the engine uses it as a cache hit.

**Expected fix:** bind a region to the authenticated connection or user that
registered it, not to a declared address. At minimum, return a registration
secret in the register reply and require it to replace or deregister the
region.

## 5. Only single-blob-bin records up to 2 MiB can be sink-read

**Where:** `place` and `as_kv_sink_prepare` in `as/src/base/kv_sink.c`.

`place` refuses any record that does not have exactly one bin of blob type
(`AS_ERR_INCOMPATIBLE_TYPE`), and `as_kv_sink_prepare` refuses any length over
`KV_SINK_MAX_VALUE_SZ`, 2 MiB, the size of a staging slot.

LMCache's segment records fit (one bin, `b`). Small objects do not: when an
object fits one record, LMCache stores the payload inline in the meta record,
next to eight or nine metadata bins (`put_meta_record` in
`csrc/storage_backends/aerospike/connector.cpp`).

**What LMCache sees:** every small object fails its sink read with
`AS_ERR_INCOMPATIBLE_TYPE` and has to be loaded over the socket; any segment
larger than 2 MiB fails with `AS_ERR_PARAMETER`.

**Expected fix:** let the sink field name the bin to place (for example, an
optional bin-name field next to field 46), so a multi-bin record can be
sink-read. Advertise the maximum value size in the register reply instead of
leaving it as a compile-time constant the client must know.

**Status: open; the limit is now advertised.** The register reply carries
`max-value=<bytes>` and the client exposes the smallest across nodes as
`as_sink.max_value_size` (`046e8558d`, `18b68325`). The single-blob-bin rule
is unchanged, so inline small objects still fall back.

## 6. Placement runs on the completion poller

**Where:** `drain`, `place` in `as/src/base/kv_sink.c`; `run_poller`,
`verbs_post` in `as/src/base/kv_sink_verbs.c`.

`drain` starts the next ops on whichever thread calls it. When a write
completes, that is the single poller thread, which then looks up the next
record, loads its bins (device I/O on an SSD namespace) and copies up to
2 MiB into a staging slot under the record lock, before it polls again. Every
completion on the node waits behind that work. All of it also runs under one
global scheduler mutex (`g_sched_lock`), which every sink read on every
service thread takes.

**What LMCache sees:** a per-node ceiling of roughly one core's memcpy rate,
and completions delayed behind storage reads. Not yet measured.

**Expected fix:** have the poller only retire completions and hand placement
to a worker pool (or the service threads), and split the scheduler lock per
region, with a separate lock for the global budget and the active ring.

## 7. Sink reads skip duplicate resolution, ping and filters

**Where:** `read_sink` in `as/src/transaction/read.c`.

`read_sink` is taken before `read_must_duplicate_resolve` and `read_must_ping`,
and it never applies the request's filter expression. The comment marks it as
a proof of concept.

**What LMCache sees:** nothing today: LMCache keys chunks by content hash and
does not send filters. On a strong-consistency namespace, though, a sink read
can return a value that a linearizable read would not.

**Expected fix:** refuse sink reads on strong-consistency namespaces with a
clear error until the full read path is supported, and refuse or apply filter
expressions rather than ignoring them.

**Status: fixed in `046e8558d`.** Sink reads on a strong-consistency namespace
or with a filter expression fail with `AS_ERR_UNSUPPORTED_FEATURE`. LMCache
uses neither.

## 8. RoCE hop limit is 1

**Where:** `to_rts` and `peer_ah_get` in `as/src/base/kv_sink_verbs.c`;
`rc_node_connect` and `srd_node_connect` in the client's
`src/main/aerospike/as_sink_verbs.c`.

Both sides build their address vectors with `grh.hop_limit = 1`. RoCEv2
packets are IP packets, and a hop limit of 1 is dropped at the first router.

**What LMCache sees:** registration succeeds, then every write fails with a
retry-exceeded completion (and issue 3 follows) whenever client and server are
in different L3 subnets.

**Expected fix:** use 64 (the usual value), or make it configurable next to
the device and GID index.

**Status: fixed** in server `046e8558d` and client `18b68325`: both use 64.

## 9. The poller never sleeps when idle

**Where:** `run_poller` in `as/src/base/kv_sink_verbs.c`.

After 10,000 empty polls the loop sleeps `usleep(10)` and polls again, and
`n_idle` never falls back below the threshold until a completion arrives. An
idle server therefore wakes the poller tens of thousands of times a second
(100,000 at most, less by the timer slack), forever.
It no longer holds an info thread, as the old reap loop did, but it still
costs a core's worth of scheduler work on a node that is not serving sinks.

**Expected fix:** after a short busy-poll, arm a completion channel
(`ibv_req_notify_cq`) and block in `ibv_get_cq_event`.

## 10. One registration failure fails the whole sink

**Where:** `register_missing`, `register_node` and `aerospike_sink_create` in
the client's `src/main/aerospike/as_sink.c`.

`register_missing` returns an error if any node fails, and
`aerospike_sink_create` then destroys the sink, even if every other node
registered. When the register command succeeds but the client's RC connect
fails, `register_node` releases its own queue pair but never sends
`kv-sink-deregister`, so the server keeps that region until the idle timeout.

**What LMCache sees:** one unreachable or misconfigured node disables RDMA for
the whole cluster, and repeated attempts leave server-side regions behind.

**Expected fix:** let `aerospike_sink_create` succeed with a per-node result
(rows sent to an unregistered node already fail with 220, which the caller
handles), and deregister on the server when the client-side connect fails.

**Status: fixed in `18b68325`.** `aerospike_sink_create` succeeds when at
least one node registered; `err` then still holds the last node's failure,
and `nodes[].registered` says which nodes have it. A failed client-side
connect sends `kv-sink-deregister`. LMCache logs a partial registration and
otherwise relies on its 220 retry.

## 11. `libaerospike.so` is not linked against libibverbs

**Where:** the `library` rule in the client's `project/settings.mk`, and the
`AS_SINK_VERBS` block of its `Makefile`.

With `AS_SINK_VERBS` on, the shared library calls `ibv_*` but is linked with
plain `cc -shared` and no `-libverbs -lefa`. Its `ibv_*` references therefore
carry no symbol version:

```text
$ objdump -T libaerospike.so | grep ' ibv_reg_mr$'
0000000000000000      D  *UND*  0000000000000000  Base        ibv_reg_mr
```

At runtime the dynamic linker binds an unversioned reference to
libibverbs' oldest version, `ibv_reg_mr@IBVERBS_1.0`, a compatibility entry
point that returns the legacy `struct ibv_mr_1_0`. `as_sink_verbs.c` reads it
as a current `struct ibv_mr`, so the descriptor sent in `kv-sink-register`
carries a garbage rkey. Traced on Soft-RoCE: the kernel registered the buffer
as rkey 7248, the client advertised 961, `lookup_mr` found nothing, and the
server's first write failed with a remote access error (then issue 3).

The examples do not see this because they link the static `libaerospike.a`
together with `-libverbs`. Any application that links the shared library
does, whatever it links itself.

**What LMCache sees:** every pipelined fetch falls back to whole-object
loads; `aerospike_sink_create` and registration report success.

**Expected fix:** add `-libverbs -lefa` to the shared-library link when
`AS_SINK_VERBS` is set, after the objects or under `-Wl,--no-as-needed`,
since the rule places `LDFLAGS` before the objects and Ubuntu links
`--as-needed` by default. The result must show
`(IBVERBS_1.1) ibv_reg_mr` and `NEEDED libibverbs.so.1`.

**Status: fixed in `18b68325`.** The Makefile appends `-libverbs` (and
`-lefa` when `efadv.h` exists) after the objects of the shared-library link;
`examples/kv_sink` gained `sinktest-so` to check it. LMCache's build script no
longer adds the flags itself but still refuses a library that lacks the
versioned binding.

## Minor

Open:

- **The server links `-libverbs -lefa` unconditionally** (`as/src/Makefile`),
  so it cannot build or start on a host without libefa, even with RDMA unused.

Fixed in server `2fb7e6084`/`046e8558d` and client `18b68325`:

- The stale "payload is never copied" header comment in `kv_sink_verbs.c`,
  and the unused `as_storage_stripe_region()`.
- Unseeded PSNs: both sides now draw them from `getrandom`.
- Failed writes log once per region instead of once per completion.
- `aerospike_key_get_into` checks `sink` for NULL.
- `rc_init` no longer grants `IBV_ACCESS_REMOTE_READ`.
- `as_sink.h` documents its threading rule: reads naming a sink may run on any
  thread; `aerospike_sink_refresh`, `aerospike_sink_reregister` and
  `aerospike_sink_destroy` must not run concurrently with each other. LMCache
  serializes refreshes under one mutex and destroys only after its workers
  stop.
