#!/bin/bash
# T-RDMA-06, CPU half: start the kv-sink server, warm it (kvsink_smoke.sh),
# run the byte oracle (100 P-exact keys, pipelined RDMA fetch vs plain get)
# in lmc-c from the LMCache-cpu clone, then stop the server.
# Runs on the host. Usage: rdma06_cpu.sh <name> [pytest -k expression]
# Output: /root/lmc-work/functional/stage3/logs/rdma06/<name>.{txt,junit.xml}
set -u
NAME=${1:?name}; SELECT=${2:-}
CLONE=/root/lmc-work/LMCache-cpu
OUT=/root/lmc-work/functional/stage3/logs/rdma06
mkdir -p "$OUT"
H=$CLONE/functional/harness
SNAP=/work/hf/hub/models--meta-llama--Llama-3.1-8B-Instruct/snapshots/0e9e39f249a16976918f6564b8830bc894c89659

KVSINK_LOG=$OUT/asd-kvsink_$NAME.log bash $H/kvsink_server.sh start || exit 1
trap 'bash $H/kvsink_server.sh stop' EXIT
docker exec -e LMCACHE_DIR=$CLONE aero-kvsink bash $H/kvsink_smoke.sh $OUT/warm_$NAME > $OUT/warm_$NAME.txt 2>&1
echo "warm-up: $(grep -E 'passed|failed' $OUT/warm_$NAME/pipelined_it.txt | tail -n 1)"

docker exec -w /work/LMCache-cpu lmc-c env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= \
  PYTHONDONTWRITEBYTECODE=1 RUN_AEROSPIKE_INTEGRATION=1 \
  AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3100 AEROSPIKE_TEST_NAMESPACE=lmcache \
  RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  RDMA_ORACLE_CORPUS=/work/functional/corpus/corpus_llama-3.1-8b-instruct.json \
  RDMA_ORACLE_MODEL_NAME=$SNAP \
  nice -n 19 timeout 1500 python -m pytest -p no:cacheprovider -v -rfEs --durations=5 \
  ${SELECT:+-k "$SELECT"} --junitxml=/work/functional/stage3/logs/rdma06/$NAME.junit.xml \
  tests/v1/distributed/test_aerospike_rdma_byte_oracle_integration.py > $OUT/$NAME.txt 2>&1
echo "pytest exit=$?" | tee -a $OUT/$NAME.txt
grep -E "PASSED|FAILED|passed|failed" $OUT/$NAME.txt | tail -n 5
