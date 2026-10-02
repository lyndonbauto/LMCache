# Stage 4 (concurrency, new kv-sink stack): versions

The same stack as [`stage3-newstack/VERSIONS.md`](../stage3-newstack/VERSIONS.md).
No rebuild, and no product change since then: `git diff b6b0caae 0f0e0ff9`
touches only `functional/` and `docs/`.

| Item | Value |
| --- | --- |
| Host | DigitalOcean MI300X droplet, 1x MI300X (gfx942, 192 GB), Ubuntu 24.04.4 LTS, kernel `6.8.0-138-generic`, MLNX OFED 24.10 IB core |
| RDMA | Soft-RoCE `rxe0` on `lo` (out-of-tree upstream `rdma_rxe`), RC, GID index 1 = 127.0.0.1, active MTU 4096 |
| ROCm / HIP | ROCm 10.0.0 (amd-smi 27.0.0, amdgpu 6.19.14), HIP `7.15.26333` |
| torch | `2.12.0+rocm10.0.0` |
| vLLM | `0.27.1.dev5+gf46a9dfe2.d20260827` (ROCm) |
| Container image | `lmcache-rocm:day1` (`sha256:50c62263906e617d0082c537a138e1a2b403386e945d806d47398f57a8a59a6c`) for `lmc-c` and `aero-kvsink-bp` |
| LMCache | `prototype-stage1`; box tree `0f0e0ff9` at the start, then the harness commits `9decc574` and `f79e00bf` (`functional/` only). Product code = `b6b0caae` (1a `934052cf`, merge `e4701b9a`, D-14 fix included). Version `0.4.6.dev1038`. `lmc-c` was built by `gpu-newstack-s3` against client `523d51ea` (not rebuilt here) |
| Aerospike C client | `sriram/kv-sink-batch-prio` `523d51ea` (private repo, never pushed), install `/root/lmc-work/deps/aerospike-kvsink-install-523d51ea` |
| kv-sink server | `Aerospike Community Edition build 8.1.3.0-111-g046e8558d`, container `aero-kvsink-bp`, 127.0.0.1:3700-3703, config `functional/configs/aerospike-kvsink-bp.conf` (namespace `lmcache` 16 GiB since `b2ea0f98`), restarted (emptied) before every group |
| Aerospike CE | `8.2.0.0`, container `aerospike-ce`, 127.0.0.1:3000 (up, not used by Stage 4) |
| Model | `meta-llama/Llama-3.1-8B-Instruct` (HF_HOME `/work/hf`); gpt-oss excluded (D-16) |
| Run settings | `VLLM_BATCH_INVARIANT=1`, temperature 0, `--use-layerwise`, vLLM async scheduling on (default), `--no-enable-prefix-caching`, chunk 256. One vLLM at `--gpu-memory-utilization 0.6`; two vLLMs at 0.3 each (they fit, 0.25 not needed). `STOP_GRACE=60`. Policy `fail` everywhere |
