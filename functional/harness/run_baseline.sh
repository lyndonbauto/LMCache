#!/bin/bash
# Run one baseline session in the container: vLLM with NO --kv-transfer-config,
# greedy, fixed seed, no prefix caching; the corpus sent at batch size 1.
# Usage: run_baseline.sh <model> <corpus.json> <out_dir> <tag> [sets]
# VLLM_EXTRA adds vLLM flags; extra environment (for example
# VLLM_BATCH_INVARIANT=1) is inherited.
set -u
MODEL=$1; CORPUS=$2; OUT=$3; TAG=$4; SETS=${5:-}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
export HF_HOME=${HF_HOME:-/work/hf} HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1

VLLM_PID=0
cleanup() {
  [ "$VLLM_PID" -gt 0 ] && kill "$VLLM_PID" 2>/dev/null
  sleep 10
  [ "$VLLM_PID" -gt 0 ] && kill -9 "$VLLM_PID" 2>/dev/null
  pkill -9 -f "VLLM::" 2>/dev/null
  sleep 3
}
trap cleanup EXIT

echo "=== $TAG start $(date -u +%T) model=$MODEL extra='${VLLM_EXTRA:-}' batch_invariant=${VLLM_BATCH_INVARIANT:-0}"
vllm serve "$MODEL" --host 127.0.0.1 --port 8000 --seed 0 --no-enable-prefix-caching \
  --max-model-len 17408 --gpu-memory-utilization 0.6 ${VLLM_EXTRA:-} \
  > "$OUT/vllm_$TAG.log" 2>&1 &
VLLM_PID=$!
for _ in $(seq 1200); do
  curl -sf http://localhost:8000/health >/dev/null && break
  kill -0 "$VLLM_PID" 2>/dev/null || { echo "vLLM exited during startup"; tail -n 20 "$OUT/vllm_$TAG.log"; exit 1; }
  sleep 1
done
curl -sf http://localhost:8000/health >/dev/null || { echo "vLLM did not come up"; exit 1; }
echo "=== $TAG serving $(date -u +%T)"
SET_ARG=""; [ -n "$SETS" ] && SET_ARG="--sets $SETS"
python "$HERE/client.py" --corpus "$CORPUS" --out "$OUT/$TAG.json" --tag "$TAG" $SET_ARG
echo "=== $TAG client exit=$? $(date -u +%T)"
