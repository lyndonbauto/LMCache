# Stage 3 on the new kv-sink stack: versions

| Item | Value |
| --- | --- |
| Host | DigitalOcean MI300X droplet, 1x MI300X (gfx942, 192 GB), Ubuntu 24.04.4 LTS, kernel `6.8.0-138-generic`, MLNX OFED 24.10 IB core |
| RDMA | Soft-RoCE `rxe0` on `lo` (out-of-tree upstream `rdma_rxe`), RC, GID index 1 = 127.0.0.1, active MTU 4096 |
| ROCm / HIP | ROCm 10.0.0 (amd-smi 27.0.0, amdgpu 6.19.14), HIP `7.15.26333` |
| torch | `2.12.0+rocm10.0.0` |
| vLLM | `0.27.1.dev5+gf46a9dfe2.d20260827` (ROCm) |
| Container image | `lmcache-rocm:day1` (`sha256:50c62263906e617d0082c537a138e1a2b403386e945d806d47398f57a8a59a6c`) for `lmc-c` (GPU, `/dev/infiniband`) and `aero-kvsink-bp` (server) |
| LMCache | `prototype-stage1`. Product code `b6b0caae` = `prototype-stage-1a` `934052cf` merged into `prototype-stage1` (merge `e4701b9a`) plus the upstream-split commit `e6d35a0c` (D-17 recompute guard) and docs/harness/ledger commits; the harness commits of this run (`adf9aa5f`, `6cb99246`, `1e364379`, `a4716acb`, `b2ea0f98`) change `functional/` only. Version `0.4.6.dev1038`, banner `gb6b0caae` |
| LMCache build (lmc-c) | `source /work/deps/aerospike-kvsink-install-523d51ea/env.sh; BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 MAX_JOBS=8 BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1 pip install -e . --no-build-isolation --no-deps --ignore-requires-python` (2026-10-01 23:47Z, [`scripts/rebuild.sh`](scripts/rebuild.sh)). `lmcache_aerospike` md5 `382619f47ffd3d9544a95dd32b77eae4`, RUNPATH `/work/deps/aerospike-kvsink-install-523d51ea/lib`; `ldd`: `libaerospike.so` from that install, `libibverbs.so.1`, `libefa.so.1` |
| Aerospike C client | `sriram/kv-sink-batch-prio` `523d51eaa6d0a3814b040ba322dd15f2d021adf1` (private repo; never pushed), install `/root/lmc-work/deps/aerospike-kvsink-install-523d51ea` built by `cpu-newstack`; `libaerospike.so` imports `ibv_reg_mr@IBVERBS_1.1` |
| kv-sink server | `sriram/kv-sink-batch-prio` `046e8558d1732ea324308580438091f83c850fe7`, build string `Aerospike Community Edition build 8.1.3.0-111-g046e8558d`, binary `/root/lmc-work/aerospike-server-kvsink-bp/target/Linux-x86_64/bin/asd`, in container `aero-kvsink-bp`, 127.0.0.1:3700-3703, config [`functional/configs/aerospike-kvsink-bp.conf`](../configs/aerospike-kvsink-bp.conf) (memory namespace `lmcache` 8 GiB until `b2ea0f98`, 16 GiB from the e2e04 long2 group on; `default-ttl 0`), env `KV_SINK_RDMA_DEVICE=rxe0 KV_SINK_GID_INDEX=1`; restarted (emptied) before every group |
| Plain-path L2 | Aerospike CE `8.2.0.0` (`aerospike/aerospike-server:latest`, `sha256:583e882d540f…`), container `aerospike-ce`, 127.0.0.1:3000, namespace `lmcache` |
| Old kv-sink (not used) | `aero-kvsink`, `8.1.3.0-112-g512b0c207`, 3100-3103: stopped throughout |
| Models | `meta-llama/Llama-3.1-8B-Instruct`, `openai/gpt-oss-120b` (HF_HOME `/work/hf`) |
