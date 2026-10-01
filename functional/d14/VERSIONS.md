# Versions: D-14 fix

| Component | Version |
| --- | --- |
| Host kernel | 6.8.0-138-generic (Ubuntu 24.04) |
| Container image | `lmcache-rocm:day1` (`lmc-d`, no GPU devices; `aero-kvsink` for the kv-sink server) |
| Python / torch | 3.14.7 / 2.12.0+rocm10.0.0 (built with `--ignore-requires-python`, as Day 1) |
| LMCache code under test | `prototype-stage1` at `e9cd0689` (rebased onto `30734468`; product code identical to the tested tree, md5-checked), tree `/root/lmc-work/LMCache-d14`, `SETUPTOOLS_SCM_PRETEND_VERSION=0.4.6.dev940` |
| LMCache native extensions | Built in `lmc-d` by `scripts/build.sh` (Day 1 HIP + Aerospike + RDMA flags), last build 2026-10-01 20:30 UTC, `lmcache_aerospike` md5 `2f5d991b8877…` |
| RDMA C++ test binaries | `make -C tests/v1/distributed/rdma pyharness` / `test` in the tree |
| Aerospike server (E2) | Community Edition 8.2.0.0, image `aerospike/aerospike-server:latest`, container `aerospike-ce-d14` |
| Aerospike server (E4) | Community Edition 8.2.0.0, `aero-n1..n3` (`functional/harness/cluster.sh`) |
| Aerospike server (E1) | kv-sink build `8.1.3.0-112-g512b0c207`, container `aero-kvsink` |
| Aerospike C client | 7.3.0, `/work/deps/aerospike-install` (stock; not Sriram's branch) |
| Aerospike Python client | 18.1.0 (inspector and benchmark only) |
| pytest | 9.1.1 |
| Soft-RoCE | `rxe0` on `lo`, GID 1 |
