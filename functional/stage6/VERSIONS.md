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
