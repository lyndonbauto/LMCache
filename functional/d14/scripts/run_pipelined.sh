#!/bin/bash
# D-14: kv-sink restart, then the pipelined RDMA integration test (warm-up run, counted run) from lmc-d.
set -u
D=/root/lmc-work/functional/d14; OUT=$D/logs
ENV="HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= PYTHONDONTWRITEBYTECODE=1 RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1"
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
