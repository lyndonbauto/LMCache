#!/bin/bash
# Stage 6 CPU half: the cluster integration tests inside lmc-c, CPU only.
# Usage: run_cluster_it.sh <log-name> [pytest -k expression] [race rounds]
set -u
NAME=$1; K=${2:-}; ROUNDS=${3:-1000}
OUT=/root/lmc-work/functional/stage6/logs
docker exec -w /work/LMCache-cpu lmc-c env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= \
  PYTHONDONTWRITEBYTECODE=1 RUN_AEROSPIKE_CLUSTER_INTEGRATION=1 \
  AEROSPIKE_CLUSTER_HOSTS=127.0.0.1:3300,127.0.0.1:3310,127.0.0.1:3320 \
  AEROSPIKE_CLUSTER_CTL=/work/aero-cluster/ctl AEROSPIKE_CLUSTER_RACE_ROUNDS=$ROUNDS \
  nice -n 19 timeout 3000 python -m pytest -p no:cacheprovider -v -s -rfEs --durations=0 \
  --junitxml=/work/functional/stage6/logs/$NAME.junit.xml ${K:+-k "$K"} \
  tests/v1/distributed/test_aerospike_cluster_integration.py > $OUT/$NAME.txt 2>&1
echo "pytest exit=$?" >> $OUT/$NAME.txt
