#!/bin/bash
# Runs inside lmc-c. Builds the kv-sink C client 5a24afdbb6 (as_sink_config.
# queue_pairs) with the .deps script, then LMCache prototype-stage-1b with HIP
# plus Aerospike + RDMA against that client, installed editable into the
# image's Python (which has vLLM 0.27.1 and torch 2.11 for ROCm 7.2.3).
set -u
CLIENT=/work/aerospike-client-c-kvsink-bp
cd /work/LMCache || exit 1
mkdir -p /work/functional/perf2/logs
if ! dpkg -s libibverbs-dev libyaml-dev libssl-dev zlib1g-dev perftest >/dev/null 2>&1; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    libibverbs-dev ibverbs-providers ibverbs-utils rdma-core libyaml-dev libssl-dev zlib1g-dev \
    make gcc g++ git perftest numactl procps
fi
echo "[$(date -u +%T)] client build"
git config --global --add safe.directory '*'
nice -n 19 bash .deps/build_aerospike_client_kvsink.sh $CLIENT || { echo CLIENT_FAILED; exit 1; }
LIB=.deps/aerospike-kvsink-install/lib/libaerospike.so
grep -n queue_pairs .deps/aerospike-kvsink-install/include/aerospike/as_sink.h
ldd $LIB | grep -E 'verbs|efa'
source .deps/aerospike-client-c.env
echo "[$(date -u +%T)] lmcache build"
export BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 MAX_JOBS=16
export BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1
export SETUPTOOLS_SCM_PRETEND_VERSION=0.4.6.dev1039
nice -n 19 pip install -e . --no-build-isolation --no-deps --ignore-requires-python -v \
  > /work/functional/perf2/logs/pip_build.txt 2>&1
echo "lmcache build exit=$?"
ls lmcache/lmcache_aerospike*.so && ldd lmcache/lmcache_aerospike*.so | grep -E 'aerospike|verbs|efa'
cd /tmp && python -c "import lmcache.lmcache_aerospike as m; r = m.L1RdmaRegistration(); print('import ok, queue_pairs', r.queue_pairs)"
echo BUILD_DONE
