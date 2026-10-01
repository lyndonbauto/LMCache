#!/bin/bash
# T-RDMA-06: byte oracle over 100 P-exact keys on the kv-sink server (3100),
# Soft-RoCE rxe0 GID 1, CPU only, inside lmc-c. Usage: run_rdma06.sh <log-name>
set -u
NAME=$1
OUT=/root/lmc-work/functional/item2/logs
docker exec -w /work/LMCache-cpu lmc-c env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= \
  PYTHONDONTWRITEBYTECODE=1 RUN_AEROSPIKE_INTEGRATION=1 \
  AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3100 AEROSPIKE_TEST_NAMESPACE=lmcache \
  RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  RDMA_ORACLE_CORPUS=/work/functional/corpus/corpus_llama-3.1-8b-instruct.json \
  RDMA_ORACLE_MODEL_NAME=/work/hf/hub/models--meta-llama--Llama-3.1-8B-Instruct/snapshots/0e9e39f249a16976918f6564b8830bc894c89659 \
  nice -n 19 timeout 1500 python -m pytest -p no:cacheprovider -v -rfEs --durations=5 \
  --junitxml=/work/functional/item2/logs/$NAME.junit.xml \
  tests/v1/distributed/test_aerospike_rdma_byte_oracle_integration.py > $OUT/$NAME.txt 2>&1
echo "pytest exit=$?" >> $OUT/$NAME.txt
