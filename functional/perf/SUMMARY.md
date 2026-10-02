# Perf sanity check: LMCache + Aerospike (device namespace) on MI300X, phase 1

Llama-3.1-8B-Instruct, one MI300X at TP=1, vLLM 0.27.1 (ROCm), 128 output tokens, closed
loop with `c` requests in flight and `n = max(4, c)` requests per point. Versions and exact
flags: [VERSIONS.md](VERSIONS.md); box changes and the Aerospike namespace stanza:
[CHANGES.md](CHANGES.md); every point: [results.csv](results.csv); charts: [charts/](charts/).

Phase 1 covers `nocache` at 8k-128k and the cached modes at 8k and 16k, with the cached KV
in an Aerospike `storage-engine device` file (`O_DIRECT`, no read or post-write cache) on
the droplet's boot disk. Phase 2 (32k-128k cached) needs a second disk (`/dev/vdc1`),
which is awaiting approval.

## Result

- **All-or-nothing (aon) from Aerospike disk beats recompute at every point**: TTFT p50
  1.5x (8k) / 2.0x (16k) faster at c=1, rising to 4.5x / 6.1x at c=32. Total latency and
  throughput improve too (8k c=32: 5.4 vs 1.25 req/s).
- **Layer-by-layer (lw) over Soft-RoCE is slower than recompute** at every point: TTFT p50
  about 1 s per 8k request and 2 s per 16k request, and retrieves run one at a time, so
  TTFT grows linearly with `c` (8k c=32: 30.8 s vs 12.0 s nocache). The fetch moves about
  1 GiB/s through the software RDMA device; aon's plain read path moves about 5 GiB/s.
- **With the connector's default 5 s layerwise wait, lw stops vLLM once retrieves queue**
  (c >= 8 at 8k and 16k; 4 INVALID points). `lw_wait600` is the same lw setup with
  `lmcache.mp.layerwise_wait_timeout_seconds 600` (a labelled deviation) and is valid at
  every point.
- 60 of 64 points are valid; every valid cached point hit 100% of the expected tokens
  (`n x (L - 1)`), and every lw retrieve logged `pipelined_outcome=pipelined`.

## Tables (p50 seconds; speedup = nocache TTFT p50 / mode TTFT p50)

`lw` = default 5 s layerwise wait; `lw_wait600` = 600 s wait. INVALID points show the
latency of the requests that finished and are excluded from the speedups.

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

### 32k, 64k, 128k (nocache only in phase 1)

| c | 32k TTFT | 32k total | 64k TTFT | 64k total | 128k TTFT | 128k total |
|---|---|---|---|---|---|---|
| 1 | 2.247 | 3.008 | 7.114 | 8.031 | 25.276 | 26.457 |
| 2 | 4.744 | 7.095 | 14.334 | 21.372 | 45.689 | 75.560 |
| 4 | 8.772 | 17.121 | 26.034 | 52.834 | 84.433 | 181.338 |
| 8 | 18.583 | 41.645 | 57.742 | 131.343 | 188.105 | 443.798 |
| 16 | 42.698 | 97.285 | 135.655 | 310.198 | 448.747 | 766.224 |
| 32 | 97.813 | 199.070 | 321.378 | 523.529 | 797.570 | 1128.943 |

128k is 130816 tokens (511 chunks of 256, leaving room for the output under 131072).

## Observations

- **nocache throughput falls as `c` grows** (8k: 1.49 req/s at c=4, 1.25 at c=32; 128k
  stays around 0.02 req/s). vLLM's chunked prefill (`max_num_batched_tokens 8192`) admits
  long prefills alongside running decodes, so decode slows as more prompts are queued. At
  128k about 9.7 prompts fit in the KV cache (1,266,704 tokens), so c=16 and c=32 run in
  waves. The 128k c=32 point took 24.7 min; it was not capped because `n = c = 32` is
  already the minimum.
- **aon retrieves are serialized too**, but each is short: about 0.19 s per 8k request
  and 0.35 s per 16k request at c=32 (5.4 and 2.8 req/s, about 5.5 GiB/s of KV). `asd` read 70,082 MiB from storage (its
  `/proc/<pid>/io` `read_bytes`) during the 8k aon session and 139,785 MiB during 16k,
  matching one full read of every requested prompt per point.
- **lw retrieves serialize at about 1 GiB/s.** Each request's TTFT is about `k x 1 s`
  (8k) or `k x 2 s` (16k) for the k-th queued retrieve. Throughput stays flat from c=8
  upward at about 0.93 req/s at 8k (nocache: 1.25-1.38) and 0.46-0.48 req/s at 16k (nocache
  falls to the same level: 0.53 at c=8, 0.46 at c=32).
- **Default-wait failure mode (lw, c >= 8).** One retrieve exceeds the connector's 5 s
  wait (`LayerProgressRetrieveGenerationTimeoutError: timed out after 5.0s waiting for
  retrieve generation 9 (shared memory shows 8)`), vLLM raises `EngineDeadError`, and the
  other in-flight requests stream no token (`outcomes`: `failed:1, pipelined:N`). After
  the engine stops, LMCache logs `ValueError: operation forbidden on released memoryview
  object` from the layer-progress record (7 times at 8k, 21 times at 16k). Earlier points
  (c <= 4) stay valid even with TTFT up to 7.8 s, so the timeout does not trigger for
  every queued retrieve. At 8k, the session ended after c=8 (c=16 and c=32 not run).
  At 16k, the harness restarted vLLM before each later point, and each one failed the
  same way.
- No HTTP 500s, no `DEVICE_OVERLOAD`, no L1 refusals; every LMCache stop was clean (TERM,
  exit 143). Store record counts matched `32 x chunks x 65` plus 130 records for the
  600-token warm-up prompt's 2 chunks.

## Caveats

- **Soft-RoCE is software RDMA and CPU bound.** `rxe0` runs on `lo`, so the lw numbers
  measure the kernel's RDMA emulation, not a NIC. On hardware RDMA, the lw fetch rate and
  the ranking against aon may differ completely; treat lw here as a functional check with
  timings.
- **Layerwise forces PIECEWISE CUDA graphs** (section 5.3): vLLM drops from
  `FULL_AND_PIECEWISE` to `PIECEWISE` when `use_layerwise` is on, which slows decode in lw
  independently of the fetch.
- **One GPU, TP=1, one vLLM instance.** LMCache serves one worker, so retrieves for that
  worker run one at a time. Multi-GPU or multi-instance setups were not tested.
- **Aerospike sits on a virtual disk of this droplet** (`/dev/vda`, virtio, network
  attached). Reads were `O_DIRECT` with no Aerospike read cache, and `asd` counted them as
  storage reads, but about 5 GiB/s for aon is above what a typical virtual disk delivers.
  The hypervisor may be caching the file, so the aon numbers may be optimistic compared
  with a cold physical disk.
- **`lw_wait600` deviates from the default connector config.** It exists only to measure
  lw latency under concurrency; the default config is not usable at c >= 8 on this box.
- `LMCACHE_LOG_LEVEL=DEBUG` was on in aon and lw (needed for the `pipelined_outcome`
  line); this adds logging overhead to both cached modes.
- `n` is 4 at c <= 4, so the p50 and p90 there come from 4 requests.
- An earlier 8k aon run (lazy L1 allocation: the first retrieve after each restart took
  9.4 s) was superseded by the run above; its files are under `superseded/` on the box.

## Files

On the box under `/root/lmc-work/functional/perf/`: `nocache/`, `L<len>_aon/`, `L<len>_lw/`,
`L<len>_store/` + `L<len>_lwwait/` (per point JSON, outcome counts, LMCache and vLLM logs,
listener checks, `asd_read_bytes.txt`) and `progress.log`. Here: `results.csv`,
`summary_tables.md` (generated by `aggregate.py`), `charts/` (generated by `charts.py`).
