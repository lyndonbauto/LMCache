#!/bin/bash
# Run an Aerospike integration test file from the item-2 clone against the
# private node on 127.0.0.1:3200, CPU only, inside lmc-c.
# Usage: run_storage_it.sh <log-name> <pytest args...>
set -u
NAME=$1; shift
OUT=/root/lmc-work/functional/item2/logs
mkdir -p $OUT
docker exec -w /work/LMCache-cpu lmc-c env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= \
  PYTHONDONTWRITEBYTECODE=1 RUN_AEROSPIKE_INTEGRATION=1 \
  AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3200 AEROSPIKE_TEST_NAMESPACE=lmcache AEROSPIKE_TEST_EVICT_NAMESPACE=lmcache_evict \
  nice -n 19 timeout 900 python -m pytest -p no:cacheprovider -v -rfEs \
  --junitxml=/work/functional/item2/logs/$NAME.junit.xml "$@" 2>&1 | tee $OUT/$NAME.txt | tail -40
echo "pytest exit=${PIPESTATUS[0]}" | tee -a $OUT/$NAME.txt
