#!/bin/bash
# Runs inside lmc-newstack (CPU only, rxe0 via uverbs0). Usage: run_tests.sh <suite> [name]
#   units  the D-14 unit set (layerwise, multiprocess, distributed, adapters), no servers
#   it     the layerwise, RDMA and Aerospike suites with the integration tests on,
#          against the kv-sink batch-read server on 127.0.0.1:3700 (upstream: 620 passed,
#          12 skipped on Soft-RoCE RC)
#   wide   tests/v1/layerwise and all of tests/v1/distributed with the integration tests on
#   pipe   the pipelined RDMA integration test only
#   slow   the region-release test (SERVER_MAX_REGIONS + 1 client lifetimes)
#   d14    the D-14 storage ITs (record layouts, storage integrity, pipelined)
set -u
SUITE=$1; NAME=${2:-$1}
R=/work/functional/newstack/logs/$NAME
mkdir -p $R
cd /work/LMCache || exit 1
source .deps/aerospike-client-c.env
TO=""; python -c "import pytest_timeout" 2>/dev/null && TO="--timeout=900"
export HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= PYTHONDONTWRITEBYTECODE=1 RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
IT="RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3700 AEROSPIKE_TEST_NAMESPACE=lmcache AEROSPIKE_TEST_EVICT_NAMESPACE=lmcache_evict"
case $SUITE in
  units)
    ENVS=""
    ARGS="tests/v1/layerwise tests/v1/multiprocess tests/v1/distributed
      --ignore=tests/v1/distributed/test_bigtable_l2_adapter.py --ignore=tests/v1/distributed/test_bigtable_l2_adapter_integration.py
      tests/v1/test_vllm_mp_adapter.py tests/v1/test_atom_mp_adapter.py tests/v1/test_heartbeat_reregistration.py";;
  it)
    ENVS=$IT
    ARGS="tests/v1/layerwise tests/v1/distributed/rdma $(ls tests/v1/distributed/test_aerospike_*.py | tr '\n' ' ')";;
  wide)
    ENVS=$IT
    ARGS="tests/v1/layerwise tests/v1/distributed
      --ignore=tests/v1/distributed/test_bigtable_l2_adapter.py --ignore=tests/v1/distributed/test_bigtable_l2_adapter_integration.py";;
  pipe)
    ENVS=$IT; ARGS="tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py";;
  slow)
    ENVS="$IT RUN_AEROSPIKE_SLOW_INTEGRATION=1"
    ARGS="tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py -k closing_releases";;
  d14)
    ENVS=$IT
    ARGS="tests/v1/distributed/test_aerospike_record_layouts_integration.py
      tests/v1/distributed/test_aerospike_storage_integrity_integration.py
      tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py";;
  *) echo "unknown suite $SUITE"; exit 2;;
esac
echo "[$(date -u +%T)] $SUITE start" > $R/pytest.txt
# shellcheck disable=SC2086
env $ENVS timeout 5400 nice -n 19 python -m pytest -p no:cacheprovider -q -rfEs $TO \
  --junitxml=$R/junit.xml --durations=20 $ARGS >> $R/pytest.txt 2>&1
echo "pytest exit=$?" >> $R/pytest.txt
echo DONE > $R/done
