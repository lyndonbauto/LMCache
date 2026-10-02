# Why layer-by-layer (lw) is ~5x slower than all-or-nothing (aon) in perf phase 1

Investigation of the code (`origin/prototype-stage1` 41b97f03, kv-sink server
`sriram/kv-sink-batch-prio` 046e8558d as built on the box) and of the phase 1 logs on the box
(`/root/lmc-work/functional/perf/`). Read-only; nothing was run on the box.

## Verdict

**The two modes do not use the same transport, and the two transports differ ~6x in rate.**
aon reads whole objects with ordinary TCP `aerospike_key_get` calls (8 parallel client
workers against 100 server service threads). lw reads with sink batch reads: the server
RDMA-writes every 512 KiB record over Soft-RoCE, one region, one RC queue pair, at most
4 MiB in flight, placed one record at a time. Measured rates: aon **6.1-6.5 GiB/s**, lw
**1.03-1.07 GiB/s**, the same at 600 tokens, 8k, 16k and 128k, so both modes are
bandwidth-bound, not latency-bound.

On a full-prefix hit there is no compute to hide the fetch behind (vLLM computes one token),
so lw TTFT = time to land the last layer = bytes / 1.03 GiB/s + ~50 ms. Layer 0 does land
early (requested first, and the server places it first), but that does not shorten TTFT
when nothing can use it. Nothing in the layerwise logic is slow: per-layer client overheads
total < 5 ms per retrieve.

**Recommendation:** finish the full-hit matrix with aon only, as Lyndon suggests. lw on full
hits over Soft-RoCE cannot be made comparable without server changes (section 6). Run the
cheap experiments in section 7 to document lw where it can win: the TCP-transport layerwise
control, `ib_write_bw` scaling, and a partial hit with a short cached prefix.

## 1. Timelines (LMCache server logs, DEBUG)

**lw, 8k, c=1** (`L8192_lw/lmcache_L8192_lw.log`, request `cmpl-b6ccc791…`):

| t (s) | Event |
|---|---|
| 19:48:19.525 | lookup: 32/32 L2 hits, **32 deferred**, prefetch "completed" in 1.9 ms (no bytes moved) |
| 19:48:19.532 | `MP retrieve start`; window leased (`L1 write reserved: 32 keys`) |
| (not logged) | layer 0 resident: estimated +35 ms (32 MiB of layer 0 at 1.03 GiB/s, plus the meta-record read) |
| 19:48:20.498 | last layer resident, window released (`L1 write finished`) |
| 19:48:20.499 | `Retrieved 8192 tokens in 0.968 seconds`, `pipelined_outcome=pipelined` |
| TTFT | 1.03 s (client). 1 GiB in 0.968 s = **1.03 GiB/s** |

Server side for that point: `kv-sink: deregistered region … - 8320 writes 4362076160 bytes`.
That is 4 x 2048 + 128 warm-up writes at exactly **524,288 bytes (512 KiB) each**. Between
the 19:48:17 and 19:48:27 ticker dumps, 4,534 of 5,798 sink rows (78%) waited **256-511 ms**
server-side (`{lmcache}-batch-sub-read` histogram, bucket 09). The per-region queue is deep
and drained at a fixed rate.

**lw, 8k, c=4** (same log; 4 requests sent together at 19:50:35.41):

| Request | retrieve start | retrieve end | took | TTFT (client) |
|---|---|---|---|---|
| `9b040b7e` | 35.450 | 36.458 | 1.013 | 4.06 |
| `80c922bf` | **36.459** | 37.438 | 0.979 | 4.06 |
| `b2a8e475` | **37.438** | 38.445 | 1.005 | 4.06 |
| `9034cad4` | **38.449** | 39.439 | 0.990 | 4.06 |

All 4 lookups finished by 35.435. Each retrieve starts within 1-4 ms of the previous one's
end: **strictly serialized**. All 4 TTFTs are equal (4.067/4.057/4.056/4.056), and at c=2
both are 2.02. The 4 requests are in one vLLM forward step, which cannot finish until the
last retrieve lands. So every request in the step pays the **sum** of the retrieves. It is
not "k x 1 s for the k-th request", as SUMMARY.md says.

**aon, 8k, c=1** (`L8192_aon/lmcache_L8192_aon.log`): prefetch submitted 19:40:32.274, L2
load completed 32.445 (**172.6 ms for 1 GiB = 6.1 GiB/s**, over TCP, at lookup time), and
`Retrieved 8192 tokens in 0.001 seconds` (an L1 to GPU copy). TTFT 0.215 s.

**aon, 8k, c=4**: the 4 loads complete at 25.131 / 25.289 / 25.457 / 25.620 (162 ms
apart, still ~6.2 GiB/s aggregate). vLLM schedules each request only when its prefetch is
done, so the TTFTs are staggered (0.26 / 0.44 / 0.61 / 0.77). With lw, the deferred lookups
"complete" in ~2 ms, so all requests enter the same step.

**128k smoke2 (16 GiB, c=1)**: aon prefetch 2.45 s (6.5 GiB/s), TTFT 2.99 s. lw retrieve
14.96 s (**1.07 GiB/s**), TTFT 15.15 s. **Memory namespace** (stage3-newstack e2e04, same
model, rxe0, server 046e8558d): pipelined 8k retrieves 0.727-0.768 s and 16k 1.45-1.57 s,
**about 1.3 GiB/s**. Removing the disk gains ~25%; the remaining ~75% of the gap is the sink
path itself.

## 2. Code answers

**a. Per-layer priority exists, and layer 0 is first.** The planner emits slots in
ascending layer order (`lmcache/v1/layerwise/planner.py:962-985`).
`SinkFetchTable::begin` makes **one batch per layer** with `priority = ordinal`
(`csrc/storage_backends/aerospike/sink_fetch_table.cpp:75-77, 96-102`). The driver queues
the batches FIFO (`connector_sink_fetch.cpp:211-219`) to **16 batch workers**, each running
one synchronous `aerospike_batch_read` (`:21-24, 290-312`). Each row carries
`sink_priority` (`:363`). The server keeps a per-region heap ordered by `(priority, seq)`
(`as/src/base/kv_sink.c:24-31, 1088-1092`). So layers 0-15 are in flight at once and
layer 0 is placed first. The pump also waits in plan order (`lmcache/v1/layerwise/pump.py:186-196`).
Arrival times are not logged, so the 35 ms layer-0 figure is an estimate.

**b. What serializes concurrent retrieves.**
1. LMCache: `RETRIEVE` is `HandlerType.BLOCKING, requires_client_affinity=True`
   (`lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py:1051-1055`). The affinity
   key is the client's ZMQ identity (`lmcache/v1/multiprocess/mq.py:574`). "Tasks submitted
   with the same affinity_key always execute on the same worker thread … sequentially in
   FIFO order" (`lmcache/v1/multiprocess/affinity_pool.py:5-6, 92-105`). One vLLM worker
   gets one thread, whatever `--max-gpu-workers` is (default 1,
   `lmcache/v1/multiprocess/config.py:39`). Inside `retrieve`, `fetch_deferred_objects`
   (`:1313-1332`) runs `run_pipelined_retrieve`, which "Blocks until every layer is loaded"
   (`lmcache/v1/layerwise/pipelined_retrieve.py:342-345`). The thread is held for the whole
   ~1 s fetch. The same thread also serves STOREs (performance-onboarding.md §5.6).
2. vLLM: all requests of one step wait for all of their retrieves (section 1).
3. Not window_count: 8 windows against 4 retrieves, and no `refused` outcomes.
4. The server is not "one fetch at a time". It runs DRR across regions
   (`kv_sink.c:871-915`), but LMCache registers **one sink (region) for all windows**
   (`connector_sink_fetch.cpp:100-103`; aerospike_rdma.md "The window range is one sink").
   So parallel fetches would share one region's 4 MiB budget and one QP. One fetch already
   fills the queue (16 batches x 64 rows), so **releasing the thread alone would not raise
   aggregate throughput.** A step would still end at Σbytes / 1.03 GiB/s.

**c. Per-layer client overheads (all small):**
- Pump poll: `DEFAULT_POLL_INTERVAL_SECONDS = 0.0001` (`pump.py:33`), ≤ 0.1 ms per layer.
- Per-layer timeout: 1.5 s (`pump.py:42`), never hit.
- Per-layer H2D plus event: about 80 µs per layer (B8, performance-onboarding.md §4).
- One meta-record batch read per retrieve (`layerwise_source.py:247-266`).
- `wait_for_copies` once at the end (`object_group_transfer.py:1244-1252`).
- The 10 ms shared-key poll (`pipelined_retrieve.py:68`) applies only to busy keys (none here).
- No other sleeps.
- Total: < 5 ms per retrieve.

**d. There is no aon-over-RDMA path.** With layerwise off, deferred keys are loaded whole
through `storage_manager.load_into_l1` (TCP) (`lmcache_driven_transfer.py:1548-1571`). With
`--pipelined-fetch` off, lookups never defer (`lookup.py:630-640`), so the prefetch loads
whole over TCP. Sink reads are reachable only through the layerwise pump. The cheap
same-transport comparison is the reverse: **layerwise over TCP**, which is
`--use-layerwise` without `--pipelined-fetch` (section 7, E2).

**e. The aon path:**
- The lookup submits a prefetch task (`lookup.py:299-305`), which runs off the affinity
  thread and overlaps across requests.
- Each object (one 32 MiB chunk) is one task on one of **8 C++ workers**
  (`aerospike_l2_adapter.py:108,123`).
- `do_single_get` reads the meta record, then its **64 segment records one after another**
  with `aerospike_key_get` over TCP (`connector.cpp:246-342`). That is ≤ 8 concurrent
  512 KiB reads.
- The server serves them with 100 service threads and 20 batch-index threads
  (asd log, `service.c:214`, `batch.c:847`), which gives parallel O_DIRECT reads.

**f. Soft-RoCE / sink path parameters:**
- `rxe0` runs on `lo`. Active MTU **4096**, the RoCE maximum, so the MTU cannot be raised
  (asd log `kv_sink_verbs.c:660`). RC transport.
- **One QP per region** (`kv_sink_verbs.c:738-752`, `QP_DEPTH 64`) and one region per
  LMCache process, so all fetches go through 1 QP.
- Records are 512 KiB: plane = 256 tokens x 8 heads x 128 x bf16. Each record is one RDMA
  write of 128 x 4 KiB packets. The namespace has `max-record-size 1048576`.
- In flight: `REGION_IN_FLIGHT_BYTES 4 MiB` (**8 writes**) and `GLOBAL_IN_FLIGHT_BYTES 8 MiB`
  (`kv_sink.c:109-110`). There are 32 staging slots of 2 MiB (`as/include/base/kv_sink.h:68,71`).
- **Placement is serial on the completion poller.** Each completion calls `drain`
  (`kv_sink.c:799-858`), which calls `place`. `place` loads the record's bins, a **QD1
  `O_DIRECT` device read** on this config (`kv_sink.c:1019`), memcpys 512 KiB into staging
  (`kv_sink_verbs.c:340-342`), then posts (`:384`), all before polling again
  (`run_poller`, `:429-470`). This is open server issue 6
  (aerospike_server_issues.md §6).
- The rate is roughly 512 KiB / (read + copy + rxe per-QP work) ≈ 0.5 ms per record, or
  about 1 GiB/s. The disk read accounts for the 1.03 → 1.3 GiB/s difference against the
  memory namespace.

## 3. Theory points

| # | Control tower theory | Verdict | Evidence |
|---|---|---|---|
| 1 | Transports differ (TCP ~5.5, Soft-RoCE ~1 GiB/s); lw TTFT ≈ bytes / 1 GiB/s | **Confirmed, refined** | 8k 0.968 s, 16k ~1.95 s, 128k 14.96 s, all ≈ 1.03-1.07 GiB/s. aon 6.1-6.5 GiB/s. The cap is the sink path as a whole: 1 region, 1 QP, 4 MiB in flight, serial placement, QD1 disk read. rxe alone is not proven; the memory namespace still only reaches ~1.3 GiB/s. |
| 2 | Full hit: no compute to overlap; layer 0 early, layer 31 at the end | **Confirmed** | TTFT − retrieve ≈ 50 ms on both modes. Layer-0-first is in the code (2a). Per-layer arrival is not logged. |
| 3 | Concurrent lw retrieves serialized | **Confirmed. Cause: the LMCache affinity thread** held per pipelined fetch | c=4 timeline (back-to-back starts), `affinity_pool.py:5-6`, `pipelined_retrieve.py:342-345`. Not window_count, not a one-fetch server. vLLM same-step batching turns it into "everyone waits for the sum". Fixing it alone gains ~nothing, because bandwidth is shared. |

## 4. Bounds and estimates (Llama-3.1-8B, 1 GiB per 8k tokens)

Full hit: TTFT ≈ bytes/BW + F, with F ≈ 50 ms (lookup, scheduling, one token). Layerwise
can save at most the ~25 ms H2D copy. Rates for 100/200/400 Gb/s are line rate (11.6 /
23.3 / 46.6 GiB/s). They also require the server to keep up, which needs issue 6 fixed and
data in RAM or async reads.

| Full hit, c=1 | rxe 1.03 | TCP 6.1 (aon today) | 100 Gb/s | 200 Gb/s | 400 Gb/s |
|---|---|---|---|---|---|
| 8k bound | **1.02** (meas. 1.02) | 0.21 (meas. aon 0.215) | 0.14 | 0.09 | 0.07 |
| 16k bound | **1.99** (meas. 1.99) | 0.38 (meas. aon 0.391) | 0.22 | 0.14 | 0.09 |

**Partial hit, 8k cached + 8k uncached.** The compute is C ≈ 0.48 s (nocache 16k 0.80 −
8k 0.32), which is 15 ms per layer. aon = T + C + F. lw ≈ max(T, C) + min(T, C)/32 + F.

| | rxe 1.03 (T=0.97) | TCP 6.1 (T=0.16) | 100 Gb/s (T=0.086) | 400 Gb/s (T=0.02) |
|---|---|---|---|---|
| aon on that transport | 1.50 | **0.69** | 0.62 | 0.55 |
| lw on that transport | **1.04** | 0.54 (−22%) | 0.54 | 0.53 |

lw hides about min(T, C). On Soft-RoCE, lw saves 0.46 s against aon on the same transport,
but still loses 0.35 s to aon over TCP.

**When lw over rxe beats aon over TCP:** only if the compute of the step that carries the
fetch exceeds T_rxe − T_tcp ≈ 0.81 s per GiB cached. vLLM's chunked prefill (8192 tokens per
step) caps that step at about 0.3-0.5 s here. So the cached prefix must be ≲ 3-4k tokens,
or about 1/3 of the uncached suffix in the first step. Example: 2k cached + 8k new (T_rxe
0.24 s, which hides fully). At the TCP rate the break-even suffix is ~0.5x the prefix; at
100 Gb/s it is ~0.3x.

## 5. Other findings

- The 5 s connector wait fails at c ≥ 8 because the 8th serialized retrieve starts after
  ~7 s. This is the mechanism behind "engine stops at c ≥ 8"; `lw_wait600` is the only
  workaround without code.
- SUMMARY.md's line "TTFT about k x 1 s for the k-th queued retrieve" should read "every
  request in a vLLM step gets the sum of that step's retrieves" (c=2: 2.01-2.03 for both;
  c=4: 4.06 for all 4).

## 6. Options, ranked

| # | Option | Effect on comparability | Needs | Effort |
|---|---|---|---|---|
| 1 | **aon-only full-hit matrix** (Lyndon's proposal), with lw reported as "transport-bound on Soft-RoCE: 1.03 GiB/s" | Honest baseline | Nothing | none |
| 2 | **Layerwise-over-TCP control**: `--use-layerwise` without `--pipelined-fetch` (E2) | Isolates layerwise cost (PIECEWISE graphs, per-layer waits) from transport | Harness mode only | 1 h of GPU time |
| 3 | **Partial-hit workload with a short cached prefix** (2k + 8k, 8k + 8k) in aon and lw (E4) | Shows the overlap benefit where compute exists; 2k + 8k should tie or beat aon even on rxe | Harness: prompts sharing a stored prefix | half a day |
| 4 | **Measure first-layer latency** (E3) | Proves layer 0 lands in ~35 ms against aon's 170 ms whole load | Test-only DEBUG log in the pump on the box (not merged) | 1 h |
| 5 | **Memory namespace for both modes** | lw 1.03 → ~1.3 GiB/s; aon also changes | Config | 1 h |
| 6 | **Server: placement off the poller, async reads, larger in-flight budgets (4/8 MiB → 32/64 MiB), N QPs per region** | Raises the sink path toward rxe's multi-QP limit; gain depends on E1 | Aerospike server team (issue 6) | days |
| 7 | **Client: one sink per window (one QP each), pipelined fetch off the affinity thread** | Concurrent retrieves on separate QPs and regions; only pays with #6 and if E1 scales with QPs | LMCache product code | 2-4 days |
| 8 | **aon-over-RDMA flag** (retrieve waits for every layer, then loads) | Same-transport aon; on a full hit it would equal today's lw ±25 ms, so it adds little | Product code, small | 0.5 day |
| 9 | **Real RDMA NIC host** (EFA / ConnectX) | The intended target; 8k full hit ≈ 0.14 s at 100 Gb/s if the server keeps up | Hardware | - |

## 7. GPU-worker experiments to confirm

**E1. rxe raw ceiling, 1 vs 4 vs 8 QPs** (host, perftest is installed; no GPU). Server:

```
ib_write_bw -d rxe0 -x 1 -m 4096 -s 524288 -q 1 -t 8 -D 10 -F --report_gbits -p 18515
```

Client: the same command plus `127.0.0.1`. Repeat with `-q 4` and `-q 8` (new port each
time), and with `-t 128` for an unbounded queue depth. `-t 8` mimics the server's
8 x 512 KiB per-region budget. Expect ~8-11 Gb/s per QP if rxe is the cap. If `-q 1 -t 8`
is well above 8.8 Gb/s (1.03 GiB/s), the cap is the server's serial placement, not rxe.
Watch `mpstat -P ALL 1` for the kernel thread(s) doing rxe work.

**E2. Layerwise over TCP (8k, c=1/4)**: perf mode lw with `AON_L2` flags plus
`--use-layerwise`, and the vLLM connector `use_layerwise` on, without `--pipelined-fetch`.
Expect `pipelined_outcome=not_deferred` and TTFT ≈ aon + PIECEWISE cost.

**E3. First-layer latency, single lw retrieve (8k, c=1)**: a throwaway patch on the box
copy adding `logger.debug("layer %d resident t=%.6f", layer_id, time.monotonic())` after
`_await_layer` in `pump.py:192`. Gives layer-0 and layer-31 arrival and the spacing
(expect ~30 ms per layer). In parallel, sample the server:
`top -H -b -n 20 -d 0.5 -p $(pgrep -x asd)` to see whether the poller thread is at 100%.

**E4. Partial hit**: store 2k-token and 8k-token prefixes, then request prefix + 8k new
tokens in aon and lw at c=1, with vLLM prefix cache off. Predicted TTFT:
- 2k + 8k: aon ≈ 0.04 + C + F, lw ≈ C + F (tie or lw slightly ahead).
- 8k + 8k: aon ≈ 0.69 s, lw ≈ 1.04 s.

**E5. Memory namespace, 8k c=1, aon vs lw**: separates the disk QD1 read from the
rxe/placement cost.
