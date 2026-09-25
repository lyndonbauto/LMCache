# Track C -- status and remaining work

What Track C can do now, what waits on another track, and why. Updated as
items land; the acceptance criteria themselves are in
[track-c-acceptance.md](track-c-acceptance.md), and the decisions behind the
items are in [system-design.md](system-design.md) section 11 and
[contract-changes.md](contract-changes.md).

## Acceptance criteria

| Criterion | State |
|---|---|
| C1 plans from real requests | Done, including the lease interface; the production `ChunkPlacer` is Track A's |
| C2 disjoint K/V ranges | Done |
| C3 sliding windows | Done |
| C4 request-scoped slot indices | Done |
| C5 slot space | Done |
| C6 out-of-order pump | Done |
| C7 failure paths abandon both sides | Done |
| C8 conformance suite | Done for the scripted source; Aerospike source pending (see blocked) |
| C9 end to end | Blocked (see below) |
| C10 no hardware in tests | Holds |

## Doable now

All done (2026-09-25):

1. **Request-level orchestration.** Done: `run_pipelined_retrieve` in
   `pipelined_retrieve.py`. One call leases, plans, pumps and releases, and
   every refusal is a `LayerwiseContractError`. Its tests use the real pump
   with the scripted source and recording sink, covering every exit (system
   design section 11 has the table). 23 of 23 deliberate mutations of the
   orchestration and window checks are caught.
2. **A per-request `ChunkPlacer`.** Done: `ChunkPlacer.lease(objects) ->
   WindowLease`, released with a `LeaseOutcome`. The builder rejects
   placements outside the window or overlapping. Track A implements the
   production placer against it.
3. **Window size check for Track A.** Done:
   `FetchModel.request_bytes(num_chunks, align_bytes)`. `window_bytes`
   stays in config, since the windows exist before any layout does; Track A
   checks it against this at registration (L4).
4. **PR split plan** for upstreaming this branch (below).
5. **Loader conformance suite.** Done: `test_load_sink_conformance.py`,
   `LoadObserver` and `SINK_HARNESS_FACTORIES`, running against the
   recording sink. Track B registers its loader there (see
   [contract-changes.md](contract-changes.md)).
6. **Fetch-start proposal.** Written:
   [fetch-start-proposal.md](fetch-start-proposal.md). Waiting on review.

## Needs a decision

- **Where a pipelined fetch starts (new, found 2026-09-25).** Today the
  lookup's prefetch (`StorageManager.submit_prefetch_task`) loads L2 hits into
  L1, and `retrieve` only copies L1 to the GPU
  (`read_prefetched_results`). Layer-by-layer delivery only helps if the
  lookup reports an L2 hit *without* loading it and the fetch starts at
  retrieve. That changes the lookup/prefetch protocol and `storage_manager`,
  which neither track owns, so it needs agreement with Track A and the
  storage-manager maintainers before the orchestration is hooked into
  `retrieve`. Until then the orchestration is complete and tested but not
  called from production code. **Proposal written:**
  [fetch-start-proposal.md](fetch-start-proposal.md) (report the remote hit
  at lookup, fetch at retrieve, fall back inside the daemon).
- ~~**One flattener or two.**~~ Decided with Track A: keep
  `pipelined_fetch_arguments`. `NativePlanIssuer` will call it and keep only
  its slot-count pre-check (Track A's change).
- ~~**A declined slot that later lands.**~~ Decided with Track A: the first
  reply for a slot is final, in both orders. It is pinned in the source
  suite (see [contract-changes.md](contract-changes.md)).
- ~~**Loader launch order.**~~ Raised by Track B: loads are strictly
  ascending, and a loader refuses any other order. Pinned in the loader
  suite.
- ~~**Offsets for windows past the first.**~~ Decided by Track A (P1):
  offsets are registration (slab) offsets, because one registration covers
  every window. Done: the lease reports `window_start()` and the builder
  checks `[window_start, window_start + window_bytes)`.
- ~~**Nodes per record (N1).**~~ Deferred to the client-server owner; the
  pipelined path is single-node only until then (see "Future work").
- ~~**What vLLM does when a retrieve fails mid-step (Track B's R5, the
  proposal's open question 1).**~~ Done:
  [vllm-load-failure.md](vllm-load-failure.md). Raising in attention kills
  vLLM's engine; failed blocks reported in the same step are recomputed
  under `kv_load_failure_policy: "recompute"`. Track B's connector must
  report instead of raising; the daemon fails a retrieve only by abandoning
  the sink.
- **Track B's questions, answered 2026-09-25** (details in
  [fetch-start-proposal.md](fetch-start-proposal.md) and
  [contract-changes.md](contract-changes.md)):
  - *Fallback vs. abandon:* neither abandon nor a new generation works; the
    fallback continues the same sink load. Done on the pump side:
    `run_resumable` abandons only the source and raises `LoadLeftOpenError`
    with the layers still to load. The fallback that uses it is PR 2.
  - *When live traffic reaches Track B's sink:* after the fetch-start
    proposal is accepted. PR 2 wires retrieve with the recording sink, PR 3
    swaps in Track B's.
  - *Reading objects still landing:* through `WindowLease.memory_obj`
    (added), which retrieve passes to the sink; not through L1 reads, which
    refuse write-reserved objects.
  - *Layout:* confirmed on the planner side and written into
    `LayerArrivalStatus.RESIDENT`. Track A should confirm the transport
    side (a slot lands exactly at its plan offset).
  - *Timeout order (R6):* the pump's default is now 2.5 s, below the
    worker's 5 s. A startup check needs the worker's value in the
    registration, which lands with C9.

## Future work

### Route each record to the node that holds it

**Owner:** the owner of the client-server interaction (not Tracks A, B or
C). Raised by Track A as N1; deferred 2026-09-25.

**The problem.** The writer splits an object into records
(`{cache_key}|s|{i}`), and Aerospike places each record by the digest of its
own key. So one object's records are spread across the cluster, as they
should be. The pipelined fetch, though, sends raw kv-sink commands straight
to a node rather than going through a normal client read, which routes each
key for you. Only the node that holds a record can write it into our window;
any other node declines. The planner currently sends all of an object's
records to one node.

**Interim (now).** Pipelined fetch is supported only against a single-node
cluster, where every record is on that node. On more nodes the pipelined
path must stay disabled and requests take the whole-object path. Nothing is
lost there except the pipelining.

**What a proper interface needs.** A way to fetch records into registered
memory without the planner knowing which node holds each one. The two shapes
discussed:

- **Batch reads.** The client issues the pipelined fetch as a batch and
  routes each record by its partition, as a normal batch get does.
- **Node-direct routing.** The planner asks a record-to-node lookup for each
  slot, e.g. the native `record_node(record_key)` over the client's partition
  map, and the session groups slots by node.

**What changes on our side when it lands.** Nodes move from objects to
slots: `ChunkLocation` and `ChunkPlacement` lose their node field, and the
placer only decides destinations. Either the planner sets
`SlotPlacement.node_index` per slot from the lookup, or, with batch reads,
nodes leave the plan altogether. The contract's `LayerFetchPlan.node_names`
is the part that would change.

## Blocked on other tracks

| Item | Waiting on | Owner |
|---|---|---|
| Aerospike source in the conformance suite | Fabric-free `ArrivalDriver`, which needs a test-only binding feeding the real native session | Track A |
| Production `ChunkPlacer` / `WindowLease` | Allocator reservation PR (with all-or-nothing delete), publishing every window, checking `window_bytes` with `request_bytes` at registration | Track A |
| Pipelined retrieve enabled for real | A working `LayerLoadSink`; Track B's branch has only the stub (`layerwise_sink.py`, 2026-09-22) | Track B |
| Hooking orchestration into `retrieve` | The fetch-start decision above | Us + Track A + storage-manager maintainers |
| Removing the chunk-level path (`chunk_fetch_arguments`, `issue_pipelined_fetch_by_keys`, `StorageManager.begin_pipelined_fetch`) | Track A on the slot-level path and rebased onto this branch | Track A |
| C9 over Soft-RoCE | All of the above, plus a server built from the `kv-sink` branch | All three |

## PR split for upstream

**What upstream already has (checked 2026-09-25 against `dev` at
`57afd4c0`).** The native Aerospike L2 adapter is upstream (#3458). The
foundation this branch sits on is not upstream and not in review: RDMA
registration and the pipelined fetch session, plane-aligned record sharding,
and multiprocess layerwise load (`layer_progress.py`, per-layer H2D, the vLLM
per-layer wait). So:

- **PR 1 stands alone.** It is prepared on branch
  `fix/aerospike-libyaml-soname`, based on `dev`:
  - the libyaml soname fix, with a build-profile test that fails on `dev`'s
    profile;
  - the registry-test unflake, which reproduces on `dev`.

  The empty-`layer_indices` fix is dropped from it, because `dev` has no
  `layer_indices`.
- **The layerwise package stands alone too.** Copied onto `dev`, all 254
  tests in `tests/v1/layerwise/` pass once
  `native_connector_l2_adapter._object_key_to_string` is made public.
  Nothing in production would call it yet.
- **PR 2 and the wiring half of PR 4 need the foundation first.** These are
  the per-kernel-group record layouts, fetch-model registration in
  `register_kv_cache`, and `max_record_bytes()`.

**Decided 2026-09-25:** PRs 2 onward stay on the fork until all three
tracks finish, and then go upstream on top of the foundation.

Each PR is independently reviewable and leaves `dev` working. Order matters:
later ones build on earlier ones.

1. **Build and CI fixes.** libyaml soname linking and the CI check;
   omitting empty `layer_indices`; the order-independent registry-test stub.
   No layerwise code.
2. **Hybrid-model record layouts.** Per-kernel-group plane runs in the writer
   and the `runs` meta bin; the parity harness.
3. **Layerwise contract and planner.** `contract.py`, `planner.py`,
   `fakes.py`, `pump.py`, with the keys contract and the slot ceiling.
4. **Planning from real requests (C1).** `request_fetch.py`, registration
   of fetch models, `max_record_bytes()`.
5. **Conformance suites and native flattening.** `ArrivalDriver`,
   `LoadObserver`, `test_arrival_source_conformance.py`,
   `test_load_sink_conformance.py`, `native_fetch.py`.
6. **Request-level orchestration.** `pipelined_retrieve.py`, the lease
   interface in `request_fetch.py`, `request_bytes`.

Design docs travel with the PR whose code they describe.
