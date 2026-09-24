# Track A -- Transport: goals and acceptance criteria

Read [exercise-goal.md](exercise-goal.md) for why, then
[system-design.md](system-design.md) for how the tracks fit together. This
document is what Track A is responsible for and how we will know it is done.

## What this track is for

Get KV cache bytes out of an Aerospike cluster and into the engine's pinned
host buffer over RDMA, and say **when each layer is complete**.

The second half is the part that is easy to undervalue. A transport that
moves the bytes quickly but can only report "all done" destroys the overlap
the whole project exists to create. Per-layer completion reporting is the
deliverable, not a nicety on top of it.

## Scope

You own:

- `csrc/storage_backends/aerospike/`: `rdma_context`, `kv_sink_client`,
  `kv_sink_fanout`, `pipelined_fetch_session`, `pipelined_fetch_issue`,
  `notification_depth`, `connector_pipelined_rdma`, `l1_rdma_registration`
- `lmcache/v1/distributed/l2_adapters/` where the adapter exposes the above
- The `kv-sink` wire protocol on both sides, including the Aerospike server
  branch
- `docs/design/v1/distributed/l2_adapters/aerospike_rdma.md`

You do not own: slot planning (Track C decides *what* to fetch, you fetch it),
the pump, or anything touching a GPU.

## What you implement

`lmcache/v1/layerwise/contract.py::LayerArrivalSource`. That file is frozen;
if it is wrong, say so and we change it together rather than working around it.
Open blockers and questions raised so far are in
[track-a-questions-for-track-c.md](track-a-questions-for-track-c.md).

When it does change, the change is written up in
[contract-changes.md](contract-changes.md) -- what moved, what breaks, and
why the old shape was wrong. Read it before picking the contract back up
after a gap. Two entries there affect Track A today: `SlotPlacement` gained
`plane` and `piece`, and `LayerFetchPlan` gained `node_names`.

## Acceptance criteria

### A1. The contract is implemented and passes the conformance suite

The Aerospike adapter satisfies `LayerArrivalSource`, and
`tests/v1/layerwise/` passes against it, not only against
`ScriptedLayerArrivalSource`.

### A2. Layer completion is exact, in both directions

`poll_layer` returns `RESIDENT` only once **every** slot carrying part of that
layer has landed, and it returns `RESIDENT` promptly once they have.

Both halves need a test. Reporting a layer ready one slot early is silently
wrong model output; reporting it late costs exactly the overlap we are here to
buy. Test with a layer split across several slots on several nodes, and with
slots arriving out of order.

### A3. A stale generation cannot be credited to a live fetch

A write from an abandoned fetch that lands after the next fetch has begun must
not move the new fetch's accounting. Test it by abandoning mid-fetch, starting
a new fetch, and delivering a late write from the old generation. This is the
single most dangerous failure in the system because it corrupts output without
raising anything.

### A4. Declined slots surface as `UNSERVABLE`, never as a hang

If a node refuses a sink or a record is missing, the affected layer reports
`UNSERVABLE`. On `rnr_retry = 7` over RC the alternative is infinite retry and
a permanently wedged region, so this path needs a test that actually declines,
not a code reading.

### A5. Command chunking respects the advertised cap

A node accepts at most `max_sinks` sinks per command, advertised in the
`kv-sink-register` reply and defaulting to 256 when absent. Fetches larger
than that are split across commands and reply accounting is per command, not
per node. Test a fetch that needs several commands on one node, and pin the
register-reply parse against the **exact byte string the real server emits**,
field order included -- a parser that works on a reordered mock and fails on
the server is the bug this criterion exists to catch.

### A6. The receive queue is sized from the device, not from hope

Receive depth is derived from the plan (you know how many slots you asked for)
and clamped to the device's `max_qp_wr` from `ibv_query_device`. A plan that
cannot fit is rejected at `begin_fetch` with a clear error rather than
deadlocking at runtime.

### A7. The EFA question is answered with hardware

**This is the highest-value item on the track and it is not a coding task.**

Determine whether EFA consumes a receive work request per
`RDMA_WRITE_WITH_IMM` immediate. Write a small `ibv_wr_rdma_write_imm`
loopback test, run it on a real EFA instance, and report what happens when
more immediates arrive than there are posted receives.

If EFA does consume a receive WR per immediate, the frozen wire contract may
need a handshake it does not currently have, and that changes Track C's plan
format. The sooner this is known the cheaper it is. Do this before polishing
anything else.

Note `EFADV_DEVICE_ATTR_CAPS_UNSOLICITED_WRITE_RECV` exists precisely to avoid
consuming a receive buffer but is absent from the `efadv.h` available on the
dev box, so this cannot be settled by reading headers.

### A8. It runs against a real server

An end-to-end fetch against a real Aerospike server returns byte-exact
payload. Soft-RoCE over `rxe0` is sufficient for correctness and needs no
special hardware; see the setup section of
[aerospike_rdma.md](../distributed/l2_adapters/aerospike_rdma.md). Real EFA is
required only for A7.

### A9. No GPU anywhere in your test suite

If a Track A test needs a GPU, the split has broken. Use
`RecordingLayerLoadSink` as the far side.

## How to work without the rest of the system

`RecordingLayerLoadSink` is a complete stand-in for Track B and enforces the
ordering rules, so wiring mistakes fail on your laptop. For plans, build
`LayerFetchPlan` directly rather than waiting on Track C's planner -- the plan
is a plain frozen dataclass for exactly this reason.

Device-free logic tests run with `make -C tests/v1/distributed/rdma
logic-test`. The fabric tests need `RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1`.

Working from Windows: everything except A7 and A8 runs in an Ubuntu VM, set up
in
[rdma_testing_on_windows.md](../distributed/l2_adapters/rdma_testing_on_windows.md).

## Done

A1--A6, A8 and A9 are green, and A7 has a written answer backed by a run on
real hardware -- including the answer "we could not get EFA access", which is
a project risk to escalate rather than a task to quietly drop.
