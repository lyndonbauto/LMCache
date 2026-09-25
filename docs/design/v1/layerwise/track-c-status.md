# Track C -- status and remaining work

What Track C can do now, what waits on another track, and why. Updated as
items land; the acceptance criteria themselves are in
[track-c-acceptance.md](track-c-acceptance.md), and the decisions behind the
items are in [system-design.md](system-design.md) section 11 and
[contract-changes.md](contract-changes.md).

## Acceptance criteria

| Criterion | State |
|---|---|
| C1 plans from real requests | Done, except the production `ChunkPlacer` (Track A) |
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

1. **Request-level orchestration.** One function that, for one retrieve,
   leases a window from the placer, plans the fetch, runs the pump, and
   releases the window as finished or abandoned -- with a single error
   handler, so the caller falls back on any `LayerwiseContractError`
   (including `PlanTooLargeError`) wherever it was raised. Tested with fakes
   for the placer, source and sink.
2. **A per-request `ChunkPlacer`.** Today `locate()` is called per object,
   but the window lease is per request. The placer becomes `lease(objects) ->
   WindowLease`, the lease answers `locate()` with window-relative offsets and
   is released with an explicit outcome. Track A implements it; we define it
   and validate what it returns (inside the window, no overlaps).
3. **Window sizing input for Track A.** A helper giving the bytes a pipelined
   retrieve of `n` chunks needs for a registered model, respecting sliding
   windows and skipping aux groups, so the connector can size `window_bytes`
   from the KV layout.
4. **PR split plan** for upstreaming this branch (below).

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
  called from production code.
- **One flattener or two.** Track A's `NativePlanIssuer` flattens the plan
  itself, duplicating `pipelined_fetch_arguments`. Keep one.
- **A declined slot that later lands.** Proposed: the layer stays
  `UNSERVABLE`. Once agreed, pin it in the conformance suite.

## Blocked on other tracks

| Item | Waiting on | Owner |
|---|---|---|
| Aerospike source in the conformance suite | Fabric-free `ArrivalDriver`, which needs a test-only binding feeding the real native session | Track A |
| Production `ChunkPlacer` / leases | Allocator reservation PR (with all-or-nothing delete), lease API, publishing every window, window sizing | Track A |
| Pipelined retrieve enabled for real | A working `LayerLoadSink`; Track B's branch has only the stub (`layerwise_sink.py`, 2026-09-22) | Track B |
| Hooking orchestration into `retrieve` | The fetch-start decision above | Us + Track A + storage-manager maintainers |
| Removing the chunk-level path (`chunk_fetch_arguments`, `issue_pipelined_fetch_by_keys`, `StorageManager.begin_pipelined_fetch`) | Track A on the slot-level path and rebased onto this branch | Track A |
| C9 over Soft-RoCE | All of the above, plus a server built from the `kv-sink` branch | All three |

## PR split for upstream

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
5. **Conformance suite and native flattening.** `ArrivalDriver`,
   `test_arrival_source_conformance.py`, `native_fetch.py`.
6. **Request-level orchestration.** Item 1 and 2 above.

Design docs travel with the PR whose code they describe.
