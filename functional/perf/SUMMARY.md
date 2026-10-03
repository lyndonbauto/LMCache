# Perf sanity check: LMCache + Aerospike (device namespace) on MI300X

Llama-3.1-8B-Instruct, one MI300X at TP=1, vLLM 0.27.1 (ROCm), 128 output tokens, closed
loop with `c` requests in flight and `n = max(4, c)` requests per point. Versions and exact
flags: [VERSIONS.md](VERSIONS.md); box changes and the Aerospike namespace stanza:
[CHANGES.md](CHANGES.md); every point: [results.csv](results.csv); charts: [charts/](charts/).

The cached KV lives in an Aerospike `storage-engine device` file (`O_DIRECT`, no read or
post-write cache), with L1 emptied before every point, so every hit is read from the drive
through Aerospike.

| Phase | Lengths | Modes | Data file on |
|---|---|---|---|
| 1 | nocache 8k-128k; cached 8k, 16k | nocache, aon, lw (default 5 s wait), lw_wait600 | boot disk (`/dev/vda`) |
| 2 | cached 32k, 64k, 128k | aon only (see below) | scratch disk (`/dev/vdc1` at `/mnt/scratch`) |

**Layer-by-layer was not run in phase 2.** Lyndon ordered at 21:05Z (Slack, relayed by the
control tower) that no more lw points be run. The phase 2 lw harness (per-length windows,
`lw_wait1800`, stopping the default-wait series at the first engine stop) is in place but
produced no points; only one pre-sweep 128k smoke request ran (below).

## Result

- **All-or-nothing (aon) from the Aerospike drive beats recompute at every point of every
  length**, and the gain grows with prompt length and concurrency. TTFT p50 speedup at c=1:
  1.5x (8k), 2.0x (16k), 3.0x (32k), 4.7x (64k), 9.3x (128k); at c=32: 4.5x, 6.1x, 9.7x,
  17.4x, 21.2x. At 128k c=32, TTFT p50 is 37.7 s against 798 s, and total latency 40 s
  against 1129 s.
- **aon moves about 5-7 GiB/s of KV**: TTFT at c=1 is about 1 s per 5.3-5.9 GiB, and at
  c=32 it delivers 5.4 (8k) to 7.0 (64k) GiB/s. The drives read far faster (fio O_DIRECT:
  scratch 31-39 GiB/s, boot 9-11 GiB/s), so the aon limit is LMCache's plain read path,
  not the disk.
- **Layer-by-layer (lw) over Soft-RoCE is slower than recompute** at 8k and 16k (phase 1):
  about 1 GiB/s, one retrieve at a time. The requests in one vLLM step all wait for the
  sum of that step's retrieves, so TTFT grows linearly with `c` (8k c=32: 30.8 s against
  12.0 s for nocache). See [LW-INVESTIGATION.md](LW-INVESTIGATION.md) and
  [LW-EXPERIMENTS.md](LW-EXPERIMENTS.md).
- **With the default 5 s layerwise wait, lw stops vLLM once retrieves queue** (c >= 8 at 8k
  and 16k). `lw_wait600` (the wait raised to 600 s, a labelled deviation) is valid at every
  point.
- 78 of 84 rows are valid. All 18 phase 2 points are valid, with 100% of the expected hit
  tokens (`n x (L - 1)`). The 6 invalid rows are all default-wait lw: 4 engine stops and
  2 not run after the engine stopped.

## Tables (p50 seconds; speedup = nocache TTFT p50 / mode TTFT p50)

`lw` = default 5 s layerwise wait; `lw_wait600` = 600 s wait. INVALID points show the
latency of the requests that finished; they and "not run" rows are excluded from speedups.

### 8k (8192 tokens)

| c | nocache TTFT | aon TTFT | lw TTFT | lw_wait600 TTFT | nocache total | aon total | lw total | lw_wait600 total | aon speedup | lw speedup | lw_wait600 speedup |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.323 | 0.215 | 1.022 | 1.115 | 0.978 | 0.942 | 2.024 | 2.172 | 1.50x | 0.32x | 0.29x |
| 2 | 0.511 | 0.256 | 2.021 | 2.101 | 1.391 | 0.997 | 3.068 | 3.168 | 1.99x | 0.25x | 0.24x |
| 4 | 1.425 | 0.528 | 4.056 | 2.990 | 2.680 | 1.386 | 5.103 | 5.000 | 2.70x | 0.35x | 0.48x |
| 8 | 2.597 | 0.845 | INVALID | 7.742 | 5.766 | 1.886 | INVALID | 8.817 | 3.07x | - | 0.34x |
| 16 | 5.517 | 1.621 | not run | 15.445 | 12.495 | 2.703 | not run | 16.826 | 3.40x | - | 0.36x |
| 32 | 11.997 | 2.644 | not run | 30.817 | 25.377 | 3.762 | not run | 34.263 | 4.54x | - | 0.39x |

### 16k (16384 tokens)

| c | nocache TTFT | aon TTFT | lw TTFT | lw_wait600 TTFT | nocache total | aon total | lw total | lw_wait600 total | aon speedup | lw speedup | lw_wait600 speedup |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.801 | 0.391 | 1.992 | 2.029 | 1.495 | 1.242 | 3.055 | 3.035 | 2.05x | 0.40x | 0.39x |
| 2 | 1.238 | 0.440 | 3.922 | 2.965 | 2.685 | 1.314 | 4.922 | 4.990 | 2.82x | 0.32x | 0.42x |
| 4 | 3.460 | 0.984 | 7.798 | 7.965 | 6.450 | 2.002 | 8.899 | 9.043 | 3.52x | 0.44x | 0.43x |
| 8 | 6.791 | 1.645 | INVALID | 15.814 | 14.962 | 2.766 | INVALID | 17.216 | 4.13x | - | 0.43x |
| 16 | 15.017 | 2.892 | INVALID | 31.544 | 34.016 | 4.020 | INVALID | 33.582 | 5.19x | - | 0.48x |
| 32 | 33.713 | 5.532 | INVALID | 63.285 | 69.580 | 6.678 | INVALID | 69.356 | 6.09x | - | 0.53x |

### 32k (32768 tokens)

| c | nocache TTFT | aon TTFT | nocache total | aon total | aon speedup |
|---|---|---|---|---|---|
| 1 | 2.247 | 0.748 | 3.008 | 1.857 | 3.00x |
| 2 | 4.744 | 0.761 | 7.095 | 1.893 | 6.23x |
| 4 | 8.772 | 1.766 | 17.121 | 2.989 | 4.97x |
| 8 | 18.583 | 2.860 | 41.645 | 4.129 | 6.50x |
| 16 | 42.698 | 5.732 | 97.285 | 6.966 | 7.45x |
| 32 | 97.813 | 10.100 | 199.070 | 11.446 | 9.68x |

### 64k (65536 tokens)

| c | nocache TTFT | aon TTFT | nocache total | aon total | aon speedup |
|---|---|---|---|---|---|
| 1 | 7.114 | 1.506 | 8.031 | 3.103 | 4.72x |
| 2 | 14.334 | 1.423 | 21.372 | 3.088 | 10.07x |
| 4 | 26.034 | 3.318 | 52.834 | 5.058 | 7.85x |
| 8 | 57.742 | 5.354 | 131.343 | 7.103 | 10.79x |
| 16 | 135.655 | 11.176 | 310.198 | 12.937 | 12.14x |
| 32 | 321.378 | 18.484 | 523.529 | 20.212 | 17.39x |

### 128k (130816 tokens)

| c | nocache TTFT | aon TTFT | nocache total | aon total | aon speedup |
|---|---|---|---|---|---|
| 1 | 25.276 | 2.711 | 26.457 | 5.274 | 9.32x |
| 2 | 45.689 | 3.110 | 75.560 | 5.754 | 14.69x |
| 4 | 84.433 | 6.229 | 181.338 | 8.920 | 13.56x |
| 8 | 188.105 | 10.620 | 443.798 | 13.312 | 17.71x |
| 16 | 448.747 | 19.101 | 766.224 | 21.761 | 23.49x |
| 32 | 797.570 | 37.679 | 1128.943 | 40.387 | 21.17x |

128k is 130816 tokens (511 chunks of 256, leaving room for the output under 131072).

## Drive throughput next to aon's effective rate

| Measurement | Scratch (`/dev/vdc1`) | Boot (`/dev/vda1`) |
|---|---|---|
| fio sequential write, 1 MiB, QD32, O_DIRECT | 19.9 GiB/s | 4.4 GiB/s |
| fio sequential read, 1 MiB, QD32, O_DIRECT | 39.4 GiB/s | 11.4 GiB/s |
| fio random read, 512 KiB (Aerospike's record size), 4 jobs x QD32, O_DIRECT | 31.1 GiB/s | 8.8 GiB/s |
| aon at c=32 (KV delivered / wall time) | 6.3 (32k), 7.0 (64k), 6.9 (128k) GiB/s | 5.4 (8k), 5.7 (16k) GiB/s |
| aon at c=1 (KV per request / TTFT p50) | 5.3, 5.3, 5.9 GiB/s | 4.7, 5.1 GiB/s |

fio ran in `aero-kvsink-bp` (the container `asd` reads through) on a 32 GiB file for 30 s
per test. `asd`'s storage reads (`/proc/<pid>/io` `read_bytes`) match one full read of every
requested prompt per point: 1,113,892 MiB in the 128k session, against 1,111,936 MiB for
68 requests x 511 chunks x 32 MiB (the rest is the warm-up retrieves after each restart). So aon reads did go to the block device, bypassing
the guest page cache. Both disks are virtual, though, and 39 GiB/s O_DIRECT means the
hypervisor serves at least part of each disk from cache or very fast backing. This test
cannot show what a cold physical drive would deliver; it does show aon uses well under the
drive's rate.

## Findings

1. **Engine stop under queued lw retrieves (default 5 s wait).** lw retrieves run one at a
   time on LMCache's worker (about 1 s per 8k prompt, 2 s per 16k). Once one waits more
   than 5 s for its turn, the connector raises `LayerProgressRetrieveGenerationTimeoutError:
   timed out after 5.0s waiting for retrieve generation 9 (shared memory shows 8)`, vLLM
   raises `EngineDeadError`, and every other in-flight request streams no token. This
   happened at c=8 at 8k and at c=8, 16 and 32 at 16k (vLLM restarted before each point).
   c <= 4 stayed valid even with TTFT up to 7.8 s, so not every queued retrieve trips it.
2. **`ValueError: operation forbidden on released memoryview object` in LMCache** after that
   engine stop: the layer-progress record read (`_RECORD_STRUCT.unpack_from(self._buffer,
   0)`) runs after vLLM's KV cache is unregistered. Seen 7 times at 8k and 21 times at 16k.
3. **Aerospike stop-writes silently trims a store.** With the data file at 1.4x the KV (as
   in phase 1), the 32k store reached `data_used_pct 70`, Aerospike's default
   `stop-writes-used-pct`, and refused further writes. LMCache logged only WARNINGs (`Store
   task 66 to adapter 0 (aerospike) failed for 31 key(s): put-payload: Aerospike status 8:
   AEROSPIKE_ERR_SERVER_FULL`; 68 keys over 4 tasks), and 34 of 4,096 chunks were missing.
   That run is in `superseded/` on the box; phase 2 used 2.0x (about 50% used) and every
   store landed exactly. Size kv-sink namespaces so the working set stays under
   `stop-writes-used-pct`, and watch these WARNINGs.
4. D-25 and D-26 did not occur. Phase 2 stored in batches of at most 64 GiB with an LMCache
   restart between them, which kept every burst under L1's headroom. Every store check
   showed 0 `DEVICE_OVERLOAD`, 0 `Failed to batched allocate`, 0 L1 refusals and exact record
   counts: 266,370 / 532,610 / 1,063,010 records, each `32 x chunks x 65` plus 130 for the
   600-token warm-up prompt.

## Observations

- **nocache throughput falls as `c` grows** (8k: 1.49 req/s at c=4, 1.25 at c=32). vLLM's
  chunked prefill (`max_num_batched_tokens 8192`) runs long prefills alongside decodes. At
  128k only about 9.7 prompts fit in the KV cache (1,266,704 tokens), so c=16 and c=32 run
  in waves. The 128k c=32 nocache point took 24.7 min; it could not be capped because
  `n = c = 32`.
- **aon retrieves are serialized too**, but at 5-7 GiB/s. aon throughput at c=32 is 4.3x
  (8k) to 20x (128k) nocache's.
- **One 128k lw data point exists**: the pre-sweep smoke (`smoke2_lw`, n=1, c=1) had TTFT
  15.1 s, `pipelined`, with 2 x 15.97 GiB windows. That is about 1.06 GiB/s, the same
  Soft-RoCE rate as at 8k and 16k. It is a single smoke request, not a measured point, and
  is not in the tables.
- Host memory with the 180 GB aon L1: lowest available was 41.7 GB (`hostmem.txt` per
  session).
- No HTTP 500s; every LMCache stop was clean (TERM, exit 143); every listener was on
  127.0.0.1.

## Caveats

- **Soft-RoCE is software RDMA and CPU bound.** `rxe0` runs on `lo`, so the lw numbers
  measure the kernel's RDMA emulation, not a NIC. On hardware RDMA, the lw rate and its
  ranking against aon may differ completely.
- **Layerwise forces PIECEWISE CUDA graphs** (section 5.3): vLLM drops from
  `FULL_AND_PIECEWISE` to `PIECEWISE` with `use_layerwise` on, which slows lw decode
  independently of the fetch.
- **One GPU, TP=1, one vLLM instance.** LMCache serves one worker, whose retrieves run one
  at a time; multi-GPU and multi-instance setups were not tested.
- **Aerospike sits on virtual disks of this droplet** (boot `/dev/vda` in phase 1,
  DigitalOcean scratch `/dev/vdc` in phase 2). Reads bypassed the guest page cache, but the
  hypervisor likely caches them (see the fio table), so a deployment on physical NVMe may
  see different aon numbers once the drive, not LMCache, is the limit.
- **lw coverage is 8k and 16k only**, by Lyndon's order. `lw_wait600` deviates from the
  default connector config; it exists only to measure lw latency under concurrency.
- `LMCACHE_LOG_LEVEL=DEBUG` was on in the cached modes (needed for `pipelined_outcome`).
- `n` is 4 at c <= 4, so the p50 and p90 there come from 4 requests.
- Superseded runs, kept on the box under `superseded/`: 8k aon with lazy L1 (cold-start
  9.4 s first retrieve), 32k aon at 1.4x file size (stop-writes), and a partial 64k store
  stopped for the same reason.

## Files

On the box under `/root/lmc-work/functional/perf/`: `nocache/`, `L<len>_aon/`, `L<len>_lw/`,
`L<len>_store/` + `L<len>_lwwait/` (phase 1), `smoke2_*`, `fio/` (per point JSON, outcome
counts, LMCache and vLLM logs, listener checks, `asd_read_bytes.txt`, `hostmem.txt`,
`store_check.txt`) and `progress.log`. Here: `results.csv` and `summary_tables.md` (from
`aggregate.py`), `charts/` (from `charts.py`): TTFT and total vs concurrency per length,
TTFT vs length at c=1 and c=32, and `speedup_c1.png`.
