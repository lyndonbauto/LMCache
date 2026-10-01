#!/bin/bash
# D-14: E1 RDMA suite (rxe0, GID 1) + make test, then the pipelined RDMA integration test
# against the kv-sink server on 127.0.0.1:3100 (warm-up run, then the counted run). lmc-d, CPU only.
set -u
D=/root/lmc-work/functional/d14; OUT=$D/logs
ENV="HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= PYTHONDONTWRITEBYTECODE=1 RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1"
docker exec -w /work/LMCache lmc-d env $ENV nice -n 19 timeout 1200 python -m pytest -p no:cacheprovider -v -rfEs \
  --junitxml=/work/functional/d14/logs/rdma_suite.junit.xml tests/v1/distributed/rdma > $OUT/rdma_suite.txt 2>&1
echo "pytest exit=$?" >> $OUT/rdma_suite.txt
docker exec -w /work/LMCache lmc-d env $ENV nice -n 19 timeout 1200 make -C tests/v1/distributed/rdma test RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  AEROSPIKE_PREFIX=/work/deps/aerospike-install/usr YAML_LIB=-l:libyaml-0.so.2 > $OUT/rdma_harness_direct.txt 2>&1
echo "make exit=$?" >> $OUT/rdma_harness_direct.txt
KV="bash /root/lmc-work/LMCache-d14/functional/harness/kvsink_server.sh"
export KVSINK_CONF=/root/lmc-work/LMCache-d14/functional/configs/aerospike-kvsink.conf KVSINK_LOG=$OUT/asd-kvsink.log
{ $KV stop; $KV start; } > $OUT/kvsink_restart.txt 2>&1
for run in warm counted; do
  docker exec -w /work/LMCache lmc-d env $ENV RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3100 \
    AEROSPIKE_TEST_NAMESPACE=lmcache nice -n 19 timeout 600 python -m pytest -p no:cacheprovider -v -rfEs \
    --junitxml=/work/functional/d14/logs/pipelined_it_$run.junit.xml \
    tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py > $OUT/pipelined_it_$run.txt 2>&1
  echo "pytest exit=$?" >> $OUT/pipelined_it_$run.txt
done
echo DONE > $OUT/rdma.done
