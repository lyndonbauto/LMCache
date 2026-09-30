#!/bin/bash
# Run one LMCache session in the container: the LMCache server (L1 in CPU
# memory, plus whatever LMCACHE_SERVER_EXTRA adds, for example an L2
# adapter), vLLM with the MP connector, then the corpus twice at batch size 1:
# "cold" (computed and stored) and "warm" (served from the cache).
# Usage: run_lmcache.sh <model> <corpus.json> <out_dir> <tag> <layerwise true|false> [sets]
# Environment: LMCACHE_SERVER_EXTRA, VLLM_EXTRA, KV_LOAD_FAILURE_POLICY
# (default "fail", so a bad load errors instead of silently recomputing),
# RESTART_SERVER_BEFORE_WARM=1 (wait for L2 writes to settle, then restart
# the LMCache server between the two sends, so the warm hit must come from
# L2; L2_NAMESPACE names the Aerospike namespace, default lmcache).
set -u
MODEL=$1; CORPUS=$2; OUT=$3; TAG=$4; LW=$5; SETS=${6:-}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
export HF_HOME=${HF_HOME:-/work/hf} HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
export LMCACHE_LOG_LEVEL=${LMCACHE_LOG_LEVEL:-DEBUG}
POLICY=${KV_LOAD_FAILURE_POLICY:-fail}
if [ "$LW" = true ]; then SFLAG=--use-layerwise; else SFLAG=--no-use-layerwise; fi

SERVER_PID=0; VLLM_PID=0
stop_server() {
  [ "$SERVER_PID" -gt 0 ] && kill "$SERVER_PID" 2>/dev/null
  sleep 5
  [ "$SERVER_PID" -gt 0 ] && kill -9 "$SERVER_PID" 2>/dev/null
  SERVER_PID=0
}
start_server() {
  lmcache server --port 6555 --http-host 127.0.0.1 --http-port 8080 --l1-size-gb 40 --eviction-policy LRU \
    --chunk-size 256 $SFLAG ${LMCACHE_SERVER_EXTRA:-} >> "$OUT/lmcache_$TAG.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 120); do
    curl -sf http://localhost:8080/metrics >/dev/null && return 0
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "LMCache server exited"; return 1; }
    sleep 1
  done
  echo "LMCache server did not come up"; return 1
}
cleanup() {
  [ "$VLLM_PID" -gt 0 ] && kill "$VLLM_PID" 2>/dev/null
  sleep 10
  [ "$VLLM_PID" -gt 0 ] && kill -9 "$VLLM_PID" 2>/dev/null
  pkill -9 -f "VLLM::" 2>/dev/null
  stop_server
  sleep 3
}
trap cleanup EXIT

echo "=== $TAG start $(date -u +%T) model=$MODEL layerwise=$LW policy=$POLICY server_extra='${LMCACHE_SERVER_EXTRA:-}' vllm_extra='${VLLM_EXTRA:-}'"
start_server || exit 1
KV="{\"kv_connector\":\"LMCacheMPConnector\",\"kv_role\":\"kv_both\",\
\"kv_connector_module_path\":\"lmcache.integration.vllm.lmcache_mp_connector\",\
\"kv_load_failure_policy\":\"$POLICY\",\"kv_connector_extra_config\":{\
\"lmcache.mp.host\":\"tcp://localhost\",\"lmcache.mp.port\":6555,\
\"lmcache.mp.use_layerwise\":$LW}}"
vllm serve "$MODEL" --host 127.0.0.1 --port 8000 --seed 0 --no-enable-prefix-caching \
  --max-model-len 17408 --gpu-memory-utilization 0.6 ${VLLM_EXTRA:-} \
  --kv-transfer-config "$KV" > "$OUT/vllm_$TAG.log" 2>&1 &
VLLM_PID=$!
for _ in $(seq 1800); do
  curl -sf http://localhost:8000/health >/dev/null && break
  kill -0 "$VLLM_PID" 2>/dev/null || { echo "vLLM exited during startup"; tail -n 30 "$OUT/vllm_$TAG.log"; exit 1; }
  sleep 1
done
curl -sf http://localhost:8000/health >/dev/null || { echo "vLLM did not come up"; exit 1; }
echo "=== $TAG serving $(date -u +%T)"
SET_ARG=""; [ -n "$SETS" ] && SET_ARG="--sets $SETS"
python "$HERE/client.py" --corpus "$CORPUS" --out "$OUT/${TAG}_cold.json" --tag "${TAG}_cold" $SET_ARG \
  || { echo "cold send failed"; exit 1; }
sleep 3
if [ "${RESTART_SERVER_BEFORE_WARM:-0}" = 1 ]; then
  python "$HERE/wait_l2_settle.py" --namespace "${L2_NAMESPACE:-lmcache}" \
    || echo "warning: L2 writes had not settled"
  echo "=== $TAG restarting the LMCache server (L1 is lost) $(date -u +%T)"
  registrations=$(grep -c "Registered KV cache" "$OUT/lmcache_$TAG.log")
  stop_server; start_server || exit 1
  # vLLM re-registers its KV cache on its next heartbeat; until then every
  # lookup misses, which would make the warm send a recompute.
  for _ in $(seq 120); do
    [ "$(grep -c "Registered KV cache" "$OUT/lmcache_$TAG.log")" -gt "$registrations" ] && break
    sleep 1
  done
  [ "$(grep -c "Registered KV cache" "$OUT/lmcache_$TAG.log")" -gt "$registrations" ] \
    || { echo "vLLM did not re-register with the restarted server"; exit 1; }
  echo "=== $TAG vLLM re-registered $(date -u +%T)"
  sleep 5
fi
python "$HERE/client.py" --corpus "$CORPUS" --out "$OUT/${TAG}_warm.json" --tag "${TAG}_warm" $SET_ARG \
  || { echo "warm send failed"; exit 1; }
curl -sf http://localhost:8080/metrics > "$OUT/metrics_$TAG.txt"
echo "=== $TAG done $(date -u +%T)"
