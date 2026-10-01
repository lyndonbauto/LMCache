#!/bin/bash
# Runs inside lmc-newstack. Builds the kv-sink C client 523d51ea with 1a's
# .deps script (installs into /work/LMCache/.deps/aerospike-kvsink-install and
# checks ibv_reg_mr@IBVERBS_1.1), then LMCache with the Day 1 HIP flags plus
# Aerospike + RDMA against that client. No GPU devices: GPU tests skip.
set -u
CLIENT=/work/aerospike-client-c-kvsink-bp
cd /work/LMCache || exit 1
if ! dpkg -s libibverbs-dev libyaml-dev libssl-dev zlib1g-dev >/dev/null 2>&1; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    libibverbs-dev ibverbs-providers rdma-core libyaml-dev libssl-dev zlib1g-dev make gcc g++ git
fi
echo "[$(date -u +%T)] client build"
git config --global --add safe.directory '*'
git -C $CLIENT rev-parse HEAD
nice -n 19 bash .deps/build_aerospike_client_kvsink.sh $CLIENT || { echo CLIENT_FAILED; exit 1; }
LIB=.deps/aerospike-kvsink-install/lib/libaerospike.so
objdump -T $LIB | grep -E 'ibv_reg_mr' ; ldd $LIB | grep -E 'verbs|efa'
source .deps/aerospike-client-c.env
echo "[$(date -u +%T)] lmcache build"
export BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 MAX_JOBS=8
export BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1
export SETUPTOOLS_SCM_PRETEND_VERSION=0.4.6.dev940
nice -n 19 pip install -e . --no-build-isolation --no-deps --ignore-requires-python -v > /work/functional/newstack/logs/pip_build.txt 2>&1
echo "lmcache build exit=$?"
ls lmcache/lmcache_aerospike*.so && ldd lmcache/lmcache_aerospike*.so | grep -E 'aerospike|verbs|efa'
readelf -d lmcache/lmcache_aerospike*.so | grep -E 'RUNPATH|RPATH'
cd /tmp && python -c "import lmcache.lmcache_aerospike as m; print('import ok', m.__file__)"
echo BUILD_DONE
