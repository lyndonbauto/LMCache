# Aerospike and Aerospike RDMA changes

Everything on this fork that is specific to the Aerospike L2 backend. Most of
it serves the upcoming **Aerospike RDMA pipelined fetch**: the Aerospike
server writes KV records straight into LMCache's pinned L1 over RDMA, and
LMCache starts loading layer *i* into vLLM as soon as layer *i*'s records
land.

Companion files:

- [bug_fixes.md](bug_fixes.md): fixes to existing code. It also gives the
  baseline and the diff recipe.
- [general_features.md](general_features.md): backend-agnostic features. MP
  layerwise load (GF-1 to GF-6) is the consumer this path feeds.

Baseline: fork `prototype-stage1` (`49f18d12`) vs upstream `dev`
(`8d7d2c47`), merge base `caba24c2`. Upstream already has the plain native
Aerospike L2 adapter (#3458); everything below builds on it.

**Updated for the `prototype-stage-1a` PR.** This file describes the branch
*after* that PR merges into `prototype-stage1`. That state is
`origin/prototype-stage1` (`0a339c63`) plus the PR's six commits
(`62b384d6` to `61d39ffa`). The merge has two small conflicts:
`aerospike_concurrent_writes.md` and
`tests/v1/distributed/rdma/csrc/fabric_free_session_pybind.cpp`. See
[What the `prototype-stage-1a` PR changes](#what-the-prototype-stage-1a-pr-changes)
below. Since then the PR has gained `f91c1972`, `b340c819` and `934052cf`:
they adapt to server `046e8558d` and client `523d51ea`, which fix most of the
new server issues (Part H).

**PR isolation check (2026-10-01).** Each item was re-checked against
upstream `dev`. The question: would it build, pass its own tests and do
something with only its stated prerequisites merged? The result is the
[PR plan](#pr-plan) at the end: 11 PRs, PR-A1 to PR-A11. Every item section
starts with a **PR** line. The main changes:

- **Only AS-1 and AS-2 stand as written.**
- **AS-5 splits** into single-node and cluster tests.
- **AS-3 needs a re-cut.** Its adapter imports the AS-L1 planner.
- **AS-4 splits** among three PRs, and part of it is dead code.
- **AS-R1, AS-R4, AS-R5 and AS-R6 become one native PR.**
- **AS-P1 and AS-P2 merge,** with GF-8 and GF-9.
- **AS-M1 to AS-M5 become one PR.**
- **Nothing in this file is user-reachable before PR-A11.** Nothing past
  PR-A7 can run end to end upstream until the kv-sink server and client ship.

No commit cherry-picks cleanly; cut each PR from the final file contents.

---

## What the `prototype-stage-1a` PR changes

The PR's main commit, `8993843e` ("Move pipelined RDMA fetch to the kv-sink
batch-read protocol"), changes who owns RDMA:

- **Before.** LMCache ran its own verbs stack and spoke the server's
  `kv-sink-fetch-pipelined` info command. The server signalled each slot
  with `RDMA_WRITE_WITH_IMM`.
- **After.** A fork of the Aerospike C client (`aerospike-client-c-kvsink`,
  branch `sriram/kv-sink-batch-prio`) owns the RDMA endpoint. LMCache
  registers its L1 windows once with `aerospike_sink_create`. Each layer is
  then an ordinary `aerospike_batch_read` whose rows name a destination
  (`sink`, `sink_offset`, `sink_length`, `sink_priority`). A row's result is
  its completion signal: no immediates, no receive queue, no CQ polling in
  LMCache. Rows are routed to each record's partition master, so **any
  cluster size works**.

**Items that are gone or no longer relevant once the PR merges:**

| Item (as written before) | What happens |
| --- | --- |
| AS-R2 `RdmaContext` (`rdma_context.{h,cpp}`, about 900 lines) | **Deleted.** The client fork owns the device, MR, QPs and CQ |
| AS-R3 kv-sink control plane (`kv_sink_client`, `kv_sink_fanout`, about 990 lines) | **Deleted.** Registration, touch and deregister are the client's business; LMCache builds no info command |
| AS-R4 `notification_depth.{h,cpp}` | **Deleted.** No receive queue to size |
| AS-R5 `pipelined_fetch_session`, `_pool`, `_issue`, `connector_pipelined_rdma` (about 1,630 lines) | **Replaced** by `connector_sink_fetch.{h,cpp}` (`AerospikeSinkFetchDriver`) and `sink_fetch_table.{h,cpp}` (`SinkFetchTable`), about 850 lines |
| AS-R6 mock writer, EFA immediate probe (A7), equivalence, pipeline, session, pool, issue and notification-depth harnesses | **Deleted.** Replaced by `sink_fetch_table_test` |
| T-RDMA-05 fanout probe (`862e8289`, already on `prototype-stage1`) | **Obsolete.** It drives the deleted `RdmaContext` and `register_all_nodes()` |
| "Single-node clusters only" (N1), in AS-P3, AS-P5, AS-L1 and AS-R5 | **Gone.** The node name in a plan is nominal |
| "Slot limit = 16 bits of the RDMA immediate" | **Reworded.** Same value (`MAX_SLOTS_PER_REQUEST = 0x10000`), now bounded by the native fetch table |
| Server issues 1 to 11 on `feat/kv-sink-fetch-pipelined` | **Fixed on the new server branch** per the updated `aerospike_server_issues.md`; replaced by 11 new ones (Part H) |
| Ledger D-07 (no overlap because replies are fenced) | **Obsolete.** Each layer's batch now returns on its own; overlap is possible but still unmeasured |
| Ledger D-08 (lazy stripe registration) | **Obsolete.** Fixed on the new server branch |
| Ledger D-12 (late completion past 256 slots) | **Very likely obsolete.** It was a bug in the old multi-command path; re-verify on the new protocol |
| Ledger D-11 (a wrong GID index is not named) | **Moves to the client fork**, which now opens the device; re-check |
| `BUILD_WITH_AEROSPIKE_EFA=1` | **A synonym** for `BUILD_WITH_AEROSPIKE_RDMA=1`; RC vs SRD is a run-time choice |

**Items that change:**

- **AS-R1 (build).** An RDMA build now needs the kv-sink C client fork,
  built by the new `.deps/build_aerospike_client_kvsink.sh`. The script
  clones from a personal GitHub account
  (`sriram588/aerospike-client-c-kvsink`). The build links `libibverbs` and
  `libefa` because the fork's transport calls them.
  `_require_kv_sink_client` refuses a stock client up front.
- **AS-P4 (arrival source).** It keeps its interface; only the docstrings
  changed. It also gains `read_write_ids`; see the next section.
- **Python layer.** It is otherwise unchanged; `8993843e` touched only
  docstrings, messages and the single-node wording.

**Already on `prototype-stage1` (not in the PR), landed after this file's
first version:**

- **D-14 (`e9cd0689`, design record `4cf34d8e`).** Per-write segment keys
  `<key>|s|<wid>|<i>`, a `wid` bin on the meta record, and create-only
  (`AS_POLICY_EXISTS_CREATE`) meta writes, so the first writer wins.
  - It fixes an upstream bug; see BF-8 in [bug_fixes.md](bug_fixes.md).
  - It changes AS-3's storage format.
  - The pipelined path gains `read_write_ids` (native, adapter and
    `LayerArrivalSource`) and `RecordKeys(write_ids)`.
- **3-node cluster tests (`9367ef65`).**
  `tests/v1/distributed/test_aerospike_cluster_integration.py` covers
  T-STO-08, T-FLT-02/03/04, T-FLT-10 and T-EVT-06, in 844 lines. It is gated on
  `RUN_AEROSPIKE_CLUSTER_INTEGRATION=1`. Add it to AS-5.
- **D-15 (`0ebf98d0`).** A test that no kv-sink write reaches L1 after
  `StorageManager.close()`. Its root cause sat in the deleted
  `AerospikePipelinedRdmaDriver::shutdown()`. With the client fork, the
  question becomes server issue 2 (queued writes had no deadline), which is
  now fixed in server `046e8558d`. Re-run the test on the new driver to close
  it.
- **D-17 (S1).** A layerwise retrieve that fails mid-forward, with
  recompute, gives wrong output. It was seen on the pipelined path. The
  leading hypothesis sits in the generic per-layer wait; see GF-5 in
  [general_features.md](general_features.md). It blocks any claim that
  failures are recoverable.

---

## How the pipelined fetch works (one paragraph and a diagram)

**Lookup.** At lookup, a pipelined-eligible request's L2 hits are *counted
but not loaded* (deferred: GF-10).

**Planning.** At retrieve, LMCache:

1. leases an RDMA window inside L1 (AS-P2, AS-P3);
2. places the request's objects in it;
3. plans one *slot* per Aerospike record, where each record lies inside one
   layer's plane (AS-3, AS-L1).

**Fetch.** LMCache then issues one kv-sink batch read per layer through the
kv-sink C client fork (AS-R5).

- Each row names a record and its destination: `sink_offset`, the record
  length, and the layer's ordinal as `sink_priority`.
- The server RDMA-writes the value into the window. It answers the row only
  once the write has completed.
- `SinkFetchTable` folds row results into per-layer readiness.

Every layer is in flight at once, but each node places lower layers first.

**Load.** `AerospikeLayerArrivalSource` (AS-P4) reports layers `RESIDENT` in
order. `LayerArrivalPump` (GF-12) feeds them to the MP layer sink (AS-M3),
which does per-layer H2D (GF-3) while vLLM waits per layer (GF-5).

**Fallback.** Any refusal or unservable layer falls back to a whole-object
load, or to recompute.

```
lookup ──► PrefetchController (deferred) ──► Session.deferred_keys
retrieve ─► run_pipelined_retrieve
             ├─ RdmaWindowLeaser.lease ─► RdmaWindowPlacer (L1 objects in window)
             ├─ FetchPlanner ─► LayerFetchPlan (slots: record → offset, layer)
             ├─ AerospikeLayerArrivalSource.begin_fetch
             │     └─ native: issue_pipelined_fetch_by_slots ─► one aerospike_batch_read
             │           per layer (rows carry sink offset, priority = layer)
             │           server ══RDMA WRITE══► L1 window; row OK ─► SinkFetchTable
             └─ LayerArrivalPump: poll_layer(i)=RESIDENT ─► sink.load_layer(i) ─► H2D(i)
```

**Ownership** (`docs/design/v1/layerwise/exercise-goal.md`,
`system-design.md`):

- **Track A** (Valentyn): transport, AS-R*, AS-P1 to AS-P4.
- **Track B** (DKZed): consumption, GF-1 to GF-5 and AS-M3's sink.
- **Track C** (lyndon): planning and junction, AS-L*, AS-M*, GF-10 to GF-12.

**Status (2026-10-01, after the PR).**

- **Correctness.** A8 passes 3/3 on Soft-RoCE (RC) and 3/3 on AWS EFA v2
  (SRD) against the new protocol. Landing is byte-exact.
- **Performance.** No overlap has been measured. It is possible now, because
  each layer's batch returns on its own, but A8 doesn't measure it.
- **Multi-node.** Allowed now, but not yet run.
- **Earlier results.** The T-RDMA-06 byte oracle and the Stage 3 GPU runs
  were on the old protocol.

**External dependencies.** Two private branches, both named
`sriram/kv-sink-batch-prio`: one of `aerospike-server`, and one of the
kv-sink fork of the Aerospike C client. LMCache's build script clones the
client from a personal GitHub account. **None of the RDMA parts (AS-R
onward) can be exercised upstream until the server and the client's sink
API ship in official Aerospike releases.** Expect this to be the main
upstream objection. The client dependency makes it worse than before: a
stock `libaerospike` can't build the RDMA path.

---

## Summary

| ID | Item | Size (+, incl. tests) | Needs RDMA? | Ships in |
| --- | --- | --- | --- | --- |
| AS-1 | Batch exists (one batch read per node) | ~+300 | No | PR-A1 (alone) |
| AS-2 | Read-touch TTL; lookups never touch | ~+150 | No | PR-A2, after PR-A1 |
| AS-3 | Plane-aligned, per-kernel-group record sharding (format change) | ~+1,300 | No | PR-A5, **re-cut** (neutral layout module; dead plane-bytes path dropped) |
| AS-4 | Record naming accessors (`record_digest_hex`, `record_node`, `max_record_bytes`, `read_write_ids`) | small | No | **Split:** `max_record_bytes` → PR-A5; `read_write_ids`, `record_digest_hex` → PR-A6; `record_node` dropped (unused) |
| AS-5 | Storage-integrity and 3-node cluster integration tests | ~+1,650 | No | **Split:** single-node → PR-A3; cluster → PR-A4 |
| AS-R1 | Opt-in RDMA build profile and kv-sink client build script | ~+190 | Build only | PR-A8 (combined) |
| ~~AS-R2~~ | ~~`RdmaContext`~~ | — | — | **Deleted by the PR** |
| ~~AS-R3~~ | ~~kv-sink control plane~~ | — | — | **Deleted by the PR** |
| AS-R4 | Pipelining model: layer readiness, slot planner | part of 2,356 | Logic only | PR-A8 (combined, trimmed) |
| AS-R5 | kv-sink batch-read fetch: `AerospikeSinkFetchDriver`, `SinkFetchTable`, pybind | part of 2,356 | Yes | PR-A8 |
| AS-R6 | C++ test harnesses (fabric-free only now) | ~+3,330 | No | **Split** by the code each tests: `shard_plan_test` → PR-A5; the rest → PR-A8 |
| AS-P1 | RDMA config and registration (`L1RdmaConfig`) | ~+400 | Yes | PR-A9 (combined with AS-P2, GF-8, GF-9) |
| AS-P2 | L1 RDMA window pool | ~+1,100 | Yes | PR-A9 |
| AS-P3 | Window leaser and placer | ~+700 | Yes | PR-A10 (combined with AS-P4, AS-P5) |
| AS-P4 | `AerospikeLayerArrivalSource` | ~+400 | Yes | PR-A10 |
| AS-P5 | L2 adapter and storage manager pipelined hooks | ~+700 | Yes | PR-A10 |
| AS-L1 | Fetch planner over Aerospike records | ~+1,300 | No (pure logic) | PR-A6 (combined with AS-L2) |
| AS-L2 | Request-level fetch building | ~+700 | No | PR-A6 (without `native_fetch` → PR-A10) |
| AS-L3 | Pipelined retrieve orchestration and deferral policy | ~+900 | No | PR-A7 |
| AS-M1 | MP server flags `--pipelined-*` | small | — | PR-A11 (combined) |
| AS-M2 | Lookup deferral and session deferred keys | ~+250 | — | PR-A11 |
| AS-M3 | Retrieve wiring, sinks, `fetch_deferred_objects` | ~+1,500 | — | PR-A11 (without `layerwise_sink` → PR-G7) |
| AS-M4 | Observability (`pipelined_outcome`, deferred counter) | ~+150 | — | PR-A11 |
| AS-M5 | Layer publish budget vs worker wait | small | — | PR-A11 (the response field and worker check too) |
| AS-D | Design and testing docs | ~+6,200 | — | Trim and split |
| AS-X | Internal process material (tracks, ledger, `functional/`) | larger than before | — | **No** |

Line totals for the groups, at the merged tip vs upstream:

- native RDMA code: +2,356 in 13 files (was +5,016 in 25);
- native tests: +3,334 (was +7,846);
- connector core (`connector.{h,cpp}`, `pybind.cpp`, `shard_plan`): +1,569;
- Python RDMA integration: +2,354 (8 files), plus the L1 windows +1,658;
- layerwise package: +3,817, with +7,141 of tests;
- MP wiring: +2,050, with +2,341 of tests.

The Python totals were measured before the PR, which barely changes Python
code. D-14 added a few hundred lines on top.

---

# Part A — Aerospike backend, no RDMA needed

These improve the existing Aerospike adapter for every user. They can go
upstream before the server ships, as ordinary Aerospike backend PRs.

## AS-1. Batch exists

> **PR: PR-A1, standalone.**
>
> - It overrides upstream's unchanged `ConnectorBase::do_batch_exists` hook.
>   #4333 (balanced tiles) is compatible.
> - `f4ac45ec` is clean (+116 / -2). Leave `a84d9b10`'s policy changes to
>   PR-A2.
> - Its `import uuid` in the test file is also needed by BF-2. Whichever
>   lands second drops it.
> - **Tests:** the batch cases on CE in the existing CI workflow.

**What.** `AerospikeNativeConnector::do_batch_exists` answers a tile of
existence checks with `aerospike_batch_read` over the meta records' headers:
one request per node per sub-batch of at most `kMaxBatchExistsKeys = 5000`
keys. It replaces one round trip per key. A key exists exactly when
`do_single_exists` would say so. Any error other than "not found" throws, as
the single-key path does.

**Where.**

- `csrc/storage_backends/aerospike/connector.cpp`: `do_batch_exists`.
- `csrc/storage_backends/aerospike/connector.h`: the declaration,
  `kMaxBatchExistsKeys`, and `WorkerAerospikeConn::batch_policy`.
- It overrides the `ConnectorBase` hook in
  `csrc/storage_backends/connector_base.h`.

**Commits.** `f4ac45ec`, `a84d9b10`.

**Tests.** `tests/v1/distributed/test_aerospike_l2_integration.py` (batch
cases, including a 10,000-key lookup that crosses sub-batches). These need a
real Aerospike CE, which the existing `aerospike_integration.yml` workflow
provides.

**Pushback.** Small. Reviewers may ask for a configurable sub-batch size,
since the server's `batch-max-requests` may be lower than 5000.

## AS-2. Read-touch TTL: loads extend, lookups never do

> **PR: PR-A2, after PR-A1.** It can also go before PR-A1, minus one line.
>
> - **Its only contact with AS-1** is
>   `conn.batch_policy.read_touch_ttl_percent = -1`.
> - **Exclude:**
>   - the `aerospike_server_issues.md` hunk;
>   - the "pipelined loads" sentence `8993843e` added to `aerospike.rst`.
> - **Tests:** CE 7.1 or later. CI's `aerospike-server:latest` qualifies.

**What.**

- **Loads touch.** A load follows the namespace's
  `default-read-touch-ttl-pct` (Aerospike 7.1 or later). It reads the meta
  record *and every segment*, so all of an object's records are extended
  together.
- **Lookups never touch.** Existence checks and the meta read inside delete
  use a new `lookup_policy` or `batch_policy`. Touching only the meta record
  would leave a meta that outlives its segments: a lookup hit that can't be
  loaded.

**Where.**

- `csrc/storage_backends/aerospike/connector.cpp` and `connector.h`:
  `WorkerAerospikeConn::{read_policy, lookup_policy, batch_policy}`.
- `docs/source/mp/l2_storage/aerospike.rst` (+30): how to set the namespace
  option, including a dynamic `asinfo` command.

**Commits.** `a84d9b10`.

**Tests.**
`test_read_touch_extends_every_record_a_load_reads_and_none_on_lookup` in
`tests/v1/distributed/test_aerospike_l2_integration.py`. It needs Aerospike
7.1 or later and skips otherwise. It sets the namespace option and checks the
server's `read_touch_success` counter: unchanged by lookups, and raised for
every record a load reads.

**Note.** On the old protocol, RDMA fetches didn't apply read-touch (old
server issue 11). The updated server-issues list counts that as fixed on
`sriram/kv-sink-batch-prio`. Re-check, and update the sentence in
`aerospike.rst` that says pipelined loads don't extend a TTL.

## AS-3. Plane-aligned, per-kernel-group record sharding (storage format change)

> **PR: PR-A5, re-cut, after PR-B7 (BF-8).** It is not standalone as
> written.
>
> **The adapter imports the planner.**
> `NativeConnectorL2Adapter.set_object_group_layouts` imports
> `record_plane_runs` from `lmcache/v1/layerwise/planner.py`, which is AS-L1
> and imports GF-12. Move `PlaneRun`, `record_plane_runs` and
> `_parse_kernel_shape` into a neutral module, such as one under
> `lmcache/v1/distributed/l2_adapters/`. The planner then imports from it.
>
> **Drop dead code.** `set_kv_plane_bytes` (base, adapter, storage manager)
> and `uniform_kv_plane_bytes` have no production caller in the final tree.
> Drop them and `test_uniform_kv_plane_bytes.py`.
>
> **Include AS-4's `max_record_bytes`** and test it here.
>
> **Re-cut the tests:**
>
> - `test_native_record_layouts.py` imports `ModelLayout` (AS-L1). Keep its
>   stub-based record cases; move the rest to PR-A6.
> - `test_aerospike_record_layouts_integration.py` imports `FetchPlanner`
>   and `RecordKeys`. Keep a real-server subset that checks the
>   `plane_b`/`runs` bins and record boundaries; move the rest to PR-A6.
> - `test_slot_plan_parity.py` compares C++ AS-R4 with Python AS-L1, so it
>   goes to PR-A8.
> - **Trim the harness `Makefile`** to `shard_plan_test` only.
> - Take AS-5's `runs-garbage` and `runs-too-short` parameters here.
>
> **Exclude:**
>
> - `a63bd1d9`'s AS-R6 scaffolding;
> - `a7af296c`'s planner, layout-registry and internal-doc hunks;
> - `87a8925b`'s BF-7 hunk.
>
> **How it is tested:** `shard_plan_test` (CPU CI), stub unit tests (CPU) and
> the real-server subset (CE).
>
> **It is functional:** it changes the stored format and keeps old records
> readable. Expect "no benefit yet" pushback. If that blocks it, hold it and
> open it alongside PR-A6.

**What.** The connector splits a large object into a meta record plus
segments (`<key>|s|<i>`). Before this change it cut by byte count, so a
segment could span two layers. Now:

- **Uniform layouts.** `set_plane_bytes(plane_bytes)` cuts segments on K/V
  plane edges, so every record belongs to exactly one layer.
- **Hybrid layouts.** `set_record_layouts(object_groups)` registers per-object-group
  lists of `PlaneRun` (one run per kernel group). Models whose kernel groups
  differ in plane size, such as gpt-oss, are still cut one plane per record,
  through `make_layered_shard_plan`.
- **Planes larger than a record.** `plane_segment_bytes(plane_bytes,
  max_record_bytes)` splits them evenly, never letting two planes share a
  record.
- **Self-describing records.** Each record persists the plane size and runs it
  was written with, in new `plane_b` and `runs` meta bins. Old records stay
  readable, and changing the layout affects only later writes.
- **Wiring.** The plane size and layouts arrive after construction, when a
  worker registers its KV cache:
  - `L2AdapterInterface.set_kv_plane_bytes` and `set_object_group_layouts`
    (default no-op) in `l2_adapters/base.py`;
  - `NativeConnectorL2Adapter` forwards them;
  - `StorageManager.set_kv_plane_bytes` and `set_object_group_layouts`;
  - `lmcache_driven_transfer.uniform_kv_plane_bytes` and `register_kv_cache`
    publish them.

**Where.**

- `csrc/storage_backends/aerospike/shard_plan.{h,cpp}` (new):
  `plane_segment_bytes`, `make_layered_shard_plan`, `PlaneRun`.
- `csrc/storage_backends/aerospike/connector.{h,cpp}`: `set_plane_bytes`,
  `set_record_layouts`, `plan()`, and the `plane_b` / `runs` bins.
- `csrc/storage_backends/aerospike/pybind.cpp`: bindings.
- `setup_extensions/storage_backend_profiles/aerospike.py`: always compiles
  `shard_plan.cpp` (`10c30f03`).
- `lmcache/v1/distributed/l2_adapters/base.py`,
  `native_connector_l2_adapter.py` and
  `lmcache/v1/distributed/storage_manager.py`.
- `lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py`:
  `uniform_kv_plane_bytes`.

**Commits.** `a63bd1d9` (plane-aligned), `dee271e7` (derive plane size),
`a7af296c` (per kernel group), `10c30f03` (link in every build, real-server
check), `87a8925b` (omit empty `layer_indices`).

**Interaction with D-14 (`e9cd0689`, already on `prototype-stage1`).**
Segment keys are now `<key>|s|<wid>|<i>`, with a per-write ID in a new `wid`
meta bin. Meta records are create-only. That is a second format change in
the same records. Upstream would review it as its own bug-fix PR (BF-8 in
[bug_fixes.md](bug_fixes.md)), so land BF-8 *before* AS-3. Both keep
old-layout records readable (T-STO-06).

**New sink constraint.** Server issue 5 on the new branch says only
single-blob-bin records of at most 2 MiB (`KV_SINK_MAX_VALUE_SZ`) can be
sink-read. The record cap AS-3 cuts against must stay at or under that, or
the pipelined path refuses those records.

**Tests.**

- `tests/v1/distributed/test_native_record_layouts.py`
- `tests/v1/distributed/test_aerospike_record_layouts_integration.py` (real
  server)
- `tests/v1/multiprocess/test_uniform_kv_plane_bytes.py`
- `tests/v1/distributed/rdma/test_shard_plan.py` with `csrc/shard_plan_test.cpp`
- `test_slot_plan_parity.py` with `fixtures/slot_plans.txt`: the C++ and
  Python planners agree.

**Pushback (expect a lot).**

- **The storage format changes with no benefit to the plain read path.** Its
  only reader is the RDMA planner. It costs some extra, smaller records when a
  plane is not a multiple of the record cap; `shard_plan.h` documents the
  cost. Offer it behind an option, or ship it with the RDMA PRs.
- **The C++ planner and the Python planner (`lmcache/v1/layerwise/planner.py`)
  duplicate the same rule.** A parity fixture guards the duplication, but
  reviewers dislike two implementations.

## AS-4. Record naming accessors

> **PR: split. It is not a PR of its own.**
>
> - `max_record_bytes` → PR-A5.
> - `read_write_ids` → PR-A6. It needs AS-1's `kMaxBatchExistsKeys` and
>   BF-8's `wid` bin.
> - `record_digest_hex` is used only by tests. It goes to PR-A6 with the
>   record-layout integration test, or is dropped.
> - `record_node` is unused anywhere, now that rows are routed by partition.
>   Drop it.

**What.** Native connector methods the planner needs to name records exactly
as the writer stored them:

- **`record_digest_hex(user_key)`:** the client's own RIPEMD-160 digest, as
  40 hex characters.
- **`record_node(user_key)`:** the node mastering the record, from the
  partition map. A snapshot; a migration may move it.
- **`max_record_bytes()`:** the record cap the writer cuts against.
- **`read_write_ids(keys)`** (D-14, `e9cd0689`): one batch read of the meta
  records' state and `wid` bins, so the planner can name per-write segment
  keys. Measured at 25-130 us on one node, and 24-358 us on 3 nodes, for 1-64
  keys. An object with no meta record, or a failed read, refuses the
  pipelined retrieve.

There is also `pipelined_max_record_bytes` on the adapter, and on
`StorageManager`.

**Where.** `connector.{h,cpp}`, `pybind.cpp`, and
`native_connector_l2_adapter.py`. `_object_key_to_string` probably needs to
become public; see `track-c-status.md`.

**Commits.** `5b699bfe`, `f5efd36c`, `344b50e1`, `adaf1ced`, `e9cd0689`.

**After the PR.** `record_node` matters less. The client routes each row by
partition, so the node a plan names is nominal.

**Notes.** Alone they have no caller; see the PR line above for where each
one goes.

## AS-5. Storage-integrity integration tests

> **PR: split into PR-A3 (single node) and PR-A4 (3-node cluster).**
>
> **PR-A3** tests behavior upstream already has, so it needs no
> prerequisite. But it is not a copy:
>
> - **Port to prefetch v2.** The file uses `PrefetchRequestSpec`, which
>   upstream removed in #5356. Use `submit_prefetch_task(PrefetchTaskSpec)`.
> - **It imports the public `object_key_to_string`,** which `dev` doesn't
>   have. Include the rename (about 5 lines) rather than reaching for the
>   private name.
> - **Leave out cases owned by other PRs:**
>   - BF-8's three cases → PR-B7;
>   - the `runs-garbage` / `runs-too-short` parameters → PR-A5.
> - **Add** T-LKP-06 (`test_native_connector_l2_adapter.py`, +48) and the
>   workflow entry.
> - **Tests:** CE in CI.
>
> **PR-A4**, after PR-B7 (T-FLT-10 tests BF-8):
>
> - **Move the harness.** The cluster file drives
>   `functional/harness/cluster_ctl_daemon.sh`, `cluster.sh` and
>   `configs/cluster/*`, all internal material (AS-X). They must move under
>   `tests/`.
> - **Port `reserve_write`.** Its subprocess script calls
>   `reserve_write(keys, layout, "new")`, but upstream dropped `mode`
>   (#5247).
> - **Tests:** a 3-node cluster, opt-in through
>   `RUN_AEROSPIKE_CLUSTER_INTEGRATION=1`. Not in upstream CI.

**What.** Real-server tests of the existing adapter's guarantees. The IDs are
from `docs/design/v1/layerwise/functional-test-plan.md`:

| ID | Guarantee |
| --- | --- |
| T-STO-03 (= T-FLT-09) | The meta record is written last, so a killed writer never leaves a lookup hit |
| T-STO-04 (= T-EVT-02) | A segment missing under an intact meta record fails the load |
| T-STO-05 | A corrupt meta record fails as corrupt, never as a short read |
| T-STO-07 | Records carry the configured TTL |
| T-LKP-06 | Keys that differ only in `object_group_id` don't collide |
| T-LKP-03 | Prefix-hit semantics |
| T-EVT-03 | Records past their TTL |
| T-EVT-01 | Filling a namespace past eviction (needs `AEROSPIKE_TEST_EVICT_NAMESPACE`) |

**3-node cluster tests** (`9367ef65`, extended by `e9cd0689`, already on
`prototype-stage1`), gated on `RUN_AEROSPIKE_CLUSTER_INTEGRATION=1`, CPU
only:

| ID | Guarantee |
| --- | --- |
| T-STO-08 | RF 2 commit-all replica counts; reads with a node down |
| T-FLT-02/03/04 | A node SIGKILLed mid-fetch at RF 1 and RF 2, then restarted |
| T-FLT-10 | Two writer processes racing on one inline and one sharded key, 1,000 rounds (the D-14 reproducer; 0 mixed after BF-8) |
| T-EVT-06 | Two storage managers sharing a cluster, with and without client-side L2 eviction |

**Where.**

- `tests/v1/distributed/test_aerospike_storage_integrity_integration.py`
  (new). `e9cd0689` adds the D-14 cases: create-only conflict, missing
  segment then re-store, delete removes named segments, and old-layout
  objects (T-STO-06).
- `tests/v1/distributed/test_aerospike_cluster_integration.py` (new, 844
  lines).
- `tests/v1/distributed/test_native_connector_l2_adapter.py` (+48).
- Some cases in `test_aerospike_l2_integration.py`.

**Commits.** `87b84b6b`, `b94cad49`, parts of `c6980baf`, `9367ef65`, and
the test half of `e9cd0689`.

**Notes.** Test-only. Add the single-node file to
`.github/workflows/aerospike_integration.yml`'s pytest list (PR-A3). The
cluster file needs a 3-node setup, which upstream CI doesn't have, so keep it
opt-in (PR-A4). T-FLT-10 is BF-8's regression test, but it needs the cluster
harness, so it ships in PR-A4, after PR-B7.

---

# Part B — RDMA transport, native C++

All of it is compiled only with `BUILD_WITH_AEROSPIKE_RDMA=1`, under
`#ifdef LMCACHE_AEROSPIKE_RDMA`. A default build is unchanged. Everything is
in `csrc/storage_backends/aerospike/`.

**After the `prototype-stage-1a` PR, the verbs code is gone from LMCache.**
The kv-sink fork of the Aerospike C client owns the RDMA endpoint. LMCache's
native RDMA code shrinks to the fetch driver, its bookkeeping, and the
layout plumbing: 13 files, +2,356 lines, down from 25 files and +5,016.

## AS-R1. Opt-in RDMA build profile and kv-sink client build script

> **PR: combined into PR-A8** (the RDMA native PR).
>
> - **Why it can't be its own PR:** the profile's source list names AS-R4 and
>   AS-R5 files.
> - **Hunks that belong elsewhere:** `_system_yaml_soname` (BF-5, PR-B5) and
>   `shard_plan.cpp` (AS-3, PR-A5).
> - **After `f91c1972`,** the build script needs client `18b68325` or later
>   and has dropped its link workaround (client issue 11 is fixed). Re-check
>   whether the profile's `-libverbs -lefa` comment is still accurate.

**What.**

- **Build flag.** `BUILD_WITH_AEROSPIKE_RDMA=1` compiles
  - `sink_fetch_table.cpp`, `connector_sink_fetch.cpp`;
  - `slot_planner.cpp`;
  - `memory_layout_conversion.cpp`, `aerospike_pipelined_pybind.cpp`.

  `layer_pipeline.cpp` was dropped from the profile in `ae4c9807`.

  It links `libibverbs` and `libefa`, because the client fork's transport
  calls into both and `libaerospike` doesn't declare them (client issue 11).
- **EFA flag.** `BUILD_WITH_AEROSPIKE_EFA=1` is now only a synonym: RC vs SRD
  is a run-time choice of the client.
- **Header check.** `_require_kv_sink_client(include_dirs)` refuses the build
  unless `aerospike/as_sink.h`, which only the fork has, is on
  `AEROSPIKE_INCLUDE_DIR`.
- **Client build script.** `.deps/build_aerospike_client_kvsink.sh` (new, 92
  lines) clones and builds the fork into `.deps/` and writes
  `.deps/aerospike-client-c.env` for the LMCache build. It defaults to
  `https://github.com/sriram588/aerospike-client-c-kvsink.git` at
  `sriram/kv-sink-batch-prio`.
- **Paths and macro.** `RDMA_CORE_INCLUDE_DIR` and `RDMA_CORE_LIBRARY_DIR`
  locate a non-system rdma-core; the macro is `LMCACHE_AEROSPIKE_RDMA`.

**Where.** `setup_extensions/storage_backend_profiles/aerospike.py` (split
out the libyaml hunk, BF-5, and the `shard_plan.cpp` line, AS-3),
`.deps/build_aerospike_client_kvsink.sh` and `.gitignore`.

**Commits.** `6abe6e69`, `8993843e`.

**Pushback.** Upstream LMCache can't take a build that clones a personal
fork. This item has to wait until the sink API is in an official Aerospike C
client release; then the script goes away.

## ~~AS-R2. `RdmaContext`~~ — deleted by the PR

`rdma_context.{h,cpp}` (device, GID, PD, `ibv_reg_mr`, RC/SRD queue pairs,
notification receives) is gone. Its commits no longer need upstreaming:
`d6dff614`, `f7fae3bd`, `8c024ade`, `a74e828c`, `1c33157a`, `ca1dae05`, and
the CQ half of `03c1ad6d`. The client fork opens the device; LMCache passes
`transport`, `device_name` and `gid_index` through `as_sink_config`.

D-11 (a wrong GID index is not named in any error) is now a question for the
client fork. Re-check what the new error looks like.

## ~~AS-R3. kv-sink control plane~~ — deleted by the PR

`kv_sink_client.{h,cpp}` and `kv_sink_fanout.{h,cpp}` are gone: the
info-command codecs, `max_sinks`, the `find_info_field` echo fix, per-node
registration and deregister-on-close. The client fork does registration,
touch and deregister itself, inside `aerospike_sink_create` and the sink's
release. Commits that no longer need upstreaming: `62513bfd`, `f8339d02`,
`586974ea`, `3b29260b`, and the fanout half of `03c1ad6d`.

What survives the idea: the whole window range is still registered **once,
as one sink**, because each registration costs every node a region and, on
RC, a queue pair. A refresh path (`refresh_sink`) re-creates the sink when a
node reports `AEROSPIKE_ERR_SINK_UNKNOWN_REGION` (220).

## AS-R4. Pipelining model: layer readiness, slot planner

> **PR: combined into PR-A8, trimmed.**
>
> - **What the production driver uses:** only the `SlotPlanner` constructor
>   (as validation), `window_fit_error` and `ObjectGroupLayout`.
> - **Trimmed on the fork (`ae4c9807`).** `RequestPlan`, `LayerReadiness`,
>   `plan_request`, `participating_chunks` and `ChunkPlacement` are gone,
>   with `layer_pipeline.*` and `request_plan_test` (about 1,400 lines).
>   Nothing is left to drop here.
> - **Dependency:** `slot_planner.cpp` no longer uses AS-3. The parity
>   harness `slot_plan_dump.cpp` (AS-R6) does, through `plane_segment_bytes`
>   and `choose_shard_plan`, so the parity test still comes after PR-A5.

**What.** Bookkeeping with no client dependency:

- ~~`layer_pipeline.{h,cpp}`~~: **deleted** (`ae4c9807`). It modelled the
  RDMA-immediate readiness protocol the batch-read driver replaced.
- **`slot_planner.{h,cpp}`.** Each layer's K/V plane ranges inside an object,
  layout validation, and a check that one chunk fits a window. The slot
  schedule is built by the Python planner (AS-L1).
- ~~`notification_depth.{h,cpp}`~~: **deleted by the PR**, since there is no
  receive queue to size.

**Commits.** `5993717d`, `2b680e11`, `8ec281c0`, `73558449`, `ae4c9807`
(trim). `11f4dac2` and `d02b3dce` (notification depth and `max_sinks` chunking) are
obsolete.

## AS-R5. kv-sink batch-read fetch driver, fetch table, bindings

> **PR: PR-A8,** with AS-R1, the trimmed AS-R4 and AS-R6's RDMA harnesses.
> Prerequisites: PR-A5, plus PR-A6 for the parity test.
>
> - **RDMA code outside the `#ifdef`:** `connector.{h,cpp}` and `pybind.cpp`
>   carry `#ifdef LMCACHE_AEROSPIKE_RDMA` blocks (constructor init,
>   `close()`, `connector.cpp` lines 814-925). Two pieces sit outside them:
>   the `PlannedSlotKey` struct and the `L1RdmaRegistration` constructor
>   parameter, header and pybind class. All of it goes in PR-A8.
> - **Fabric-free files** (std-only, testable in CPU CI): `sink_fetch_table.*`,
>   `slot_planner.*` and `memory_layout_conversion.*`.
> - **Build-only with the client fork:** `connector_sink_fetch.*` (includes
>   `aerospike/as_sink.h`), `aerospike_pipelined_pybind.*` and the connector
>   glue.
> - **Upstream CI can't build the RDMA extension.** The client fork is
>   cloned from a personal account; its public visibility is unverified.
> - **`f91c1972`** now accepts a sink registered on only some nodes, with a
>   warning.

**What (after the PR).**

- **`sink_fetch_table.{h,cpp}`** (new, about 360 lines): `SinkFetchTable`,
  pure bookkeeping with no client.
  - One active fetch per window, named by a non-zero generation.
  - The fetch is split into one batch per layer, in plan order, each tagged
    with its position as the server-side priority.
  - Per-row results fold into per-layer readiness.
  - Results for a fetch that is no longer active are dropped.
  - `kMaxSlotsPerRequest = 1 << 16`, which matches `MAX_SLOTS_PER_REQUEST` in
    `contract.py`.
- **`connector_sink_fetch.{h,cpp}`** (new, about 490 lines):
  `AerospikeSinkFetchDriver`.
  - `initialize` calls `aerospike_sink_create` over the window range.
  - Batch worker threads (`start_workers`, `run_worker`, `execute`) send each
    layer's `aerospike_batch_read` and record row results.
  - `refresh_sink` handles a node that lost the sink.
  - The rest of its API: `issue`, `is_layer_ready`, `unservable_layers`,
    `finish_request`, `abandon_request`, `max_slots_per_request`, `node_name`
    and `shutdown`.
- **`memory_layout_conversion.{h,cpp}`**: `MemoryLayoutDesc` to
  `rdma::ObjectGroupLayoutInput`. Each kernel group carries an
  `element_size` in bytes, which Python sends as `element_sizes`
  (`dtype.itemsize`) from `_native_object_group_layouts` in
  `native_connector_l2_adapter.py`. A zero size is refused. Since
  `7c4b2278`, C++ no longer parses dtype names: the parser sized FP8, int8
  and float64 as 4 bytes. Its Python tests,
  `test_the_pipelined_planner_gets_each_kernel_groups_element_size` and
  `test_hybrid_kernel_groups_keep_their_own_element_sizes` in
  `test_native_record_layouts.py`, go to PR-A8 with it, not to PR-A5.
- **`aerospike_pipelined_pybind.{h,cpp}`**: Python bindings for the pipelined
  types. Small edits.
- **Connector methods** (`connector.{h,cpp}`, under `#ifdef`), which keep
  their names:
  - `pipelined_fetch_ready`, `pipelined_fetch_init_error`,
    `pipelined_fetch_node_name`;
  - `set_object_group_layouts`;
  - `issue_pipelined_fetch_by_slots(node_names, slots)`;
  - `pipelined_max_slots_per_request`, `is_pipelined_layer_ready`,
    `pipelined_unservable_layers`;
  - `finish_pipelined_fetch`, `abandon_pipelined_fetch`;
  - `try_initialize_pipelined_rdma`.

  `poll_pipelined_fetch_notifications` is **gone**, because row results
  arrive on the batch workers.
- **`pybind.cpp`.** The binding chain still ends outside the `#ifdef`
  (`b3d67843`).

**Deleted by the PR:** `pipelined_fetch_session`, `pipelined_fetch_pool`,
`pipelined_fetch_issue` and `connector_pipelined_rdma`
(`AerospikePipelinedRdmaDriver`). Their commits no longer need upstreaming:
`f7202fb0`, `3985b9dd`, `52f2c47e`, `ecf64504`, `33dd95b5`, `6fd5fc4d`,
`ba7b4e06`, `fd0b16c7`. The two rules they enforced survive in
`SinkFetchTable`: one fetch per window, and stale results never credit a
newer fetch.

**Commits.** `8993843e`, `61d39ffa` (format), `7c4b2278` (element sizes from
torch), plus the surviving parts of `b359b4ea`, `7333280b` and `adaf1ced`.

**Pushback.**

- **Size.** About 2,360 lines of new C++, down from about 5,000.
- **Untestable upstream.** It needs the client fork, and no upstream CI has
  RDMA.
- **Thread pool inside the connector.** The batch workers add one.
- **Multi-node.** Allowed, but not yet tested.

## AS-R6. C++ test harnesses

> **PR: split by the code each harness tests.**
>
> - `shard_plan_test` → PR-A5.
> - `sink_fetch_table_test`, `slot_planner_test`,
>   `memory_layout_conversion_test` and `fabric_free_session_pybind.cpp` →
>   PR-A8.
> - `test_slot_plan_parity.py` → PR-A8, after PR-A6.
>
> Each PR carries only its targets in the harness `Makefile`.

**What (after the PR).** `tests/v1/distributed/rdma/`, about +3,330 lines
(was about 7,850). All of it is fabric-free:

- **Build.** A `Makefile` with `logic`, `logic-test` and `pyharness` targets,
  and a `conftest.py`.
- **Tests,** each a `csrc/*_test.cpp` plus a `test_*.py` wrapper:
  - `slot_planner` (layer geometry, layout validation and window fit only,
    since `ae4c9807`), `shard_plan`;
  - ~~`request_plan`~~: deleted with `layer_pipeline.*` (`ae4c9807`);
  - `sink_fetch_table` (new);
  - `memory_layout_conversion` (new, C++ only, run by `logic-test`):
    element sizes are used as given, hybrid kernel groups keep their own,
    and a zero size is refused;
  - `test_slot_plan_parity.py`, with `slot_plan_dump.cpp` and
    `fixtures/slot_plans.txt`. Since `ae4c9807` the dump builds its own C++
    slot schedule from `SlotPlanner`'s geometry (formerly
    `SlotPlanner::plan_request`), so the test compares the Python planner
    against an independent schedule and against the records the writer
    cuts.
- **`fabric_free_session_pybind.cpp`**, used by the Python harness in
  `tests/v1/layerwise/`. It is one of the two merge-conflict files.

**Deleted by the PR:**

- the mock writer (`kv_sink_mock_writer.{h,cpp}`);
- `efa_imm_probe.cpp` (A7);
- `rdma_equivalence_test`, `rdma_pipeline_test`, `pipelined_fetch_test`,
  `pipelined_fetch_session_test`, `pipelined_fetch_pool_test`,
  `pipelined_fetch_issue_test` and `notification_depth_test`, with their
  Python wrappers.

Fabric coverage now comes only from the real-server A8 suite (AS-M3).

**Pushback.** Smaller, and now upstream CI could run it all. It is still
built outside `setup.py`.

---

# Part C — RDMA integration in the distributed layer (Python)

## AS-P1. RDMA config and native registration

> **PR: PR-A9, combined with AS-P2, GF-8 and GF-9. Prerequisite: PR-A8.**
>
> **AS-P1 and AS-P2 depend on each other:**
>
> - `config._reserve_rdma_windows_for_adapter` (AS-P2) imports
>   `rdma_config_of` (AS-P1);
> - `storage_manager._build_l2_adapter` calls `validate_windows_reserved`
>   (AS-P2);
> - `test_l1_rdma_windows.py` imports the AS-P1 config.
>
> **It needs PR-A8:** `L1RdmaRegistration` fields are bound only by the RDMA
> native build. **It needs GF-8:** `rdma_registration.py` uses
> `MemoryGrowthPolicy`.

**What.**

- **Adapter config.** `AerospikeL2AdapterConfig` gains an `rdma` block,
  parsed by `L1RdmaConfig.from_dict` with these fields:
  - `transport`: `RdmaTransport` `DISABLED` / `RC` / `SRD`;
  - `device_name`;
  - `gid_index`;
  - `queue_pairs` (RC queue pairs per node, 1-16);
  - `window_plan`: `RdmaWindowPlan(window_count, window_bytes)`;
  - `fetch_timeout_seconds`.
- **Translation to native.** `_build_native_rdma_registration` translates it
  into the native `L1RdmaRegistration`. It raises if the extension was built
  without RDMA, or if no L1 descriptor is available.
- **Checks.**
  - `RdmaWindowPlan.validate_against(l1_memory_desc)` refuses a `GROWABLE`
    slab (it needs GF-8).
  - `validate_windows_reserved` confirms L1 reserved the windows.
  - `validate_fetch_timeout_against_write_ttl` requires the fetch timeout to
    be under the L1 write-lock TTL. It is called from
    `StorageManager._build_l2_adapter`.

**Where.**

- `lmcache/v1/distributed/l2_adapters/rdma_registration.py` (new).
- `lmcache/v1/distributed/l2_adapters/aerospike_l2_adapter.py`.
- `lmcache/v1/distributed/storage_manager.py`: `_build_l2_adapter`.

**Commits.** `92c26c82`, `94e5df38`, `1a5ef296`, `5dc5306b`, `7b7cee99`.

**Tests.** `tests/v1/distributed/l2_adapters/test_rdma_registration.py`.

## AS-P2. L1 RDMA window pool

> **PR: PR-A9** (see AS-P1).
>
> **Port it onto upstream's L1 changes:**
>
> - #5247 replaced `mode` with `tag`; the fork's `reserve_write(pool=)`
>   requires `mode="new"`;
> - #5068 added `finish_write_and_delete`, which overlaps `abort_write`;
> - #5393 (hugepages) and #5077 touch the same allocators.
>
> **Tests** are fabric-free (CPU CI).
>
> **Library PR:** the windows do nothing until PR-A10 leases them.

**What.** A fixed number of equal windows carved from the front of the L1
slab. They are excluded from general allocation and eviction, and leased to
pipelined retrieves:

- **Config** (`lmcache/v1/distributed/config.py`):
  `L1MemoryManagerConfig.rdma_window_count` and `rdma_window_bytes`, plus
  `_reserve_rdma_windows_for_adapter`, which sizes them from the adapter's
  `rdma` block.
- **Pool names** (`internal_api.py`): `L1Pool` and `L1PoolKind`;
  `GENERAL_L1_POOL` and `L1Pool.rdma_window(i)`.
- **Memory manager** (`memory_manager/l1_memory_manager.py`).
  - Windows are `RangeMemoryAllocator`s over a prefix reserved with
    `MixedMemoryAllocator(reserved_prefix_bytes=...)`.
  - `get_pool` and per-pool free.
  - Usage metrics exclude windows.
  - `l1_manager_protocol.py` gains `allocate(pool)`, `get_pool` and
    `get_rdma_window_count`.
  - The GDS and devdax managers refuse windows.
- **L1 manager** (`l1_manager.py`).
  - `reserve_write(..., pool=)`, which requires `mode="new"`.
  - Per-window key tracking.
  - `reclaim_rdma_window`, `get_rdma_window_count` and
    `get_rdma_window_object_count`.
  - Window objects are never evicted.
  - The generic helpers in GF-9.

**Commits.** `83f391d3`, `0703d954`.

**TODO, not in this PR: size windows from the model.** The 8 MiB default
`window_bytes` is smaller than one chunk of most models (32 MiB for
Llama-3-8B), so with defaults nothing is pipelined. The windows are reserved
and registered at server start, before any model registers, so fixing this
means deferring both to the first `REGISTER_KV_CACHE`. It is a follow-up PR
after PR-A9 and PR-A10. The design doc's "Sizing `window_bytes`" section
records it.

**Tests.** `tests/v1/distributed/test_l1_rdma_windows.py` and
`tests/v1/test_range_memory_allocator.py`.

**Upstream overlap: high.** Dry-run merge conflicts in `l1_manager.py` and
`l1_memory_manager.py`. Upstream changed `reserve_write` (#5247), added
`finish_write_and_delete` (#5068), and added hugepages L1 (#5393).

**Pushback.** "RDMA" is baked into a generic L1 API: `L1Pool.rdma_window`,
`reclaim_rdma_window`. Consider renaming to "reserved pools" or "pinned
windows", so any zero-copy transport can use them.

## AS-P3. Window leaser and placer

> **PR: PR-A10, combined with AS-P4 and AS-P5.**
>
> - **Dependencies:** `rdma_window_placer.py` imports `request_fetch` (AS-L2,
>   PR-A6). `test_rdma_placer_end_to_end.py` and
>   `test_storage_manager_placer.py` import `pipelined_retrieve` (AS-L3), so
>   PR-A10 follows PR-A7.
> - **Upstream drift:** `select_l1_retentions` is now `plan_l1_retention`
>   (prefetch v2).

**What.**

- **`RdmaWindowLeaser`** (`rdma_window_leaser.py`) leases windows.
  - It picks an empty window first. Otherwise it takes the longest-released
    window whose objects are all unlocked, reclaiming it by deleting through
    L1's normal path, so eviction events still fire.
  - A window released after an *abandoned* fetch is quarantined, because
    late writes may still land. Quarantine assumes a row that missed
    `fetch_timeout_seconds` is written by then or never. Server issue 2
    (queued writes had no deadline) broke that assumption; it is fixed in
    server `046e8558d`, where each op carries the transaction's deadline.
  - `WindowLease` carries `window_index`, `base_offset`, `size_bytes` and
    `lease_id`, so a stale lease can't release a newer one.
  - `FetchOutcome` is `FINISHED` or `ABANDONED`. These are rules W1 to W4.
- **`RdmaWindowPlacer`** (`rdma_window_placer.py`) implements the layerwise
  `ChunkPlacer`; `WindowPlacement` implements `WindowLease` from
  `request_fetch.py`.
  - It reserves each object in the leased window at its aligned offset.
  - `check_window_holds_request` refuses objects that don't fit.
  - `select_retentions` (the prefetch policy's L1 retention; default
    `retain_none`) decides which fetched objects stay in L1. A clean release
    uses `finish_write_and_reserve_read` then `finish_read`, so no store
    notification fires. This is F1, `8e18aabd`.
  - The default stays `retain_none` (G-08). The design doc tells users who
    want L1 hits on repeats to set `--l2-prefetch-policy retain`, and that a
    retained chunk lives only until its window is leased again. Carry that
    section ("Keeping fetched chunks in L1") with this PR.

**Where.** `lmcache/v1/distributed/l2_adapters/rdma_window_leaser.py` and
`rdma_window_placer.py` (both new). The storage manager builds the placer
(`2987f5fc`).

**Commits.** `0703d954`, `d4a5651b`, `dad66a2b`, `8e18aabd`, `2987f5fc`,
`e3c665db`.

**Tests.**

- `tests/v1/distributed/test_rdma_window_leaser.py`
- `test_rdma_window_placer.py`
- `test_pipelined_placer_access.py`
- `tests/v1/layerwise/test_rdma_placer_end_to_end.py`
- `test_storage_manager_placer.py`

## AS-P4. `AerospikeLayerArrivalSource`

> **PR: PR-A10.**
>
> - **Brings `native_fetch.py` and `test_native_fetch.py`** out of AS-L2. This
>   source is their only caller.
> - **Re-adds the `"aerospike"` harness** in `tests/v1/layerwise/conftest.py`.
>   PR-G6 removed it.
> - **Tests:** `test_aerospike_concurrent_fetches.py` and the aerospike
>   conformance harness build `fabric_free_session` from
>   `sink_fetch_table.cpp`. They need pybind11 and skip without it; no RDMA
>   hardware.

**What.** It implements the `LayerArrivalSource` contract (GF-12) over the
native connector.

- `begin_fetch` issues the plan through `NativePlanIssuer`, which calls
  `issue_pipelined_fetch_by_slots`. After the PR that queues one batch read
  per layer.
- `poll_layer` returns `RESIDENT`, `PENDING` or `UNSERVABLE`. After the PR it
  reads row results recorded by the batch workers; there are no
  notifications to drain.
- `finish_fetch` and `abandon_fetch` complete the lifecycle.
- `read_write_ids(keys)` (D-14, `e9cd0689`) exposes the native batch read of
  write IDs to the planner.
- `_as_contract_errors` maps native exceptions to contract errors.
- The `PipelinedFetchConnector` and `PlannedFetchConnector` protocols let
  tests substitute the native object.

**Where.** `lmcache/v1/distributed/l2_adapters/layerwise_source.py` (new).

**Commits.** `6ca5b48a`, `c72489a0`, `1aa3dceb`, `b359b4ea`, `83210bf1`,
`e9cd0689`, `8993843e` (docstrings).

**Pushback.** `read_write_ids` is part of the "generic" `LayerArrivalSource`
contract (GF-12), but it only means something for Aerospike's D-14 layout.

**Tests.**

- `tests/v1/layerwise/test_aerospike_layer_arrival_source.py`
- `test_aerospike_concurrent_fetches.py`, `test_track_a_source_shape.py`
- `test_arrival_source_conformance.py` (runs over the real session)
- `tests/v1/distributed/test_layer_arrival_source_access.py`

## AS-P5. L2 adapter and storage manager pipelined hooks

> **PR: PR-A10. Prerequisites:** PR-A6, PR-A7, PR-A8, PR-A9 and PR-G6.
>
> - **Dependencies:** `native_connector_l2_adapter.py` imports
>   `layerwise_source` at module level; `storage_manager.py` imports
>   `request_fetch` and builds the window leaser.
> - **Tests** need `PipelinedNativeClientStub` in `tests/v1/distributed/utils.py`.
>   Upstream changed that file too (+44), so expect a conflict.
> - **Hunks that belong elsewhere:** in the same files, `failure_reason` is
>   BF-2's, and `load_into_l1` / `query_prefetch_outcome` are PR-G8's.
> - **Library PR:** no production caller until PR-A11. If reviewers object,
>   merge PR-A10 into PR-A11.

**What.**

- **`L2AdapterInterface`** (`l2_adapters/base.py`) gains default-raising
  hooks: `layer_arrival_source`, `pipelined_fetch_node_name`,
  `pipelined_max_slots_per_request` and `pipelined_max_record_bytes`. It also
  gains `pipelined_fetch_init_error`, which defaults to `""`. Each raises
  `LayerwiseContractError` by default, so other backends need no change.
- **`NativeConnectorL2Adapter`** (+253) implements them over the native
  client.
- **`StorageManager`** (+427) exposes the same accessors plus
  `pipelined_adapter_id`, `pipelined_window_placer` and
  `rdma_window_placer`. `_first_ready_pipelined_adapter` gives one rule for
  which adapter is "the pipelined one" (`16d471e8`).

**Where.** `l2_adapters/base.py`, `native_connector_l2_adapter.py` and
`storage_manager.py`.

**Commits.** `6ca5b48a`, `1aa3dceb`, `adaf1ced`, `c295d527`, `a3b4fef3`,
`aecc663e`, `2987f5fc`, `16d471e8`.

**Upstream overlap.** `storage_manager.py` and `native_connector_l2_adapter.py`
conflict in the dry-run merge.

**Pushback.** Several pipelined-only methods on a generic base class.
Reviewers may prefer one optional capability object, for example
`adapter.pipelined() -> PipelinedCapability | None`.

**After the PR.** `pipelined_fetch_node_name` no longer means "the cluster's
one node". It returns *a* node, and the name in a plan is nominal. With
rows routed by partition, consider dropping it from the interface before
upstreaming.

---

# Part D — Layerwise planning package (Aerospike-aware half)

The transport-agnostic half (`contract.py`, `pump.py`, `fakes.py`) is GF-12
in [general_features.md](general_features.md). The modules below assume
Aerospike records, RDMA windows and slots. `track-c-status.md` reports all
254 tests in `tests/v1/layerwise/` passing when the package is copied onto
`dev`, once `_object_key_to_string` is made public.

## AS-L1. Fetch planner over Aerospike records

> **PR: PR-A6, combined with AS-L2.**
>
> - **Prerequisites:**
>   - PR-G6 (contract);
>   - PR-A5 (the record format it plans; `record_plane_runs` from the
>     neutral module);
>   - PR-B7 (the segment naming);
>   - PR-A1 (`kMaxBatchExistsKeys` for `read_write_ids`).
> - **Pure logic.** It imports no native code, AS-4 or AS-P classes. It
>   hard-codes BF-8's naming and AS-3's plane rule in Python.
> - **Also carries:**
>   - BF-8's pipelined half (`RecordKeys(write_ids)`);
>   - AS-4's `read_write_ids` and `record_digest_hex`;
>   - the planner parts of `test_native_record_layouts.py` and
>     `test_aerospike_record_layouts_integration.py`.
> - **Tests:**
>   - `test_fetch_planner` (CPU torch);
>   - the record-layout integration test (CE).
>   - The slot-plan parity test waits for PR-A8.
> - **Library PR:** consumed by PR-A7 and PR-A10.

**What.** `lmcache/v1/layerwise/planner.py`:

- **`ModelLayout`**, built from the registered layouts: per-kernel-group
  geometry (`KernelGroupGeometry`) and the global layer order.
- **`FetchPlanner`** turns a `PlanRequest` (objects and their window offsets)
  into a `LayerFetchPlan` of slots. A slot is "record *k* lands at offset *o*
  and counts toward layer *L*".
- **`RecordKeys`** and `RecordKeySource` name records exactly as the writer
  stored them (uses AS-4). Since D-14 (`e9cd0689`) that is
  `RecordKeys(write_ids)`, naming `<key>|s|<wid>|<i>`.
- **`record_plane_runs`** and **`plane_segment_bytes`** mirror AS-3's C++
  rule. A parity fixture guards the duplication.
- **Slot ceiling.** Plans past the ceiling raise `PlanTooLargeError`;
  `MAX_SLOTS_PER_REQUEST` (`0x10000`) is in `contract.py`. After the PR it is
  justified by the native fetch table's size rather than the RDMA
  immediate's 16 bits; the value is the same.
- **Node per slot.** Since the PR, an object's node name is nominal. The
  old "only correct on a single-node cluster" caveat is gone from the
  docstrings.
- **No window helper.** `FetchPlanner.participating_chunks` had no caller
  and was removed (`ae4c9807`); a sliding window's chunks come from
  `first_in_window_chunk` (AS-L2). The module docstring now says this
  planner is the production one.

**Commits.** `600aa620`, `18455f35`, `93150d2f`, `344b50e1`, `5b699bfe`,
`a7af296c`, `5aa8271c`, `93a77e56`, `e9cd0689` (write IDs), `8993843e`
(docstrings), `ae4c9807` (dead helper removed).

**Tests.** `tests/v1/layerwise/test_fetch_planner.py`,
`test_layer_fetch_plan.py`, and `tests/v1/distributed/rdma/test_slot_plan_parity.py`.

## AS-L2. Request-level fetch building

> **PR: PR-A6, with AS-L1.**
>
> - **Why combined:** `request_fetch` imports the planner.
> - **Rename:** make `object_key_to_string` public in the native adapter
>   (about 10 lines). This is already in PR-A3 if that merged first.
> - **Moves out:** `native_fetch` and `test_native_fetch` → PR-A10.
> - **Tests:** `test_request_fetch`, with `placers.py` and `vllm_requests.py`.
>   Needs the core native extension.

**What.**

- **`request_fetch.py`** builds a whole request's fetch.
  - `build_request_fetch` and `objects_to_place` select the objects to fetch.
    Since D-14, `build_request_fetch(write_ids=...)` reads the write IDs once
    per request. An object with none refuses the pipelined path.
  - `request_cache_keys` and `first_in_window_chunk` handle sliding windows.
  - The `ChunkPlacer` and `WindowLease` protocols are implemented by AS-P3.
  - `LeaseOutcome` reports how a lease ended.
  - `FetchModel`, `ModelRegistry` and `FetchModelRegistry` hold the fetch
    models built at registration.
- **`native_fetch.py`.** `pipelined_fetch_arguments(plan)` flattens a plan
  into the native call's arguments: one entry per slot.

**Commits.** `fa114939`, `7333280b`, `bea6f091`, `1280926c`, `77cbc154`,
`d4bd73c5`, `ad70e7cf`, `c4eaa29a`.

**Tests.** `tests/v1/layerwise/test_request_fetch.py`,
`test_native_fetch.py` and `vllm_requests.py` (request fixtures).

## AS-L3. Pipelined retrieve orchestration and deferral policy

> **PR: PR-A7.** Prerequisites: PR-G6, PR-A6 and PR-G8 (for `ResidentKeys`).
>
> - **Runs end to end in its own tests:** `run_pipelined_retrieve` through
>   `PackingPlacer`, the scripted source and the recording sink.
> - **Tests:** `test_pipelined_retrieve`, `test_shared_keys` and
>   `test_deferral`, all on fakes.
> - **Moves out:** `test_three_track_retrieve` → PR-A11.
> - **Library PR:** consumed by PR-A11.

**What.**

- **`pipelined_retrieve.py`.** `run_pipelined_retrieve` owns one retrieve:
  1. leases a window;
  2. places the objects;
  3. plans the fetch;
  4. runs `LayerArrivalPump` into the sink;
  5. on refusal or failure, falls back to a whole-object load
     (`_finish_from_whole_objects`) or reports recompute.
  - `resolve_shared_keys` handles keys another request holds, with
    `SharedKeysBusyError`.
  - It returns `PipelinedRetrieveResult` and `RetrieveCompletion`.
  - `PipelinedRetrieveRefused` covers refusals.
- **`deferral.py`.**
  - `PipelinedFetchConfig` holds the timeouts: shared-key wait 1.0 s, pump
    per-layer 1.5 s, whole-object load 1.5 s, and their sum
    `layer_publish_budget_seconds`.
  - `PipelinedModel`.
  - `PipelinedDeferral` (an `L2Deferral` from GF-10): defer only when one
    pipelined adapter holds all the L2 hits and the plan fits.
  - `SharedKeyPolicy` is `RECOMPUTE` or `WAIT`.

**Commits.** `bea6f091`, `2a3c104d`, `8ada78d7`, `ae529b58`, `004b692b`,
`b88ec0ff`, `6c913eb7`.

**Tests.** `tests/v1/layerwise/test_pipelined_retrieve.py`,
`test_shared_keys.py`, `test_deferral.py` and `test_three_track_retrieve.py`.

**Known cleanups.**

- D-03, partly fixed. Since `49971250`, `fetch_deferred_objects` lists the
  deferred objects once and reuses them for the lease and the whole-object
  fallback; keys that don't match the model raise before anything loads.
  Still open: `run_pipelined_retrieve` lists them again, and
  `build_request_fetch` serializes every key again. Fixing that means both
  take the listed objects instead of the keys, which touches about 30 call
  sites. The cost is about 1 ms per retrieve.
- D-04: defaults are duplicated between `MPServerConfig` and
  `PipelinedFetchConfig`, and the sliding-window rule is written four times.

---

# Part E — MP server wiring for the pipelined fetch ("C9")

## AS-M1. MP server flags

> **PR: PR-A11** (with all of AS-M). The flags have no consumer beyond
> validation until AS-M3 reads them.

`lmcache/v1/multiprocess/config.py` adds four flags:

- `--pipelined-fetch` requires `--use-layerwise` (enforced in validation).
- `--pipelined-max-chunks` defaults to 64.
- `--pipelined-shared-keys {recompute,wait}` defaults to `recompute`.
- `--pipelined-shared-wait-seconds` defaults to 1.0.

`engine_context.py` adds `pipelined_fetch` and `pipelined_models`, and
`server.py` installs the sink factory.

**Commits.** `7b292537`, `ae529b58`.

**Tests.** `tests/v1/multiprocess/test_config.py`.

## AS-M2. Lookup deferral and session deferred keys

> **PR: PR-A11.**
>
> - **Why it can't go earlier:** `_l2_deferral_for` returns a deferral only
>   when `ctx.pipelined_models` holds a model, which only AS-M3 fills.
>   `Session.deferred_keys` is read only by AS-M3's retrieve.
> - **Upstream drift:** the `lookup.py` hunk must be rewritten on prefetch
>   v2's `lookup.py` (`fold_unfold_grouped`). The `session.py` hunk applies
>   as is.

- **Lookup.** `modules/lookup.py`: `LookupModule._l2_deferral_for(key,
  num_kv_readers)` returns a `PipelinedDeferral` only for single-reader,
  world-size-1 lookups of a model with a registered pipelined setup. The
  lookup reads `query_prefetch_outcome` and records the deferred keys.
  `free_lookup_locks` skips deferred keys, which hold no lock.
- **Session.** `session.py`: `Session.record_prefetch_result(...,
  deferred_keys)`, `deferred_keys()`, and `claim_deferred_keys(keys)`, which
  hands each key to one retrieve only.

**Commits.** `7b292537`, `004b692b` (the reused-key leak).

**Tests.** `tests/v1/multiprocess/test_lookup_pipelined_deferral.py`,
`test_session.py` and `test_lookup_wait_prefetch.py`.

## AS-M3. Retrieve wiring, sinks and `fetch_deferred_objects`

> **PR: PR-A11, the feature PR for the whole pipelined path.**
>
> **Prerequisites:**
>
> - PR-G5 (MP layerwise load);
> - PR-G7 (`layerwise_sink.py`, moved out of this item);
> - PR-G8;
> - PR-A5, PR-A7 and PR-A10.
>
> **Hidden runtime dependency.** It imports no AS-P classes, but at run time
> it calls AS-P5's `StorageManager` accessors (`pipelined_window_placer`,
> `pipelined_max_*` and `pipelined_adapter_id`). Its MagicMock tests would
> pass without AS-P, but production would not work.
>
> **Re-port:**
>
> - the `protocols.base` imports in `lmcache_driven_transfer.py` and
>   `lookup.py` (upstream #5161 moved `HandlerType` to `request_handler`);
> - fit #5308's batch memcpy.
>
> **Also carries** `per_layer_staging_ranges` (out of GF-3) and
> `test_three_track_retrieve` (out of AS-L3).
>
> **Tests:**
>
> - mocks for the unit tests;
> - `test_three_track_retrieve`: fabric-free, needs `sink_fetch_table.cpp`;
> - A8 and T-RDMA-06: opt-in, real kv-sink server and RDMA.

- **Registration** (`modules/lmcache_driven_transfer.py`).
  - `register_kv_cache` calls `_register_pipelined_model`, which builds the
    fetch model and `PipelinedModel`.
  - It runs `check_staging_matches_plan`: if the planner's per-layer byte
    ranges differ from per-layer staging's, the model loads whole objects.
- **Retrieve** (same file). `retrieve` calls `_claim_deferred_keys` and
  `_resolve_deferred_keys`, then `fetch_deferred_objects`.
- **Loading** (`pipelined_loading.py`, new).
  - `fetch_deferred_objects` and `_WindowLoader`.
  - `DeferredLoad`, `PipelinedOutcome` and `DeferredFetchResult`.
  - `ObjectTable`, `PipelinedLoadRequest`, the `PipelinedSinkFactory`
    protocol and `check_staging_matches_plan`.
- **Sinks.**
  - `pipelined_sink.py` (new): `PipelinedRetrieveSink` and
    `MultiprocessPipelinedSinkFactory`.
  - `layerwise_sink.py` (new): `MultiprocessLayerLoadSink` implements
    `LayerLoadSink` with per-layer staging (GF-3), and `LayerLauncher`.

**Commits.** `7f75a0ba`, `48288b94`, `235f9a24`, `ae529b58`, `e3c665db`,
`004b692b`, `b88ec0ff`, `694eb6b8`, `49971250` (list the deferred objects
once; D-03, partly).

**Tests.**

- `tests/v1/multiprocess/test_pipelined_loading.py`,
  `test_lmcache_driven_deferred_retrieve.py`, `test_staging_matches_plan.py`
  and `test_lmcache_driven_layout_registry.py`
- `tests/v1/layerwise/test_multiprocess_sink.py`, `test_track_b_sink_shape.py`
  and `test_load_sink_conformance.py`
- `tests/v1/layerwise/multiprocess_sink_harness.py` and `aerospike_harness.py`
- the real-server integration tests:
  - `tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py`
    (A8): a retrieve through a real `StorageManager` with RDMA on, byte-exact
    against a kv-sink server.
    - The PR ported it to the batch-read protocol (`8993843e`), and it
      passes 3/3 on Soft-RoCE and on EFA v2.
    - `c09dee44` runs its late-write and region-release cases on the
      kv-sink server. The 1,025-lifetime region-release case is gated behind
      `RUN_AEROSPIKE_SLOW_INTEGRATION=1` (17 min on EFA).
    - `0ebf98d0` adds the D-15 "no write after `close()`" case.
  - `tests/v1/distributed/test_aerospike_rdma_byte_oracle_integration.py`
    (T-RDMA-06): pipelined RDMA vs plain get, in fetches of at most 4 chunks
    and over vLLM-stored keys (`eeb0f402`). Its recorded results are from the
    old protocol; `d8c20e00` dropped the old command limit from its
    docstring. Re-run it on the new one.

**Upstream overlap.** `lmcache_driven_transfer.py`, `lookup.py` and
`engine_context.py` conflict in the dry-run merge. The `test_qstore` fake
needed the sink factory (`694eb6b8`).

## AS-M4. Observability

> **PR: PR-A11.**
>
> - **Why combined:** the new fields are emitted only by AS-M3's retrieve.
> - **`L2_PREFETCH_DEFERRED` goes to PR-G8.** Note that v2 removed
>   `L2_PREFETCH_LOOKUP_COMPLETED`.

- **Event field.** `MP_RETRIEVE_END` gains `pipelined_outcome` and
  `deferred_count`.
- **Logging.** The logging subscriber prints them
  (`subscribers/logging/mp_server.py`).
- **Counter.** `lmcache_mp.num_deferred_retrieves`, with an `outcome`
  attribute, in `subscribers/metrics/mp_transfer.py`.
- **Prefetch event.** `L2_PREFETCH_DEFERRED` (with GF-10).
- **Docs.** `docs/design/v1/mp_observability/EVENTS.md`, `METRICS.md` and
  `docs/source/mp/observability/metrics.rst`.

**Commits.** `004b692b`, `0644e859`.

**Tests.** `tests/v1/mp_observability/subscribers/metrics/test_mp_transfer.py`.

## AS-M5. Layer publish budget vs worker wait

> **PR: PR-A11, all of it.** That covers:
>
> - the `layer_publish_budget_seconds` response field (deferred from GF-4);
> - the worker's `_check_layerwise_wait_covers_budget`;
> - the daemon side;
> - `test_layerwise_wait_budget.py`.
>
> The budget is non-zero only on the pipelined path. The response struct
> isn't array-encoded, so adding the field after PR-G5 is wire-compatible.

**What.** The daemon's waits (shared key, pump, fallback) sum to
`layer_publish_budget_seconds`, which is returned in
`RegisterKvCacheResponse` (GF-4). A layerwise worker refuses to register if
its per-layer wait is less than the budget plus 0.5 s. Without that check, a
worker timeout stops vLLM.

**Where.**

- `lmcache/v1/layerwise/deferral.py`
- `lmcache/v1/multiprocess/custom_types.py`
- `transfer_context/worker_transfer.py`: `_check_layerwise_wait_covers_budget`

**Commits.** `b88ec0ff`.

**Tests.** `tests/v1/multiprocess/test_layerwise_wait_budget.py`.

**Note.** It touches the GF-4 protocol. Both the response field and the
check ship in PR-A11, not with GF-4 (PR-G5); see the PR line above.

---

# Part F — Documentation

**Upstreamable design docs** (`docs/design/v1/distributed/l2_adapters/`).
They need trimming of track and process language before upstreaming:

| File | Lines | Content | Ship with |
| --- | --- | --- | --- |
| `aerospike_rdma.md` | about 720 (was 1,541; rewritten by the PR) | kv-sink batch-read protocol, bounded windows, write-lock TTL invariant, build, "what is proven" | PR-A8 |
| `aerospike_concurrent_writes.md` | 296 (new; D-14 decision record) | Per-write segment keys, create-only meta, pipelined write-ID read, measured costs | PR-B7, trimmed (pipelined part → PR-A6) |
| `layerwise_transfer_data_model.md` | 891 | Why one plane per record; hybrid models; economics | PR-A5 (record half), PR-A6 (planning half) |
| `layerwise-transfer-data-model.html` and `.check.js` | 3,405 | Interactive walkthrough plus its self-check | Probably **don't** upstream; host elsewhere |
| `rdma_testing_on_efa.md` | 151 | EFA setup, kv-sink EFA results, copied-tree build pitfalls | PR-A8 |
| `rdma_testing_on_windows.md` | 419 | Soft-RoCE in an Ubuntu VM on Windows | PR-A8 |
| `aerospike_server_issues.md` | about 390 | 11 issues on `sriram/kv-sink-batch-prio` (server and client); 4 open, 1 partly fixed | **Don't** upstream; hand to the Aerospike teams |

`aerospike_concurrent_writes.md` is one of the two merge-conflict files when
the PR merges.

**User docs.** `docs/source/mp/l2_storage/aerospike.rst` (AS-2) and
`docs/source/mp/observability/metrics.rst` (AS-M4). Upstream will also want a
user page for enabling RDMA, which doesn't exist yet. Write it for PR-A11,
the first PR a user can turn on.

---

# Part G — Internal material: do not upstream (AS-X)

| Path | What |
| --- | --- |
| `docs/design/v1/layerwise/` (13 files, about 5,700 lines) | Track acceptance (`track-a/b/c-acceptance.md`), `track-a-questions-for-track-c.md`, `track-c-status.md`, `contract-changes.md`, `c9-wiring.md`, `c9-bring-up.md`, `fetch-start-proposal.md`, `functional-test-plan.md`, `vllm-load-failure.md`, `exercise-goal.md`, `system-design.md`. Mine `system-design.md`, `c9-wiring.md` and `vllm-load-failure.md` for the real design docs; drop the rest |
| `functional/` (100 files, about 8,560 lines after the PR merges; was 42 and 3,540) | Test harness, ledger (D-01 to D-17), day and stage summaries, server configs, D-14 bench, D-17 S1 repro, kv-sink build notes |
| `.github/workflows/layerwise_track_b.yml` | Track-named CI; fold into a real workflow or drop |

**Host details are committed.** `functional/stage3/KVSINK-SERVER-BUILD.md`
(line 42) and `functional/stage3/C-CLIENT-SWITCH-PLAN.md` (line 74) contain
the test box's public IP, in `root@<ip>` scp commands. They came in with
`cf14c4b5` and `9c6661a1`. Redact them before pushing anywhere public; they
are already in the fork's history.

---

# Part H — External blockers and open defects

**After the PR**, `aerospike_server_issues.md` tracks the server branch
`sriram/kv-sink-batch-prio` (checked at `24357d20d`). It also tracks the
client fork at the same branch name (`769304f7`, based on client 7.5.0).
**All 11 issues of the old list are reported fixed on that branch.** That
list was against `feat/kv-sink-fetch-pipelined` and covered: record release
during the send, EFA write detection, the crash after a refused device,
fenced replies (D-07), lazy stripe registration (D-08), fixed device and
GID, concurrent fetches on one region, leaked regions and missing
read-touch.

The list below is the new one. Its statuses were re-checked at server
`046e8558d` and client `523d51ea` (`f91c1972` on the PR branch). **Open: 4,
5, 6, 9. Partly fixed: 1.**

| # | Issue | Side | Effect on LMCache | Status |
| --- | --- | --- | --- | --- |
| 1 | The local transport writes into any process on the server host | Server and client | **Security:** remote memory write. LMCache never uses `"local"` | Partly fixed: opt-in, no peer check |
| 2 | Queued writes have no deadline | Server | A late write after abandon could outlive the window quarantine (AS-P3; D-15) | Fixed in `046e8558d` |
| 3 | A failed RC write breaks the region with the wrong error | Server | Silent permanent fallback | Fixed in `046e8558d` |
| 4 | Region ownership is self-declared | Server | **Security:** silent wrong data; the window bound is only as strong as region ids are unguessable | **Open** |
| 5 | Only single-blob-bin records of at most 2 MiB can be sink-read | Server | Contract gap; caps AS-3's record size | **Open**; the limit is now advertised in the register reply |
| 6 | Placement runs on the completion poller | Server | Throughput ceiling | **Open** |
| 7 | Sink reads skip duplicate resolution, ping and filters | Server | Stale reads under strong consistency | Fixed (refused on SC namespaces) |
| 8 | RoCE hop limit is 1 | Server and client | No routed RoCEv2 | Fixed (`046e8558d`, `18b68325`) |
| 9 | The poller never sleeps when idle | Server | CPU | **Open** |
| 10 | One registration failure fails the whole sink | Client | One bad node means no pipelined fetch at all | Fixed in `18b68325`; LMCache logs partial registration (`f91c1972`) |
| 11 | `libaerospike.so` is not linked against libibverbs | Client | Needed a build-script workaround | Fixed in `18b68325`; workaround removed (`f91c1972`) |

**Ledger defects after the PR:**

| Defect | Status |
| --- | --- |
| D-07 (no overlap; fenced replies) | Obsolete: each layer's batch returns on its own. Overlap still unmeasured |
| D-08 (lazy stripes) | Obsolete: fixed on the new server branch |
| D-12 (late completion past 256 slots) | Very likely obsolete, since `kv-sink-fetch-pipelined` is gone. `0a339c63` on `prototype-stage1` "broadened" it on the old protocol; re-verify on the new one |
| D-11 (wrong GID index not named) | Moves to the client fork, which opens the device; re-check |
| D-15 (RDMA writes after `close()`) | Its LMCache cause (`AerospikePipelinedRdmaDriver::shutdown()`) was deleted, and server issue 2 is now fixed. Re-run `test_no_server_write_reaches_l1_after_close` on the new driver to close it |
| D-17 (S1: wrong output after a failed layerwise load with recompute) | Traced to vLLM ([vllm#49250](https://github.com/vllm-project/vllm/issues/49250)); not transport-specific. LMCache needs a startup guard in PR-G5; see GF-5 |

---

# PR plan

Every PR below builds and passes its own tests on upstream `dev` plus its
listed prerequisites. The **Kind** column says whether a user can reach it:

- **Feature:** a user can turn it on.
- **Tests:** tests of behavior upstream already has.
- **Library:** real feature code plus its unit tests. CI runs those tests
  from the day it merges, but no user path reaches the code until the PR
  named after "used by" wires it in.

**Decision (2026-10-01): keep the stack.** Library PRs stay separate, and
each names the PR that uses it. The fallback, if maintainers refuse, is
below.

The **Runs where** column says what a reviewer needs to run the tests.
Sizes are rough, from per-file numstat at `abebb20d`, split by hand.

| PR | Title | Contents | Prerequisites | Kind | Runs where | Size |
| --- | --- | --- | --- | --- | --- | --- |
| PR-A1 | Aerospike: batch exists | AS-1 | none | Feature | CE, in CI | ~+120 |
| PR-A2 | Aerospike: read-touch TTL for loads, never for lookups | AS-2 + `aerospike.rst` section | PR-A1 | Feature | CE 7.1+, in CI | ~+150 |
| PR-A3 | Aerospike: storage-integrity integration tests | AS-5 single-node, ported to prefetch v2; T-LKP-06; `object_key_to_string` made public; workflow entry | none | Tests | CE, in CI | ~+800 |
| PR-A4 | Aerospike: 3-node cluster integration tests | AS-5 cluster file, harness moved under `tests/`, `reserve_write` ported; T-FLT-10 | PR-B7 | Tests | 3-node cluster, opt-in | ~+850 + harness |
| PR-A5 | Aerospike: plane-aligned record sharding | AS-3 re-cut; AS-4's `max_record_bytes`; neutral `PlaneRun` module; `shard_plan_test`; `layerwise_transfer_data_model.md` (trimmed) | PR-B7 | Feature (storage format) | CPU CI + CE | ~+1,900 |
| PR-A6 | Layerwise: fetch planner and request fetch | AS-L1 + AS-L2 (no `native_fetch`); `read_write_ids` (native + Python); `record_digest_hex`; BF-8's pipelined half | PR-G6, PR-A5 (and so PR-B7), PR-A1 | Library; used by PR-A7, PR-A10 | CPU + core native ext; CE for the layout test | ~+3,500 |
| PR-A7 | Layerwise: pipelined retrieve orchestration and deferral | AS-L3 | PR-G6, PR-A6, PR-G8 | Library; used by PR-A11 | fakes, CPU | ~+1,500 |
| PR-A8 | Aerospike RDMA: kv-sink batch-read fetch (native) | AS-R1 + trimmed AS-R4 + AS-R5 + AS-R6's RDMA harnesses + slot-plan parity test + `l1_rdma_registration.h`; `aerospike_rdma.md` and the testing guides | PR-A5, PR-A6 | Library; used by PR-A9, PR-A10 | Logic harnesses in CPU CI (make, g++, pybind11). **Building the RDMA extension needs the client fork** | ~+2,000 code, +1,300 tests |
| PR-A9 | Distributed: L1 RDMA windows | AS-P1 + AS-P2 + GF-8 + GF-9, ported onto #5247 / #5068 / #5393 | PR-A8 | Library; used by PR-A10 | fabric-free, CPU CI | ~+1,400 code, +960 tests |
| PR-A10 | Distributed: window leasing, Aerospike arrival source, storage-manager hooks | AS-P3 + AS-P4 + AS-P5, plus `native_fetch` | PR-A6, PR-A7, PR-A8, PR-A9, PR-G6 | Library; used by PR-A11 | fabric-free (pybind11) | ~+1,700 code, +2,450 tests |
| PR-A11 | MP: pipelined retrieve (feature) | AS-M1 to AS-M5, `per_layer_staging_ranges`, `test_three_track_retrieve`, A8 and T-RDMA-06 opt-in tests | PR-G5, PR-G7, PR-G8, PR-A5, PR-A7, PR-A10 | **Feature** (`--pipelined-*`) | unit tests on mocks; **end to end needs the kv-sink server, client fork and RDMA (or Soft-RoCE)** | ~+4,200 |

## What "functional" can and can't mean here

- **Stand on their own now:** PR-A1 to PR-A5, opened against today's `dev`.
  - PR-A1, PR-A2 and PR-A5 change behavior users get.
  - PR-A3 and PR-A4 test behavior upstream already has.
- **Tested but not user-reachable:** PR-A6 to PR-A10 are libraries until
  PR-A11. Each passes its own tests on fakes or fabric-free harnesses, and
  each description must name PR-A11 as the consumer.
- **Fallbacks, if maintainers refuse library PRs:**
  - The only way to make every RDMA PR user-reachable is to merge PR-A8 to
    PR-A11 into one PR. That PR would be roughly +9,000 code and +5,000
    tests, past what the coding standards call reviewable.
  - A compromise: keep PR-A8 (native) and PR-A9 (L1 windows) separate, and
    merge PR-A10 into PR-A11.
- **Upstream can't build or run any of PR-A8 to PR-A11 end to end** until
  the kv-sink server and the client's sink API ship in official Aerospike
  releases. Drop `.deps/build_aerospike_client_kvsink.sh` once they do.

## Order across all three files

Cross-file prerequisites decide the order. PR-B* is in
[bug_fixes.md](bug_fixes.md) and PR-G* in
[general_features.md](general_features.md).

1. **Now, all independent:**
   - PR-B1 to PR-B7 (PR-B4 after PR-B3);
   - PR-G1, PR-G2;
   - PR-A1, then PR-A2;
   - PR-A3.
2. **After PR-B7:** PR-A4 and PR-A5.
3. **After agreeing the design against upstream #4460:** PR-G3, then PR-G4,
   then PR-G5 (MP layerwise load, user-visible).
4. **Library groundwork,** shortly before the pipelined path:
   - PR-G6;
   - PR-G8;
   - PR-G7 (after PR-G4);
   - then PR-A6, then PR-A7.
5. **After the kv-sink server and client ship:** PR-A8, PR-A9, PR-A10 and
   PR-A11.
