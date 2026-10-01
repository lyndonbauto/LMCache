#!/bin/bash
# D-14: an Aerospike integration test file against the private node 127.0.0.1:3200, CPU only, in lmc-d.
# Usage: run_storage_it.sh <log-name> <pytest args...>
set -u
NAME=$1; shift
OUT=/root/lmc-work/functional/d14/logs
docker exec -w /work/LMCache lmc-d env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= \
  PYTHONDONTWRITEBYTECODE=1 RUN_AEROSPIKE_INTEGRATION=1 \
  AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3200 AEROSPIKE_TEST_NAMESPACE=lmcache AEROSPIKE_TEST_EVICT_NAMESPACE=lmcache_evict \
  nice -n 19 timeout 1800 python -m pytest -p no:cacheprovider -v -rfEs \
  --junitxml=/work/functional/d14/logs/$NAME.junit.xml "$@" > $OUT/$NAME.txt 2>&1
echo "pytest exit=$?" >> $OUT/$NAME.txt
tail -3 $OUT/$NAME.txt
