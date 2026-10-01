# Versions: work item 2

| Component | Version |
| --- | --- |
| Host kernel | 6.8.0-138-generic (Ubuntu 24.04) |
| ROCm / HIP | 7.15.26333 (host `/opt/rocm`); HIP 7.15 in the container. No GPU used |
| torch | 2.12.0+rocm10.0.0 |
| vLLM | 0.27.1.dev5+gf46a9dfe2 (installed, not run) |
| Container image | `lmcache-rocm:day1` (`lmc-c` for tests, `aero-kvsink` for the kv-sink server) |
| LMCache code under test | `prototype-stage1` at `b6cf78e8` (product code) plus the test commits `87b84b6b`, `c6980baf`, `b94cad49`, `e2e28d3a`; clone `/root/lmc-work/LMCache-cpu` |
| LMCache native extensions | Built 2026-09-30 20:34 UTC (the Day 1 build, same `.so` as `/root/lmc-work/LMCache`, md5 `157e9c01…` for `lmcache_aerospike`). No C++ under `csrc/` changed after `a84d9b10` (2026-09-29) |
| RDMA C++ test binaries | Built by `make -C tests/v1/distributed/rdma` in the clone during this run |
| Aerospike server (E2) | Community Edition 8.2.0.0, sha `b4d13ef`, image `aerospike/aerospike-server:latest` (`sha256:583e882d…`), container `aerospike-ce-t2` |
| Aerospike server (E1) | kv-sink build `8.1.3.0-112-g512b0c207` (`feat/kv-sink-fetch-pipelined` @ `512b0c2079eb`), container `aero-kvsink` |
| Aerospike C client | 7.3.0, as built into the LMCache extension (Day 1) |
| Aerospike Python client | 18.1.0 (test inspector only) |
| rdma-core / libibverbs | 50.0-2ubuntu0.2 (container) |
| Soft-RoCE | `rxe0` on `lo`, out-of-tree upstream `rdma_rxe` (Day 1), GID 1 = `::ffff:127.0.0.1` |
