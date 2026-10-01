# Aerospike adapter: concurrent writes of one key (D-14)

**Status:** Decided (option 2) and implemented in e9cd0689; the pipelined
path uses sub-option A (one extra batch read of the meta records).
**Decided by:** Lyndon Bauto, 2026-10-01 ("we may want to revisit"); sub-option
A chosen 2026-10-01 19:30Z.
**Defect:** D-14 (S2) in [`functional/LEDGER.md`](../../../../../functional/LEDGER.md),
found by T-FLT-10.

## Problem

A sharded object is stored as one metadata record plus `nseg` segment
records (`csrc/storage_backends/aerospike/connector.cpp`):

```text
<key>|m          meta: state=ready, nseg, seg_bytes, plane_bytes, runs, total
<key>|s|0 .. <key>|s|<nseg-1>    payload segments
```

`do_single_set` writes the segments with `AS_POLICY_EXISTS_IGNORE`, then the
metadata record. Segment keys are fixed per key, and nothing in the metadata
record names *which* write its segments belong to. Two writers of one key
therefore overwrite each other's segments, and a reader sees whatever mix is
there when it reads. A reader during a single rewrite can see the same mix.

Measured in T-FLT-10 (two writer processes released together on one key,
read by a third adapter, 3-node CE 8.2 at RF 2):

| Object | Rounds | Mixed |
| --- | --- | --- |
| Sharded, 3 MiB in 4 segments of 768 KiB | 1,000, twice | 141 and 145 |
| Inline, 64 KiB (meta record only) | 1,000, twice | 0 and 0 |

Mixing is always at whole-segment granularity. Output impact today is small,
because LMCache keys are content hashes and two engines produce equal or
near-equal bytes for one key, but the store gives no guarantee. A create-only
metadata write alone does not fix it: by the time the loser's metadata put
fails, the loser has already overwritten the winner's segments.

Evidence: box `/root/lmc-work/functional/stage6/logs/flt10.txt`,
`cluster_it_full.txt`; commit 9367ef65.

## Options considered

1. **Per-write segment keys.** Each store picks a write ID and writes
   `<key>|s|<wid>|<i>`; the metadata record, written last, carries `wid`.
   A reader always sees one writer's set. Last writer wins; the old set is
   deleted best effort and otherwise expires by TTL.
2. **Option 1 plus create-only metadata.** The metadata put uses
   `AS_POLICY_EXISTS_CREATE`. The first writer wins; a loser deletes its own
   segments and reports success. Nobody ever deletes segments another writer
   might be reading.
3. **Accept.** Payloads are near-equal; document the gap.

## Decision

**Option 2.** It is the only option where no writer touches records another
writer's metadata points at, so a reader never races a delete of the set it
was told to read. "First writer wins" is safe because keys are content
hashes. Decided by Lyndon Bauto on 2026-10-01, with the note that we may want
to revisit it. The pipelined path learns the write ID with one batch read of
the meta records before it plans (sub-option A in
[Read-path cost](#read-path-cost)).

## Protocol

**Key derivation.** For each store, the writer draws a random 64-bit write ID
and encodes it as 16 lowercase hex digits:

```text
meta:     <key>|m                       bins: ..., wid = "9f3c0a17e2b45d68"
segment:  <key>|s|9f3c0a17e2b45d68|<i>  i = 0 .. nseg-1
```

Inline objects (`nseg == 1`, payload in the metadata record) have no
segments and no `wid`; create-only applies to their metadata record too.

**Write order.**

1. Draw `wid`.
2. Put every segment `<key>|s|<wid>|<i>` (unchanged policy; the keys are
   unique to this write).
3. Put the metadata record with `wid` and `AS_POLICY_EXISTS_CREATE`.

**Create-only outcome.**

| Metadata put returns | Meaning | Writer does |
| --- | --- | --- |
| `AEROSPIKE_OK` | This write won | Returns success |
| `AEROSPIKE_ERR_RECORD_EXISTS`, existing object intact | Another write (or an older object) holds the key | Deletes its own `nseg` segments best effort, returns success: the key is present |
| `AEROSPIKE_ERR_RECORD_EXISTS`, existing object names an absent segment | The holder is damaged | Removes it (guarded, see [Self-heal](#side-effects)) and retries the create once |
| Any other error, not in doubt | Store failed | Deletes its own segments best effort, raises as today |
| Any other error, `err.in_doubt` | The meta record may have been written | Keeps its segments (they may be named), raises; if the meta put did not land, the segments are orphans until the TTL |

A failed segment put deletes the segments written so far and raises; no meta
record is attempted.

**Reader rules.**

1. Read the metadata record. Not found or not `ready`: miss (unchanged).
2. If it has a `wid` bin, read `<key>|s|<wid>|<i>`.
3. If it has no `wid` bin, it is an old-layout record: read `<key>|s|<i>`.
   Old records stay readable (the T-STO-06 rule) and keep the D-14 exposure
   until they expire.
4. A missing segment is a miss, as today (T-STO-04). The reader deletes
   nothing; the next store of the key replaces the object (see
   [Self-heal](#side-effects)).

**Delete and client LRU.** `do_single_delete` already reads the metadata
record for `nseg`; it also reads `wid`, removes the metadata record, then
removes the segments that `wid` (or the old layout) names.

**TTL.** Every record keeps `default_ttl_seconds`. Segments are written a few
milliseconds before their metadata, so they expire that much earlier; a read
in that window is a clean miss (unchanged). Orphans expire by the same TTL.

## Read-path cost

| Path | Extra round trips | Why |
| --- | --- | --- |
| Plain get | 0 | The metadata record is read first today; `wid` comes back in the same read |
| Exists / batch exists | 0 | Header-only metadata read, unchanged |
| Delete / client LRU | 0 | Already reads the metadata record for `nseg` |
| Store, no conflict | 0 | Create-only instead of ignore on the same put |
| Store, lost race | +1 meta select, up to `nseg` header reads, `nseg` deletes | Loser checks the winner is intact, then removes its own segments |
| Whole-object `kv-sink-fetch` | 0 | No product caller (`build_fetch_command` is used only by `rdma_equivalence_test.cpp`) |
| Pipelined fetch | +1 batch read | Reads every planned object's `wid` before planning (below) |

**Pipelined path (sub-option A, implemented).** `RecordKeys.record_key_for`
in `lmcache/v1/layerwise/planner.py` used to build `f"{cache_key}|s|{index}"`
from the layout alone; the keys become digests in `kv-sink-fetch-pipelined`,
so the planner must know each object's `wid` first. Now:

1. `build_request_fetch` calls `source.read_write_ids(sorted(cache_keys))`
   once. On Aerospike that is the native `read_write_ids`: one
   `aerospike_batch_read` of the `state` and `wid` bins (no TTL touch), in
   batches of `kMaxBatchExistsKeys`, parallel across nodes.
2. `RecordKeys` takes the key-to-`wid` map and names
   `<key>|s|<wid>|<i>`, or `<key>|s|<i>` for `wid == ""` (old layout).
3. A key with no ready meta record, or a failed read, refuses the retrieve
   (`PipelinedRetrieveRefused`); it loads whole objects instead.
4. If a winner's meta record is replaced between this read and the fetch,
   the named segments are gone, the layer is unservable and the object loads
   whole: a fallback, never mixed bytes.

The contract change is recorded in
[`contract-changes.md`](../../layerwise/contract-changes.md).

Measured on the box (lmc-d, CPU only, loopback, 3 MiB objects, p50 of 300
calls, `functional/d14/scripts/read_cost.py`):

| Operation | 1 key | 4 keys | 16 keys | 64 keys |
| --- | --- | --- | --- | --- |
| `read_write_ids`, one CE 8.2 node | 25 µs | 47 µs | 63 µs | 129 µs |
| `read_write_ids`, 3-node cluster, RF 2 | 24 µs | 131 µs | 223 µs | 358 µs |
| For scale: one meta get | 25 µs | | | |
| For scale: whole 3 MiB load (meta + 4 segments), one node / cluster | 1.0 ms / 1.3 ms | | | |

So a pipelined fetch of up to 4 chunks (the D-12 cap) pays about one meta
read on one node and about 0.13 ms on the cluster, where the batch fans out
to several nodes, before its first layer. The plain path pays nothing.

Alternatives kept for a revisit:

| Sub-option | How the planner gets `wid` | Cost |
| --- | --- | --- |
| B. Carry it from the lookup | `batch_exists` also reads `wid`; the adapter keeps key-to-`wid` until the retrieve. A stale `wid` makes a segment missing, so the layer is unservable and the object reloads whole | 0 RTT; couples lookup to retrieve, adds a cache and plumbing through the MP server |
| C. Defer | The pipelined path refuses new-layout objects and loads them whole | Track C loses layer pipelining for every new object |

## Side effects

- **Self-heal on store (implemented).** Before, a re-store overwrote a
  damaged object. With create-only metadata, a meta record whose segment was
  lost or evicted would block every re-store until its TTL
  (`default_ttl_seconds`, 86,400 s by default). So a store whose create-only
  put returns `RECORD_EXISTS` calls `remove_damaged_object` before giving up:
  1. select the meta record's `nseg` and `wid` (and its generation);
  2. `exists` each named segment; stop at the first `RECORD_NOT_FOUND`. Any
     other error, or every segment present, leaves the object alone (the
     store lost: it deletes its own segments);
  3. remove the meta record with `AS_POLICY_GEN_EQ` at the generation from
     step 1, then that write's segments;
  4. retry the create-only put once. `RECORD_EXISTS` again means a
     concurrent store took the freed key, and this store lost.

  **Why on store, not on read.** The first version dropped the object in
  the reader when a segment read returned not-found. At RF 1 a segment reads
  as not-found while its node is down, and comes back when the node rejoins,
  so the reader turned a transient outage into a permanent miss: T-FLT-04's
  "every key byte-exact after the node rejoins" went from 3 misses (the known
  CE flush loss) to 13. On store, a damaged object is replaced by a complete
  one, so nothing is lost; a reader never deletes.

  **Check-then-remove window.** A filter expression (`wid == <read wid>`) on
  the remove would close it, but the C client's `as_exp_*` macros use nested
  designators that g++ rejects in C++ (hipcc accepts them with a warning), so
  the generation check alone guards it. If the meta record is deleted and
  re-created between steps 1 and 3 (create-only records start at generation
  1), the remove can take the new winner's meta record. The store then
  creates its own complete object, so a reader still sees one write's bytes;
  the new winner's segments are orphans until the TTL.

  Cost: only a store that loses a race pays it: one meta select plus up to
  `nseg` header reads. The pipelined path never deletes; a missing segment
  there makes the layer unservable and the retrieve loads the object whole.
- **Orphan segments.** Per-write keys are never reused. A writer that dies
  before its metadata put, or a loser whose cleanup fails, leaks segments
  until the TTL. There is no sweeper; accepted for now.
- **First writer wins.** A store of an existing key no longer replaces it.
  Safe for content-hash keys. Tests that re-store a key with different bytes
  must delete it first.

## Timeline

Before (fixed segment keys, ignore policy), writer A and B store key K with
4 segments:

```text
time ->
A:  s|0      s|1           s|2  s|3       meta(A)
B:      s|0      s|1  s|2            s|3          meta(B)
records after: s|0=B  s|1=B  s|2=A  s|3=B, meta "ready"
reader: reads all four under one meta record -> mixed object, stays mixed
```

After (option 2):

```text
time ->
A:  s|a|0  s|a|1  s|a|2  s|a|3   meta(wid=a, CREATE) -> OK
B:     s|b|0  s|b|1  s|b|2  s|b|3        meta(wid=b, CREATE) -> EXISTS
B:                                        delete s|b|0..3
reader: meta says wid=a -> reads s|a|0..3 only; B's records are never named
```

## Tests

| Test | Checks |
| --- | --- |
| T-FLT-10 (`test_aerospike_cluster_integration.py`, sharded and inline, RF 1 and RF 2, 1,000 rounds) | Every read equals one writer's bytes; after each round the reader deletes the key, and at the end the set holds 0 records, so no loser's segments survive |
| `test_a_second_store_of_a_key_keeps_the_first_and_leaves_no_records` (sharded, inline) | Create-only: the second store reports success, the first bytes stay, the loser's segments are gone |
| `test_a_store_replaces_an_object_whose_segment_is_missing` | A meta record naming a missing segment is a miss and the read deletes nothing; the next store replaces the object and leaves no stale record |
| T-FLT-02/04 RF 1 (`test_rf1_node_killed_mid_fetch_...`) | Keys missed while a node is down load again once it rejoins (the reader must not delete) |
| `test_a_delete_removes_the_segments_its_meta_record_names` | Delete and LRU remove the named segments |
| `test_an_object_written_before_write_ids_existed_is_still_readable` | T-STO-06: an old-layout object loads, reports `wid == ""`, plans `<key>\|s\|<i>`, and deletes cleanly |
| `test_write_ids_name_sharded_inline_and_absent_objects`, `test_request_fetch.py`, `test_fetch_planner.py` | The pipelined path names the winning write's records and refuses objects without a meta record |

T-FLT-10 used to read the same key every round, so it could not see a lost
race's leftovers. It now deletes the key after each round's read: every
round is a race of two first stores, and the final record count proves the
loser cleaned up.

Result after the fix (3-node CE 8.2, `functional/d14/`):

| Object | RF 1 | RF 2 |
| --- | --- | --- |
| Sharded, 3 MiB in 4 segments | 0 mixed of 1,000, 0 records left | 0 mixed of 1,000, 0 records left |
| Inline, 64 KiB | 0 mixed of 1,000, 0 records left | 0 mixed of 1,000, 0 records left |

Before: sharded 141 and 145 mixed of 1,000 (RF 2), inline 0.

## Open questions

1. Should a loser's cleanup be synchronous (adds `nseg` deletes to a lost
   race; current) or queued?
2. Is a 64-bit random write ID enough, or should it include a host/process
   tag for debugging orphans?
3. Orphans from writers killed between their segments and their meta record
   persist until the TTL. Add a sweeper if T-STO-03-style kill runs show the
   volume matters.

## How to revisit

- **If last-writer-wins is needed** (keys stop being content hashes, or a
  re-store must replace bytes): keep per-write keys, switch the metadata put
  back to replace with a generation check, and delete the previous `wid`'s
  segments after the swap (option 1). Readers that already hold the old
  `wid` may then miss; that is the cost option 2 avoids.
- **If orphan volume matters:** measure orphaned segments after a T-FLT-10
  run (records whose `wid` no metadata names) and add a sweeper.
- **Regression test:** T-FLT-10 (0 mixed of 1,000 rounds, sharded and inline)
  plus T-STO-06 for old-layout records.
