# New kv-sink stack: versions

| Item | Value |
| --- | --- |
| Host kernel | `6.8.0-138-generic` (Ubuntu 24.04), MLNX OFED 24.10 IB core, Soft-RoCE `rxe0` on `lo` (RC, GID index 1, active MTU 4096) |
| Container image | `lmcache-rocm:day1` (`sha256:50c62263906e…`) for both containers |
| Containers | `lmc-newstack` (LMCache build and tests; `/root/lmc-work:/work`, `/root/lmc-work/LMCache-1a:/work/LMCache`, `uverbs0`, no GPU devices); `aero-kvsink-bp` (server build and run; `/root/lmc-work:/root/lmc-work`, `uverbs0`) |
| ROCm / HIP | HIP `7.15.26333`, AMD clang 23.0.0git |
| torch | `2.12.0+rocm10.0.0` |
| vLLM | `0.27.1.dev5+gf46a9dfe2.d20260827` (not run) |
| Python / pytest | 3.14.7 / 9.1.1 |
| rdma-core (containers) | 50.0-2ubuntu0.2 |
| LMCache | `prototype-stage1` merge of `prototype-stage-1a` `934052cfa6e5aa30970d770f7bf842071106c17a` into `d10a376fd6ff165f27dea420c9a2f583cc231754` (commit in `SUMMARY.md`); tree on the box `/root/lmc-work/LMCache-1a` |
| LMCache build | In `lmc-newstack`: `BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1` plus `.deps/aerospike-client-c.env`; no `BUILD_WITH_AEROSPIKE_EFA` (the client links `libefa` itself). `lmcache_aerospike` md5 `b232eecf382232bb8e4eff8458fee4ae`, RUNPATH `/work/LMCache/.deps/aerospike-kvsink-install/lib` |
| Aerospike server | `citrusleaf/aerospike-server` `sriram/kv-sink-batch-prio` `046e8558d1732ea324308580438091f83c850fe7`; build string `Aerospike Community Edition build 8.1.3.0-111-g046e8558d`; binary `/root/lmc-work/aerospike-server-kvsink-bp/target/Linux-x86_64/bin/asd` (md5 `02703bbf080f0f0959f6f58b1339dca6`, links `libibverbs.so.1` and `libefa.so.1`); 97 s at `-j8`, `nice -n 19` |
| Server config | [`functional/configs/aerospike-kvsink-bp.conf`](../configs/aerospike-kvsink-bp.conf), env `KV_SINK_RDMA_DEVICE=rxe0 KV_SINK_GID_INDEX=1`; log line `kv-sink: RC, GID index 1, active mtu 4096, completion queue depth 16384` |
| Aerospike C client | `sriram588/aerospike-client-c-kvsink` (private) `sriram/kv-sink-batch-prio` `523d51eaa6d0a3814b040ba322dd15f2d021adf1`, built by `.deps/build_aerospike_client_kvsink.sh` (no event library, `-DAS_SINK_VERBS -DAS_SINK_EFA`); `libaerospike.so` md5 `85d9d69bbb6037e7147e156ad9513d30`, binds `ibv_reg_mr@IBVERBS_1.1`, `NEEDED` `libibverbs.so.1`, `libefa.so.1` |
| Client installs | `/root/lmc-work/LMCache-1a/.deps/aerospike-kvsink-install` (the build's own); a copy for other trees at `/root/lmc-work/deps/aerospike-kvsink-install-523d51ea` with `env.sh` (`/work/deps/...` inside the containers) |
| Old stack, untouched | `aero-kvsink` (`8.1.3.0-112-g512b0c207`, 3100-3103), `aerospike-ce` (3000), `lmc-c`, `/root/lmc-work/LMCache` |
