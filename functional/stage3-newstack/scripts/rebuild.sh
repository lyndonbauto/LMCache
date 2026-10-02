#!/bin/bash
# Rebuild LMCache in lmc-c against the kv-sink client 523d51ea.
cd /work/LMCache
source /work/deps/aerospike-kvsink-install-523d51ea/env.sh
export BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 MAX_JOBS=8
export BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1
env | grep -iE 'aerospike|kvsink' 
date -u
pip install -e . --no-build-isolation --no-deps --ignore-requires-python
echo "pip rc=$?"
date -u
for f in lmcache/lmcache_aerospike*.so lmcache/*rdma*.so; do echo "== $f"; ldd $f | grep -E "aerospike|verbs|efa"; done
objdump -T /work/deps/aerospike-kvsink-install-523d51ea/lib/libaerospike.so | grep "ibv_reg_mr"
python -c "import lmcache, lmcache.lmcache_aerospike as a; print(a.__file__)"
