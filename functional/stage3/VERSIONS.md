# Stage 3: versions (T-RDMA-06 CPU half and harness dry runs, 2026-10-01)

| Component | Version |
|---|---|
| Host | Ubuntu 24.04.4 LTS, kernel 6.8.0-138-generic, 20 vCPU, MI300X (not used by this item) |
| Container for LMCache code | `lmc-c`, image `lmcache-rocm:day1` (`sha256:50c62263906e…`); CPU only (`HIP_VISIBLE_DEVICES=` `CUDA_VISIBLE_DEVICES=`) |
| torch / HIP / vLLM (installed; vLLM not started) | 2.12.0+rocm10.0.0 / 7.15.26333 / 0.27.1.dev5+gf46a9dfe2 |
| LMCache code under test | `/work/LMCache-cpu` at `1f01fc84` (prototype-stage1) with the Day 1 native extensions, plus this item's test and harness changes, since committed as `eeb0f402` (test) and `614368d5`/`ec8ae2b1` (harness); no product code changed |
| Aerospike C client (LMCache's extension) | aerospike-client-c-libuv 7.3.0 (ubuntu24.04, x86_64), `/work/deps/aerospike-install` |
| kv-sink server (`asd` in `aero-kvsink`, 127.0.0.1:3100-3103) | `feat/kv-sink-fetch-pipelined` @ `512b0c2079eb`, build `8.1.3.0-112-g512b0c207` (fencing; see `KVSINK-SERVER-BUILD.md`) |
| RDMA | Soft-RoCE `rxe0` on `lo`, RC, GID index 1 (`::ffff:127.0.0.1`), MTU 4096; unchanged |
| Corpus (T-RDMA-06 keys) | `/work/functional/corpus/corpus_llama-3.1-8b-instruct.json`; model name = Llama-3.1-8B-Instruct snapshot `0e9e39f249a16976918f6564b8830bc894c89659` |
| Llama-3.3-70B-Instruct (for Stage 6) | downloaded to `HF_HOME=/work/hf`, snapshot `6f6073b423013f6a7d4d9f39144961bfbfbc386b`, 30/30 safetensors shards, 132 GB |
