# Aerospike adapter: concurrent writes of one key (D-14)

**Status:** Decided (option 2), pipelined-path variant pending.
**Decided by:** Lyndon Bauto, 2026-10-01 ("we may want to revisit").
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
to revisit it. The plain path is settled; how the pipelined path learns the
write ID is pending (see [Read-path cost](#read-path-cost)).

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
| `AEROSPIKE_ERR_RECORD_EXISTS` | Another write (or an older object) holds the key | Deletes its own `nseg` segments best effort, returns success: the key is present |
| Any other error | Store failed | Deletes its own segments best effort, raises as today |

**Reader rules.**

1. Read the metadata record. Not found or not `ready`: miss (unchanged).
2. If it has a `wid` bin, read `<key>|s|<wid>|<i>`.
3. If it has no `wid` bin, it is an old-layout record: read `<key>|s|<i>`.
   Old records stay readable (the T-STO-06 rule) and keep the D-14 exposure
   until they expire.
4. A missing segment is a miss, as today (T-STO-04). See
   [no self-heal](#side-effects) for the proposed guarded delete.

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
| Store, lost race | +`nseg` deletes | Loser removes its own segments |
| Whole-object `kv-sink-fetch` | 0 | Removed with the info-command protocol; no caller |
| Pipelined fetch | depends on sub-option | See below |

The pipelined path is the hard case. `RecordKeys.record_key_for` in
`lmcache/v1/layerwise/planner.py` builds `f"{cache_key}|s|{index}"` from the
layout alone and never reads the metadata record; the keys become the keys
of the sink batch-read rows. Under per-write keys it must know each object's
`wid` first. Shipping the plain fix alone would make every pipelined fetch of
a new object name records that do not exist and fall back to whole loads
(and fail `test_aerospike_pipelined_rdma_integration.py`).

| Sub-option | How the planner gets `wid` | Cost | Estimate |
| --- | --- | --- | --- |
| **A. One extra batch read (recommended)** | Native `read_write_ids(keys)`: one `aerospike_batch_read` of the `wid` bin, exposed on `LayerArrivalSource`; `RecordKeys` takes a key-to-`wid` map. Missing metadata refuses the fetch (whole-load fallback) | +1 RTT before the first layer, parallel across nodes; contract change in `contract-changes.md`; 4 test fakes | 1.5-2 h on top of the plain fix |
| B. Carry it from the lookup | `batch_exists` also reads `wid`; the adapter keeps key-to-`wid` until the retrieve. A stale `wid` makes a segment missing, so the layer is unservable and the object reloads whole | 0 RTT; couples lookup to retrieve, adds a cache and plumbing through the MP server | about +3 h |
| C. Defer | Plain path now; the pipelined path refuses new-layout objects and loads them whole | Track C loses layer pipelining for every new object until A or B lands | fast |

The pipelined path has the same exposure today: it reads fixed-key segments
without checking the metadata record.

## Side effects

- **No self-heal.** Today a re-store overwrites a damaged object. With
  create-only metadata, a metadata record whose segment was lost or evicted
  blocks every re-store until its TTL (`default_ttl_seconds`, 86,400 s by
  default). Proposed: a reader
  that finds a named segment missing returns a miss *and* deletes the
  metadata record with a generation check (`AS_POLICY_GEN_EQ`, generation
  from the metadata read), so the next store can win and a newer winner is
  never deleted by mistake.
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

## Open questions

1. Pipelined sub-option: A (recommended), B or C. Asked 2026-10-01 19:03Z.
2. Is the guarded delete-on-missing-segment in scope of the D-14 fix, or a
   follow-up? Without it a damaged object is stuck until TTL.
3. Should a loser's cleanup be synchronous (adds latency to a lost race) or
   queued?
4. Is a 64-bit random write ID enough, or should it include a host/process
   tag for debugging orphans?

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
