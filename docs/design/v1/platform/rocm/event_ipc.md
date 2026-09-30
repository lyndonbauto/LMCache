# ROCm event IPC backend

`lmcache/v1/platform/rocm/event_ipc.py` provides `RocmEventIPCBackend`, the
`EventIPCBackend` that `RocmDeviceSpec` returns when isolated IPC is off. It
keeps native HIP interprocess events but changes their lifetimes so that
ROCm's handle rules hold.

## Why ROCm needs its own backend

On ROCm 10 (checked on an MI300X), an interprocess event is backed by a ROCr
IPC signal. The 64-byte handle is roughly `type | creator pid | signal
address`. Destroying an event only queues its signal for deferred cleanup.
Three behaviours follow, all reproduced with two processes:

| Behaviour | Effect with the CUDA-style default backend |
| --- | --- |
| A process can open a handle only once; a second `hipIpcOpenEventHandle` fails with `hipErrorInvalidValue`, even after the first import is dropped. | The worker records one event per step and every request carries its handle, so the server's second request in a step fails to import it. |
| Handle bytes repeat once the exporter frees a signal address (2 distinct handles over 6 create/destroy cycles). | A cache keyed by handle bytes could hand back an import of a dead event. |
| After a peer has imported an event, the exporter can fail to export a new event that lands on the freed address. | Per-transfer events can fail to export on later steps. |

The timeline-semaphore backend (`cuda/timeline_semaphore_event_ipc.py`) is not
a substitute. It snapshots the sequence number at export, whereas the
layerwise path exports each per-layer event once at registration and then
re-records it on every retrieve. The worker's GPU wait on that re-recorded
event is what orders compute behind the copy.

## Design

```
exporter process                          importer process
----------------                          ----------------
create_event()  -> lease from pool
record_event(lease, stream)
export_event(lease) -> MAGIC|nonce|native ---> import_event(bytes)
drop lease -> native back to pool                cache[(bytes, device)] hit?
                                                   yes: same event object
                                                   no:  from_ipc_handle once
```

1. **Pooled exports.** `create_event` returns a `RocmPooledEvent` lease. When
   the lease is garbage-collected, its native event goes back to the device's
   pool and is never destroyed. Signal addresses are therefore never freed,
   and a handle always names one live event. `create_event` prefers a pooled
   event whose last record has completed: ROCm makes a re-record wait for the
   previous record, so reusing a busy event would stall the host.
2. **Once-only imports.** `import_event` caches the imported event by
   `(exported bytes, device index)` and opens each handle once, under a lock.
3. **Exporter nonce.** Exports are `b"LMRE\x01" + 16-byte random nonce +
   native handle`. A restarted peer that reuses a pid and signal address gets a
   new nonce. That makes it a cache miss, which evicts the stale entry, instead
   of a stale hit that would wait on a dead signal and skip ordering. Both
   processes must therefore use this backend. `import_event` rejects any other
   format with a `RuntimeError`.

## Recycled events can make a peer wait longer

A pooled event can be re-recorded for a new transfer before a peer has waited
on its previous use. The peer then waits for the newer record, which is
safe:

- A handle leaves the exporter only after the record it describes has been
  enqueued, so a peer never waits on a record that is still to come.
- The newer record follows only work enqueued before it. None of that work
  waits on the late peer, so the longer wait cannot deadlock.

## Tests

`tests/v1/platform/test_rocm_event_ipc.py` covers:

- one open per handle, both sequential and concurrent;
- lease reuse, busy events not being reused, and per-device pools;
- the restarted-exporter case, format rejection, and operation delegation;
- a two-process HIP test (ROCm GPUs only): repeated imports, then a recycled
  event whose re-record the importer observes.
