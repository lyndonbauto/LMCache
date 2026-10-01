#!/bin/bash
# Usage: run_threshold.sh <log-name> <chunks...>
set -u
NAME=$1; shift
OUT=/root/lmc-work/functional/item2/logs
docker exec -w /work/LMCache-cpu lmc-c env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/work/LMCache-cpu RUN_AEROSPIKE_INTEGRATION=1 \
  AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3100 AEROSPIKE_TEST_NAMESPACE=lmcache \
  RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  RDMA_ORACLE_CORPUS=/work/functional/corpus/corpus_llama-3.1-8b-instruct.json \
  RDMA_ORACLE_MODEL_NAME=/work/hf/hub/models--meta-llama--Llama-3.1-8B-Instruct/snapshots/0e9e39f249a16976918f6564b8830bc894c89659 \
  nice -n 19 timeout 900 python /work/functional/item2/scripts/rdma06_threshold.py "$@" > $OUT/$NAME.txt 2>&1
echo "exit=$?" >> $OUT/$NAME.txt
