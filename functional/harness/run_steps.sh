#!/bin/bash
# Run one scripted LMCache session in the container: a sequence of steps
# against one LMCache server (L1 in CPU memory plus LMCACHE_SERVER_EXTRA,
# e.g. an Aerospike L2 adapter) and vLLM with the MP connector. Unlike
# run_lmcache.sh (cold send, optional restart, warm send) the steps are
# given on the command line, so a test can send several prompt subsets,
# switch models, restart the server or edit L2 between sends.
# Usage: run_steps.sh <out_dir> <tag> <layerwise true|false> <step>...
# Steps (one argument each; words after the verb are key=value):
#   server                         start the LMCache server
#   vllm model=<id>                start vLLM (stopping a running one first)
#   vllm_stop                      stop vLLM
#   send name=<n> sets=<s> [ids=<a,b>] [salt=<s>] [conc=<N>] [corpus=<path>] [stats=1]
#                                  client.py -> $OUT/<tag>_<n>.json (tag <tag>_<n>);
#                                  stats=1 dumps Aerospike stats around it
#   settle                         wait until L2 writes stop
#   restart                        settle, restart the server (L1 lost), wait
#                                  for vLLM to re-register
#   reset                          truncate the L2 set, then restart (empty L1 and L2)
#   keys name=<n>                  snapshot L2 digests -> $OUT/keys_<tag>_<n>.json
#   delkeys before=<n> after=<n>   delete the L2 records added between two snapshots
# Environment: CORPUS (default corpus for send), LMCACHE_SERVER_EXTRA,
# VLLM_EXTRA, KV_EXTRA, KV_LOAD_FAILURE_POLICY (default fail), L2_NAMESPACE
# (default lmcache), SEND_TIMEOUT (per-request seconds, default 900).
# If a send fails, $OUT/HANG_<tag>_<n> is written with the PIDs and the
# script waits up to 300 s for $OUT/STACKS_DONE_<tag>_<n> (the host driver
# captures Python stacks) before stopping everything.
set -u
OUT=$1; TAG=$2; LW=$3; shift 3
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
export HF_HOME=${HF_HOME:-/work/hf} HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
export LMCACHE_LOG_LEVEL=${LMCACHE_LOG_LEVEL:-DEBUG}
POLICY=${KV_LOAD_FAILURE_POLICY:-fail}
NS=${L2_NAMESPACE:-lmcache}
if [ "$LW" = true ]; then SFLAG=--use-layerwise; else SFLAG=--no-use-layerwise; fi
LOG=$OUT/lmcache_$TAG.log
METRICS_URLS=http://localhost:8000/metrics,http://localhost:8080/metrics

SERVER_PID=0; VLLM_PID=0
stop_server() {
  [ "$SERVER_PID" -gt 0 ] && kill "$SERVER_PID" 2>/dev/null
  sleep 5
  [ "$SERVER_PID" -gt 0 ] && kill -9 "$SERVER_PID" 2>/dev/null
  SERVER_PID=0
}
start_server() {
  lmcache server --port 6555 --http-host 127.0.0.1 --http-port 8080 --l1-size-gb 40 --eviction-policy LRU \
    --chunk-size 256 $SFLAG ${LMCACHE_SERVER_EXTRA:-} >> "$LOG" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 120); do
    curl -sf http://localhost:8080/metrics >/dev/null && return 0
    kill -0 "$SERVER_PID" 2>/dev/null || { echo "LMCache server exited"; return 1; }
    sleep 1
  done
  echo "LMCache server did not come up"; return 1
}
stop_vllm() {
  [ "$VLLM_PID" -gt 0 ] || return 0
  kill "$VLLM_PID" 2>/dev/null
  for _ in $(seq 30); do kill -0 "$VLLM_PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$VLLM_PID" 2>/dev/null
  pkill -9 -f "VLLM::" 2>/dev/null
  VLLM_PID=0
  sleep 10
}
start_vllm() {
  local model=$1 log=$OUT/vllm_${TAG}_$(basename "$1").log
  local kv="{\"kv_connector\":\"LMCacheMPConnector\",\"kv_role\":\"kv_both\",\
\"kv_connector_module_path\":\"lmcache.integration.vllm.lmcache_mp_connector\",\
\"kv_load_failure_policy\":\"$POLICY\",\"kv_connector_extra_config\":{\
\"lmcache.mp.host\":\"tcp://localhost\",\"lmcache.mp.port\":6555,\
\"lmcache.mp.use_layerwise\":$LW${KV_EXTRA:+,$KV_EXTRA}}}"
  local before; before=$(grep -c "Registered KV cache" "$LOG")
  vllm serve "$model" --host 127.0.0.1 --port 8000 --seed 0 --no-enable-prefix-caching \
    --max-model-len 17408 --gpu-memory-utilization 0.6 ${VLLM_EXTRA:-} \
    --kv-transfer-config "$kv" >> "$log" 2>&1 &
  VLLM_PID=$!
  for _ in $(seq 1800); do
    curl -sf http://localhost:8000/health >/dev/null && break
    kill -0 "$VLLM_PID" 2>/dev/null || { echo "vLLM exited during startup"; tail -n 30 "$log"; return 1; }
    sleep 1
  done
  curl -sf http://localhost:8000/health >/dev/null || { echo "vLLM did not come up"; return 1; }
  [ "$(grep -c "Registered KV cache" "$LOG")" -gt "$before" ] \
    || echo "warning: no new KV cache registration in the LMCache log"
  echo "=== $TAG vLLM serving $model $(date -u +%T)"
}
wait_reregistered() {
  local registrations=$1
  [ "$VLLM_PID" -gt 0 ] || return 0
  local restarted_at; restarted_at=$(date +%s)
  for _ in $(seq 120); do
    [ "$(grep -c "Registered KV cache" "$LOG")" -gt "$registrations" ] && break
    sleep 1
  done
  [ "$(grep -c "Registered KV cache" "$LOG")" -gt "$registrations" ] \
    || { echo "vLLM did not re-register with the restarted server"; return 1; }
  echo "=== $TAG vLLM re-registered $(date -u +%T), $(( $(date +%s) - restarted_at )) s after the server came back"
  sleep 5
}
restart_server() {
  local registrations; registrations=$(grep -c "Registered KV cache" "$LOG")
  stop_server
  start_server || return 1
  wait_reregistered "$registrations"
}
settle() {
  python "$HERE/wait_l2_settle.py" --namespace "$NS" || echo "warning: L2 writes had not settled"
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

send() {
  local name="" sets="" ids="" salt="" conc=1 corpus=${CORPUS:-} stats=0 kv
  for kv in "$@"; do
    case $kv in
      name=*) name=${kv#name=};; sets=*) sets=${kv#sets=};; ids=*) ids=${kv#ids=};;
      salt=*) salt=${kv#salt=};; conc=*) conc=${kv#conc=};; corpus=*) corpus=${kv#corpus=};;
      stats=*) stats=${kv#stats=};; *) echo "send: unknown argument $kv"; return 1;;
    esac
  done
  local run=${TAG}_$name
  [ "$stats" = 1 ] && python "$HERE/l2_stats.py" dump --namespace "$NS" --out "$OUT/l2stats_${run}_before.json"
  echo "=== $TAG send $name sets=$sets ids=${ids:--} salt=${salt:--} conc=$conc $(date -u +%T)"
  if ! python "$HERE/client.py" --corpus "$corpus" --out "$OUT/$run.json" --tag "$run" --sets "$sets" \
      --ids "$ids" --salt "$salt" --concurrency "$conc" --timeout "${SEND_TIMEOUT:-900}" \
      --metrics-urls "$METRICS_URLS"; then
    echo "=== $TAG send $name FAILED $(date -u +%T)"
    echo "server_pid=$SERVER_PID vllm_pid=$VLLM_PID engine_pids=$(pgrep -f 'VLLM::' | paste -sd,)" > "$OUT/HANG_$run"
    for _ in $(seq 300); do [ -f "$OUT/STACKS_DONE_$run" ] && break; sleep 1; done
    return 1
  fi
  [ "$stats" = 1 ] && python "$HERE/l2_stats.py" dump --namespace "$NS" --out "$OUT/l2stats_${run}_after.json"
  return 0
}

echo "=== $TAG start $(date -u +%T) layerwise=$LW policy=$POLICY server_extra='${LMCACHE_SERVER_EXTRA:-}' vllm_extra='${VLLM_EXTRA:-}'"
for step in "$@"; do
  read -r verb rest <<< "$step"
  case $verb in
    server) start_server || exit 1;;
    vllm) start_vllm "${rest#model=}" || exit 1;;
    vllm_stop) stop_vllm; echo "=== $TAG vLLM stopped $(date -u +%T)";;
    send) # shellcheck disable=SC2086
      send $rest || exit 1;;
    settle) settle;;
    restart) settle; echo "=== $TAG restarting the LMCache server (L1 is lost) $(date -u +%T)"
      restart_server || exit 1;;
    reset) python "$HERE/l2_keys.py" --namespace "$NS" truncate; sleep 5
      echo "=== $TAG L2 truncated, restarting the LMCache server $(date -u +%T)"
      restart_server || exit 1;;
    keys) settle; python "$HERE/l2_keys.py" --namespace "$NS" dump --out "$OUT/keys_${TAG}_${rest#name=}.json";;
    delkeys) read -r b a <<< "$rest"
      python "$HERE/l2_keys.py" --namespace "$NS" delete "$OUT/keys_${TAG}_${b#before=}.json" "$OUT/keys_${TAG}_${a#after=}.json" || exit 1;;
    *) echo "unknown step: $step"; exit 1;;
  esac
done
curl -sf http://localhost:8080/metrics > "$OUT/metrics_$TAG.txt"
echo "=== $TAG done $(date -u +%T)"
