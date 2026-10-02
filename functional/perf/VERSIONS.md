# Perf sanity check: versions and run definitions

Phase 1, 2026-10-02 (gpu-perf1). The stack is the one `gpu-e2e11` used; no rebuild and no
product change (the box tree was at `704a50a3` at the start; product code last changed in
`e6d35a0c`; this item added only `functional/` files).

| Component | Version |
|---|---|
| Host | DigitalOcean MI300X droplet, 1x MI300X (gfx942, 192 GB), 20 vCPU, 235 GiB RAM, Ubuntu 24.04.4 LTS, kernel `6.8.0-138-generic`, MLNX OFED 24.10 IB core |
| Boot disk (Aerospike data) | `/dev/vda` 720 GB virtio disk (`/dev/vda1` ext4 on `/`), scheduler `mq-deadline`; the droplet's network-attached virtual disk, not a local NVMe |
| ROCm / HIP / torch / vLLM | ROCm 10.0.0 (amdgpu 6.19.14), HIP `7.15.26333`, torch `2.12.0+rocm10.0.0`, vLLM `0.27.1.dev5+gf46a9dfe2.d20260827` (ROCm) |
| Container | `lmc-c`, image `lmcache-rocm:day1` (`sha256:50c62263906e…`) |
| LMCache | `prototype-stage1`, version `0.4.6.dev1038`, native extensions built with HIP, Aerospike and Aerospike RDMA against client `523d51ea` (by `gpu-newstack-s3`; not rebuilt) |
| Aerospike C client | `sriram/kv-sink-batch-prio` `523d51ea` (private, never pushed) |
| Aerospike server (L2) | kv-sink server `Aerospike Community Edition build 8.1.3.0-111-g046e8558d` in `aero-kvsink-bp`, 127.0.0.1:3700-3703, device namespace ([CHANGES.md](CHANGES.md)) |
| RDMA (lw only) | Soft-RoCE `rxe0` on `lo`, RC, GID index 1, active MTU 4096 (software RDMA, CPU bound) |
| Charts | matplotlib 3.10.8 on the local machine |

## Run definitions

| Item | Value |
|---|---|
| Model | `meta-llama/Llama-3.1-8B-Instruct`, bf16, TP=1 |
| vLLM flags (every mode) | `--max-model-len 131072 --gpu-memory-utilization 0.9 --no-enable-prefix-caching --async-scheduling --seed 0`; chunked prefill on with `max_num_batched_tokens=8192` (vLLM default); `VLLM_BATCH_INVARIANT` unset |
| KV cache capacity (vLLM log) | 1,266,704 tokens (nocache, aon); 1,275,664 tokens (lw); about 9.7 prompts of 128k fit at once |
| CUDA graphs | `FULL_AND_PIECEWISE` in nocache and aon; vLLM drops to `PIECEWISE` with layerwise on (lw) |
| Prompts | Token-ID prompts of exactly 8192 / 16384 / 32768 / 65536 / 130816 tokens (130816 = 511 chunks of 256, leaving room for output under 131072). Prompt `i` of length `L`: tokens drawn uniformly from `[1000, 128000)` by NumPy `default_rng(L * 1009 + i)`, first token replaced by `500 + 64 * length_index + i`, so no two prompts share even the first token. The same prompts in every mode (`perf_client.prompt_tokens`) |
| Output | `max_tokens 128`, `ignore_eos true`, `temperature 0`; every request generated 128 tokens (checked per point) |
| Load | Closed loop with exactly `c` requests in flight; `n = max(4, c)` requests per point (prompts `0..n-1`); `c = 1` sends 4 one after another |
| Metrics | Streaming `/v1/completions` (`stream=True`, `stream_options.include_usage`). TTFT = first streamed token minus send; total = last streamed token minus send; p50/p90 are NumPy linear-interpolation percentiles over the point's `n` requests; req/s and output tok/s over the point's wall time |
| Hit check | vLLM's `vllm:external_prefix_cache_hits_total` delta per point against `n x (L - 1)` (vLLM always computes the last prompt token); a cached point under 95% is INVALID |
| Warm-up / primes | One fixed 600-token prompt after every vLLM start (two chunks, so the connector's worker heartbeat starts); after every LMCache restart, short text primes until vLLM re-registers, then the same 600-token prompt again (an L2 hit, so the first measured retrieve does not pay the cold-start cost). All excluded from metrics. Its 2 chunks are why the store counts are 130 records over `32 x chunks x 65` |
| LMCache server (aon) | `lmcache server --port 6555 --http-host 127.0.0.1 --http-port 8080 --l1-size-gb 100 --eviction-policy LRU --chunk-size 256 --no-use-layerwise --no-l1-use-lazy --l2-adapter {"type":"aerospike","hosts":"127.0.0.1:3700","namespace":"lmcache","set_name":"kv_chunks"}` (no RDMA, no pipelined fetch: L2 hits are read by the plain Aerospike path into L1, then copied to the GPU) |
| LMCache server (lw) | the same with `--use-layerwise --pipelined-fetch --pipelined-max-chunks 64 --l1-size-gb 116` and `"rdma":{"transport":"RC","device_name":"rxe0","gid_index":1,"window_count":8,"window_bytes":2147483648}` |
| Connector | `LMCacheMPConnector`, `kv_load_failure_policy fail`, `lmcache.mp.use_layerwise` false (aon) / true (lw) |
| lw variants | `lw`: connector default `lmcache.mp.layerwise_wait_timeout_seconds` (5 s). `lw_wait600`: the same server and connector with `lmcache.mp.layerwise_wait_timeout_seconds 600` (a deviation, added because the default stops the engine once retrieves queue; see SUMMARY). In both, vLLM is restarted before any point where `/health` fails (`<session>/vllm_restarts_*.txt`) |
| Window sizing (lw) | Cap 64 chunks covers the longest phase-1 prompt (16k = 64 chunks); a window holds 64 x 32 MiB = 2 GiB; 8 windows = 16 GiB on top of 100 GB of general L1. More than 8 pipelined retrieves in flight would load whole (`refused`); with one vLLM, retrieves run one at a time on its LMCache worker thread, so 8 is never reached |
| L1 (general) | 100 GB, so the 32 GiB (8k) or 64 GiB (16k) store pass stays under the 0.8 eviction watermark (D-25) |
| Cached point procedure | Per length: store all 32 prompts once (aon session, 8 in flight), wait for L2 writes to settle, check the record count (65 records per chunk). Then, per mode and per point: restart the LMCache server (`STOP_GRACE 60`, D-18), so L1 is empty and every hit is read from Aerospike's disk; wait for re-registration; send the point. vLLM is restarted between aon and lw (with a fresh LMCache server, so the D-23 reap does not apply). `lw_wait600` runs in a later session per length: a fresh device file, an aon store-only pass (record count checked again), then the six lw points |
| Logging | `LMCACHE_LOG_LEVEL=DEBUG` in aon and lw (the per-retrieve `pipelined_outcome` line is DEBUG) |
