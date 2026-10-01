#!/bin/bash
# Run one reference session in the container: vLLM with NO
# --kv-transfer-config (VLLM_EXTRA adds flags, for example
# "--enable-prefix-caching --block-size 256"), then several sends in order
# against the same server, so a send can hit vLLM's own prefix cache from
# an earlier one. Each send records per-request growth of vLLM's prefix
# cache counters (client.py --metrics-urls), which shows where the cached
# prefix ended.
# Usage: run_ref.sh <model> <corpus.json> <out_dir> <tag> <port> <send>...
#   send: "name=<n> sets=<s> [conc=<N>]" -> <out_dir>/<tag>_<n>.json
# GPU_UTIL (default 0.45) lets two reference servers share the GPU; extra
# environment (for example VLLM_BATCH_INVARIANT=1) is inherited.
set -u
MODEL=$1; CORPUS=$2; OUT=$3; TAG=$4; PORT=$5; shift 5
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
export HF_HOME=${HF_HOME:-/work/hf} HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
URL=http://127.0.0.1:$PORT

VLLM_PID=0
cleanup() {
  [ "$VLLM_PID" -gt 0 ] && kill "$VLLM_PID" 2>/dev/null
  for _ in $(seq 30); do kill -0 "$VLLM_PID" 2>/dev/null || break; sleep 1; done
  [ "$VLLM_PID" -gt 0 ] && kill -9 "$VLLM_PID" 2>/dev/null
  sleep 3
}
trap cleanup EXIT

echo "=== $TAG start $(date -u +%T) model=$MODEL port=$PORT extra='${VLLM_EXTRA:-}' batch_invariant=${VLLM_BATCH_INVARIANT:-0}"
vllm serve "$MODEL" --host 127.0.0.1 --port "$PORT" --seed 0 \
  --max-model-len 17408 --gpu-memory-utilization "${GPU_UTIL:-0.45}" ${VLLM_EXTRA:-} \
  > "$OUT/vllm_$TAG.log" 2>&1 &
VLLM_PID=$!
for _ in $(seq 1200); do
  curl -sf "$URL/health" >/dev/null && break
  kill -0 "$VLLM_PID" 2>/dev/null || { echo "vLLM exited during startup"; tail -n 20 "$OUT/vllm_$TAG.log"; exit 1; }
  sleep 1
done
curl -sf "$URL/health" >/dev/null || { echo "vLLM did not come up"; exit 1; }
echo "=== $TAG serving $(date -u +%T)"
for step in "$@"; do
  name=""; sets=""; conc=1
  for kv in $step; do
    case $kv in
      name=*) name=${kv#name=};; sets=*) sets=${kv#sets=};; conc=*) conc=${kv#conc=};;
      *) echo "unknown send argument $kv"; exit 1;;
    esac
  done
  echo "=== $TAG send $name sets=$sets conc=$conc $(date -u +%T)"
  python "$HERE/client.py" --corpus "$CORPUS" --out "$OUT/${TAG}_$name.json" --tag "${TAG}_$name" \
    --url "$URL" --sets "$sets" --concurrency "$conc" --metrics-urls "$URL/metrics" \
    || { echo "=== $TAG send $name FAILED $(date -u +%T)"; exit 1; }
done
echo "=== $TAG done $(date -u +%T)"
