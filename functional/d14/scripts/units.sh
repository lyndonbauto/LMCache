#!/bin/bash
# D-14: rebuild, build the RDMA test harness, then the day1fix batch1 unit set (CPU only, lmc-d).
set -u
R=/work/functional/d14/logs/${1:-units}
mkdir -p $R
/work/functional/d14/scripts/build.sh > $R/build.txt 2>&1 || { echo BUILD_FAILED > $R/done; exit 1; }
cd /work/LMCache
nice -n 19 make -C tests/v1/distributed/rdma pyharness AEROSPIKE_PREFIX=/work/deps/aerospike-install/usr YAML_LIB=-l:libyaml-0.so.2 > $R/pyharness.txt 2>&1; echo "make exit=$?" >> $R/pyharness.txt
TO=""; python -c "import pytest_timeout" 2>/dev/null && TO="--timeout=600"
HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 timeout 5400 nice -n 19 python -m pytest -p no:cacheprovider -q -rfEs $TO \
  --junitxml=$R/junit.xml --durations=20 \
  tests/v1/layerwise tests/v1/multiprocess tests/v1/distributed --ignore=tests/v1/distributed/test_bigtable_l2_adapter.py --ignore=tests/v1/distributed/test_bigtable_l2_adapter_integration.py \
  tests/v1/test_vllm_mp_adapter.py tests/v1/test_atom_mp_adapter.py \
  tests/v1/test_heartbeat_reregistration.py > $R/pytest.txt 2>&1
echo "pytest exit=$?" >> $R/pytest.txt
echo DONE > $R/done
