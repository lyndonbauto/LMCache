# ROCm event IPC backend

`lmcache/v1/platform/rocm/event_ipc.py` provides `RocmEventIPCBackend`, the
`EventIPCBackend` that `RocmDeviceSpec` returns when isolated IPC is off. It
gives ROCm the event semantics LMCache relies on and CUDA provides. It does
this without native HIP interprocess events: each event is a 64-bit counter
in shared memory that the GPU writes and waits on.

## Why native HIP events don't work here

LMCache orders cross-process transfers with CUDA's rule: `cudaStreamWaitEvent`
waits for the record the event holds **when the wait is enqueued**. The
layerwise path depends on it. The daemon creates one event per layer at
registration and re-records it on every retrieve; the worker imports each one
once and waits on it for every retrieve.

On ROCm 10 an interprocess event is a ROCr IPC signal, and three behaviours
break that. Each was reproduced with two processes on an MI300X:

| Behaviour | Effect on LMCache |
| --- | --- |
| A queued wait blocks on the signal's live value. If the event is re-recorded before the waiting stream reaches the wait, the wait blocks on the new record (a probe finished at 8.6 s instead of about 3 s). | With vLLM's async scheduling, step N+1's retrieve re-records a layer's event while step N's forward pass still has a wait on it queued. Step N then waits for step N+1's copy, which is queued behind step N: a deadlock with 4 or more concurrent cached requests. |
| A process can open a given handle only once, even after dropping the first import. | Several requests in one step carry the same event, so the second import failed. |
| Handle bytes repeat once the exporter frees an event, and exporting onto a freed address can fail while a peer still has it attached. | Handles can't be cached or recycled safely. |

## Design

Each process owns one POSIX shared-memory region, `lmcache_rocm_evt_<pid>_<random>`,
registered with `hipHostRegister` (portable and mapped) in every process that
uses it:

```
header (64 B) | values[slots] : uint64  | published[slots] : uint64
                 written by the GPU        epoch << 40 | seq, written by
                 when a record lands       the owner when it enqueues one
```

| Operation | What it does |
| --- | --- |
| `create_event` | Take a free slot and bump its epoch |
| `record_event(stream)` | `seq += 1`; on `stream`, wait until `values[slot] >= seq - 1`, then `hipStreamWriteValue64(values[slot], seq)`; publish `(epoch, seq)` |
| `export_event` | `LMRS`, version 2, region name, slot, epoch |
| `import_event` | Attach the exporter's region once per process; return a wait-only event |
| `wait_event(stream)` | Read the event's published `seq` now and `hipStreamWaitValue64(values[slot] >= seq)` |
| `query_event` | `values[slot] >= seq`, read on the host |
| `synchronize_event` | The same wait on a per-thread stream, then `hipStreamSynchronize` |

Why it's correct:

- **A wait can't be retargeted.** It waits for a number, not a signal. A later
  record raises the counter past that number, so it can only release the wait,
  never hold it back.
- **Counters only increase.** Records of a slot are serialized under a lock,
  and each waits on its stream for the previous one before writing. Records
  from different streams therefore still land in order, and `>= seq` means
  "this record and everything recorded before it".
- **Recycled slots can't confuse old handles.** A released slot (the event was
  garbage-collected) is reused only after its last record has landed, and
  reuse bumps the epoch. A handle with an older epoch therefore names an event
  whose last record has completed, so waiting on it is a no-op, as waiting on
  a finished CUDA event is.
- **Imports are cheap and repeatable.** An import only attaches a region once
  per process; there's no per-handle HIP import to exhaust.
- **Only the exporter records.** Recording an imported event raises
  (system-design invariant 3).

## Requirements and limits

- Both processes must share `/dev/shm`, for example `--ipc host` between
  containers. The layerwise progress record already needs this, and CUDA IPC
  events need it too.
- 4096 live events per process by default (`slot_count`). `create_event`
  raises once they're all held by live events or unfinished records.
- The per-slot counter has 40 bits and the epoch 24 bits. Neither wraps in
  practice (about 10^12 records and 1.6 × 10^7 reuses per slot).
- The owner unlinks its region at exit; importers keep their mapping until
  they exit.

## Tests

`tests/v1/platform/test_rocm_event_ipc.py`:

- **Semantics, over a fake HIP layer whose streams run only when a test drains
  them:**
  - a queued wait isn't retargeted by a re-record;
  - records from two streams land in order;
  - an unfinished slot isn't reused, and reuse bumps the epoch;
  - concurrent records get distinct, increasing numbers;
  - format and origin checks.
- **Real HIP, two processes (ROCm GPUs only):**
  - the retargeting probe as a test: the consumer's marker must land in under
    6 s, where native events take about 9 s;
  - 20 rounds of producer writes, each followed by a record, with the
    consumer checking the data after every wait, including recycled events.

`tests/v1/platform/test_event_ipc_ordering.py` also runs against this backend
on ROCm, through `get_event_ipc_backend`.
