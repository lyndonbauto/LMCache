# Stage 6 CPU half: versions

Collected by `scripts/versions.sh` on 2026-10-01 (raw output: `logs/versions.txt`
on the box).

| Component | Version |
|---|---|
| Host | Ubuntu 24.04.4 LTS, kernel 6.8.0-138-generic, 20 vCPU, MI300X (not used) |
| Container for LMCache code | `lmc-c`, image `lmcache-rocm:day1` (`sha256:50c62263906e…`); CPU only (`HIP_VISIBLE_DEVICES=` `CUDA_VISIBLE_DEVICES=`) |
| torch / HIP / vLLM (installed, not exercised) | 2.12.0+rocm10.0.0 / 7.15.26333 / 0.27.1.dev5+gf46a9dfe2 |
| LMCache code under test | `/work/LMCache-cpu` at `e2e28d3a` (prototype-stage1; later commits up to `f4527b8f` touch only corpus, results and ledger), with the Day 1 native extensions copied in; the stage 6 test files on top |
| Aerospike C client (LMCache's extension and the fanout probe) | aerospike-client-c-libuv 7.3.0 (ubuntu24.04, x86_64), `/work/deps/aerospike-install` |
| E4 CE cluster (`aero-n1..3`) | Aerospike Community Edition 8.2.0.0, image `aerospike/aerospike-server:latest` = `sha256:583e882d540f…` (the image `aerospike-ce` runs) |
| kv-sink cluster (3 `asd` in `aero-kvsink`) | `feat/kv-sink-fetch-pipelined` @ `512b0c2079eb`, build `8.1.3.0-112-g512b0c207` (the build from `stage3/KVSINK-SERVER-BUILD.md`) |
| RDMA | Soft-RoCE `rxe0` on `lo`, RC, GID index 1 (`::ffff:127.0.0.1`); unchanged |

## Stage 6 GPU half, part A (gpu-stage6a, 2026-10-02, new kv-sink stack)

The same stack as [`stage5/VERSIONS.md`](../stage5/VERSIONS.md); no rebuild and no
product change since then (`git diff 0f0e0ff9 b5076622` outside `functional/` and
`docs/` is empty).

| Component | Version |
|---|---|
| Host | DigitalOcean MI300X droplet, 1x MI300X (gfx942, 192 GB), Ubuntu 24.04.4 LTS, kernel `6.8.0-138-generic`, MLNX OFED 24.10 IB core |
| ROCm / HIP / torch / vLLM | ROCm 10.0.0, HIP `7.15.26333`, torch `2.12.0+rocm10.0.0`, vLLM `0.27.1.dev5+gf46a9dfe2.d20260827` (ROCm, V2 model runner, async scheduling on) |
| Container | `lmc-c`, image `lmcache-rocm:day1` (`sha256:50c62263906e…`) |
| LMCache | `prototype-stage1`, box tree `b5076622` at the start, then the harness commits of this item (`functional/` only); product code `b6b0caae`, version `0.4.6.dev1038`; `lmc-c` built by `gpu-newstack-s3` against client `523d51ea` (not rebuilt) |
| Aerospike C client | `sriram/kv-sink-batch-prio` `523d51ea` (private, never pushed), `/root/lmc-work/deps/aerospike-kvsink-install-523d51ea` |
| kv-sink server (single node, pipelined) | `Aerospike Community Edition build 8.1.3.0-111-g046e8558d`, container `aero-kvsink-bp`, 127.0.0.1:3700-3703, `functional/configs/aerospike-kvsink-bp.conf` (namespace `lmcache` 16 GiB), restarted (emptied) before every kv-sink session |
| E4 CE cluster (plain path) | `aero-n1..3`, Aerospike CE `8.2.0.0` (`aerospike/aerospike-server:latest`), 127.0.0.1:3300/3310/3320, wiped and restarted at the session's RF before every cluster session |
| Model | `meta-llama/Llama-3.1-8B-Instruct` |
| Run settings | `VLLM_BATCH_INVARIANT=1`, temperature 0, `--no-enable-prefix-caching`, chunk 256, layerwise on, async scheduling on. Two hosts: `--gpu-memory-utilization 0.3` each (one host: 0.6), L1 20 GB per server. kv-sink sessions: `--no-l1-use-lazy --pipelined-fetch --pipelined-max-chunks 64`, two RDMA windows of 2 GiB (reserved in the L1 slab). `STOP_GRACE=60` |
