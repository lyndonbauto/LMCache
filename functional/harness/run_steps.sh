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
# Fault-test steps (Stages 3 and 5):
#   send ... bg=1 errors=1         bg=1 runs the send in the background (finish
#                                  it with wait_bg); errors=1 records failed
#                                  requests instead of failing the step
#   wait_bg                        wait for background sends; never fails
#   wait_log what=<w> [timeout=<s>] wait for a new LMCache log line since the
#                                  last background send: w = retrieve_start,
#                                  retrieve_end, lookup_end
#   restart [extra=alt] [downtime=<s>]
#                                  as restart; extra=alt starts the server with
#                                  LMCACHE_SERVER_EXTRA_ALT instead (and keeps it
#                                  for later restarts until extra=main)
#   kill9                          SIGKILL the LMCache server (no restart)
#   server_up                      start the server again after kill9 and wait
#                                  for vLLM to re-register
#   freeze_server secs=<s>         SIGSTOP the LMCache server for s seconds
#   host action=<a> [k=v ...]      ask the host driver to run action a (it
#                                  watches $OUT/HOSTREQ_*; actions and their
#                                  words in functional/harness/host_actions.sh)
#                                  and wait up to 300 s for its answer
#   vllm_check name=<n>            record whether vLLM is alive and its engine
#                                  error lines -> $OUT/vllm_check_<tag>_<n>.txt;
#                                  a dead vLLM is cleaned up so `vllm` restarts it
#   vllm_ensure model=<id>         start vLLM only if none is running (after a
#                                  vllm_check found it dead)
#   l2seg prompt=<id> chunk=<c> seg=<s>
#                                  delete record <s> of chunk <c> of a prompt
#                                  (l2_segments.py); the meta record stays
#   rxe name=<n>                   snapshot rxe0's port counters (packets of
#                                  4096 bytes) -> $OUT/rxe_<tag>_<n>.txt
#   sleep secs=<s>
# Second vLLM instance (Stage 4: retrieves from one vLLM run one at a time on
# its LMCache affinity thread, so concurrent retrieves need two clients and
# the server's --max-gpu-workers 2):
#   vllm2 model=<id>               start a second vLLM on 127.0.0.1:8001 against
#                                  the same LMCache server (log vllm2_<tag>_*.log)
#   vllm2_stop                     stop it
#   send ... port=8001             send to the second instance (default 8000)
# With both running, restart and server_up wait for both to re-register.
# Environment: CORPUS (default corpus for send), LMCACHE_SERVER_EXTRA,
# LMCACHE_SERVER_EXTRA_ALT, VLLM_EXTRA, KV_EXTRA, KV_LOAD_FAILURE_POLICY
# (default fail), L2_NAMESPACE (default lmcache), L2_PORT (default 3000),
# L2_SET (default kv_chunks), SEND_TIMEOUT (per-request seconds, default 900).
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
L2_ARGS="--port ${L2_PORT:-3000} --namespace $NS"
if [ "$LW" = true ]; then SFLAG=--use-layerwise; else SFLAG=--no-use-layerwise; fi
LOG=$OUT/lmcache_$TAG.log
SERVER_PID=0; VLLM_PID=0; VLLM2_PID=0
SERVER_EXTRA=${LMCACHE_SERVER_EXTRA:-}
BG_PIDS=""; BG_MARK=0; HOST_SEQ=0
stop_server() {
  [ "$SERVER_PID" -gt 0 ] && kill "$SERVER_PID" 2>/dev/null
  sleep 5
  [ "$SERVER_PID" -gt 0 ] && kill -9 "$SERVER_PID" 2>/dev/null
  SERVER_PID=0
}
start_server() {
  echo "=== $TAG server extra: $SERVER_EXTRA" >> "$LOG"
  lmcache server --port 6555 --http-host 127.0.0.1 --http-port 8080 --l1-size-gb 40 --eviction-policy LRU \
    --chunk-size 256 $SFLAG $SERVER_EXTRA >> "$LOG" 2>&1 &
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
stop_vllm2() {
  [ "$VLLM2_PID" -gt 0 ] || return 0
  kill "$VLLM2_PID" 2>/dev/null
  for _ in $(seq 30); do kill -0 "$VLLM2_PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$VLLM2_PID" 2>/dev/null
  VLLM2_PID=0
  sleep 10
}
# vLLM instances that must re-register after an LMCache restart.
n_vllm() { echo $(( (VLLM_PID > 0) + (VLLM2_PID > 0) )); }
start_vllm() {
  start_vllm_on "$1" 8000 vllm || return 1
  VLLM_PID=$LAUNCHED_PID
}
start_vllm2() {
  start_vllm_on "$1" 8001 vllm2 || return 1
  VLLM2_PID=$LAUNCHED_PID
}
# start_vllm_on <model> <port> <log-prefix>: sets LAUNCHED_PID.
LAUNCHED_PID=0
start_vllm_on() {
  local model=$1 port=$2 log=$OUT/${3}_${TAG}_$(basename "$1").log
  local kv="{\"kv_connector\":\"LMCacheMPConnector\",\"kv_role\":\"kv_both\",\
\"kv_connector_module_path\":\"lmcache.integration.vllm.lmcache_mp_connector\",\
\"kv_load_failure_policy\":\"$POLICY\",\"kv_connector_extra_config\":{\
\"lmcache.mp.host\":\"tcp://localhost\",\"lmcache.mp.port\":6555,\
\"lmcache.mp.use_layerwise\":$LW${KV_EXTRA:+,$KV_EXTRA}}}"
  local before; before=$(grep -c "Registered KV cache" "$LOG")
  vllm serve "$model" --host 127.0.0.1 --port "$port" --seed 0 --no-enable-prefix-caching \
    --max-model-len 17408 --gpu-memory-utilization 0.6 ${VLLM_EXTRA:-} \
    --kv-transfer-config "$kv" >> "$log" 2>&1 &
  LAUNCHED_PID=$!
  for _ in $(seq 1800); do
    curl -sf "http://localhost:$port/health" >/dev/null && break
    kill -0 "$LAUNCHED_PID" 2>/dev/null || { echo "vLLM exited during startup"; tail -n 30 "$log"; return 1; }
    sleep 1
  done
  curl -sf "http://localhost:$port/health" >/dev/null || { echo "vLLM did not come up"; return 1; }
  [ "$(grep -c "Registered KV cache" "$LOG")" -gt "$before" ] \
    || echo "warning: no new KV cache registration in the LMCache log"
  echo "=== $TAG vLLM serving $model on port $port $(date -u +%T)"
}
wait_reregistered() {
  local registrations=$1 want; want=$(( $1 + $(n_vllm) ))
  [ "$(n_vllm)" -gt 0 ] || return 0
  local restarted_at; restarted_at=$(date +%s)
  for _ in $(seq 120); do
    [ "$(grep -c "Registered KV cache" "$LOG")" -ge "$want" ] && break
    sleep 1
  done
  [ "$(grep -c "Registered KV cache" "$LOG")" -ge "$want" ] \
    || { echo "vLLM did not re-register with the restarted server ($(n_vllm) instance(s))"; return 1; }
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
  python "$HERE/wait_l2_settle.py" $L2_ARGS || echo "warning: L2 writes had not settled"
}
wait_bg() {
  local pid
  for pid in $BG_PIDS; do wait "$pid"; echo "=== $TAG background send (pid $pid) exited $? $(date -u +%T)"; done
  BG_PIDS=""
}
wait_log() {
  local what="" timeout=60 kv pattern
  for kv in "$@"; do
    case $kv in what=*) what=${kv#what=};; timeout=*) timeout=${kv#timeout=};;
      *) echo "wait_log: unknown argument $kv"; return 1;; esac
  done
  case $what in
    retrieve_start) pattern="MP retrieve start:";;
    retrieve_end) pattern="MP retrieve end:";;
    lookup_end) pattern="MP lookup/prefetch end:";;
    *) echo "wait_log: unknown what=$what"; return 1;;
  esac
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$(tail -n +"$((BG_MARK + 1))" "$LOG" | grep -c "$pattern")" -gt 0 ] \
      && { echo "=== $TAG saw '$pattern' $(date -u +%T.%N | cut -c1-12)"; return 0; }
    sleep 0.05
  done
  echo "=== $TAG wait_log: no '$pattern' within $timeout s"; return 1
}
host_action() {
  # The request file holds the step's key=value words as given; the host
  # driver parses them (action=, secs=, on=, ...).
  case "$*" in *action=*) ;; *) echo "host: action= is required"; return 1;; esac
  HOST_SEQ=$((HOST_SEQ + 1))
  local req=$OUT/HOSTREQ_${TAG}_$HOST_SEQ
  # A rerun into the same directory must not see the last run's answer; the
  # request goes first so the host driver never replays it.
  rm -f "$req" "$OUT/HOSTDONE_${TAG}_$HOST_SEQ" "$OUT/HOSTFAULT_${TAG}_$HOST_SEQ.txt"
  echo "$*" > "$req.tmp" && mv "$req.tmp" "$req"
  echo "=== $TAG host request $HOST_SEQ: $* $(date -u +%T.%N | cut -c1-12)"
  for _ in $(seq 3000); do
    [ -f "$OUT/HOSTDONE_${TAG}_$HOST_SEQ" ] && {
      echo "=== $TAG host request $HOST_SEQ done: $(cat "$OUT/HOSTDONE_${TAG}_$HOST_SEQ") $(date -u +%T.%N | cut -c1-12)"
      return 0; }
    sleep 0.1
  done
  echo "=== $TAG host request $HOST_SEQ: no answer from the host driver in 300 s"; return 1
}
vllm_check() {
  local name=${1#name=} f
  f=$OUT/vllm_check_${TAG}_$name.txt
  local vlog; vlog=$(ls -t "$OUT"/vllm_"${TAG}"_*.log 2>/dev/null | head -n 1)
  {
    if curl -sf http://localhost:8000/health >/dev/null; then echo "vllm=alive"; else echo "vllm=dead"; fi
    echo "generation_timeout_errors=$(grep -c LayerProgressRetrieveGenerationTimeoutError "$vlog" 2>/dev/null)"
    echo "engine_dead_errors=$(grep -cE 'EngineDeadError|EngineCore.*(died|failed)' "$vlog" 2>/dev/null)"
    echo "tracebacks=$(grep -c Traceback "$vlog" 2>/dev/null)"
    echo "last_error: $(grep -E 'Error' "$vlog" 2>/dev/null | tail -n 1 | cut -c1-300)"
  } > "$f"
  echo "=== $TAG vllm_check $name: $(paste -sd' ' "$f" | cut -c1-200)"
  if grep -q vllm=dead "$f"; then
    [ "$VLLM_PID" -gt 0 ] && kill -9 "$VLLM_PID" 2>/dev/null
    pkill -9 -f "VLLM::" 2>/dev/null
    VLLM_PID=0; sleep 10
  fi
}
cleanup() {
  [ "$VLLM_PID" -gt 0 ] && kill "$VLLM_PID" 2>/dev/null
  [ "$VLLM2_PID" -gt 0 ] && kill "$VLLM2_PID" 2>/dev/null
  sleep 10
  [ "$VLLM_PID" -gt 0 ] && kill -9 "$VLLM_PID" 2>/dev/null
  [ "$VLLM2_PID" -gt 0 ] && kill -9 "$VLLM2_PID" 2>/dev/null
  pkill -9 -f "VLLM::" 2>/dev/null
  stop_server
  sleep 3
}
trap cleanup EXIT

send() {
  local name="" sets="" ids="" salt="" conc=1 corpus=${CORPUS:-} stats=0 bg=0 errors=0 port=8000 kv
  for kv in "$@"; do
    case $kv in
      name=*) name=${kv#name=};; sets=*) sets=${kv#sets=};; ids=*) ids=${kv#ids=};;
      salt=*) salt=${kv#salt=};; conc=*) conc=${kv#conc=};; corpus=*) corpus=${kv#corpus=};;
      stats=*) stats=${kv#stats=};; bg=*) bg=${kv#bg=};; errors=*) errors=${kv#errors=};;
      port=*) port=${kv#port=};;
      *) echo "send: unknown argument $kv"; return 1;;
    esac
  done
  local run=${TAG}_$name
  local err_flag=""; [ "$errors" = 1 ] && err_flag=--allow-errors
  local url=http://localhost:$port metrics=http://localhost:$port/metrics,http://localhost:8080/metrics
  [ "$stats" = 1 ] && python "$HERE/l2_stats.py" dump $L2_ARGS --out "$OUT/l2stats_${run}_before.json"
  echo "=== $TAG send $name sets=$sets ids=${ids:--} salt=${salt:--} conc=$conc bg=$bg errors=$errors port=$port $(date -u +%T)"
  if [ "$bg" = 1 ]; then
    BG_MARK=$(wc -l < "$LOG")
    python "$HERE/client.py" --corpus "$corpus" --out "$OUT/$run.json" --tag "$run" --sets "$sets" \
      --ids "$ids" --salt "$salt" --concurrency "$conc" --timeout "${SEND_TIMEOUT:-900}" \
      --url "$url" --metrics-urls "$metrics" $err_flag > "$OUT/client_$run.txt" 2>&1 &
    BG_PIDS="$BG_PIDS $!"
    return 0
  fi
  if ! python "$HERE/client.py" --corpus "$corpus" --out "$OUT/$run.json" --tag "$run" --sets "$sets" \
      --ids "$ids" --salt "$salt" --concurrency "$conc" --timeout "${SEND_TIMEOUT:-900}" \
      --url "$url" --metrics-urls "$metrics" $err_flag; then
    echo "=== $TAG send $name FAILED $(date -u +%T)"
    echo "server_pid=$SERVER_PID vllm_pid=$VLLM_PID vllm2_pid=$VLLM2_PID engine_pids=$(pgrep -f 'VLLM::' | paste -sd,)" > "$OUT/HANG_$run"
    for _ in $(seq 300); do [ -f "$OUT/STACKS_DONE_$run" ] && break; sleep 1; done
    return 1
  fi
  [ "$stats" = 1 ] && python "$HERE/l2_stats.py" dump $L2_ARGS --out "$OUT/l2stats_${run}_after.json"
  return 0
}
restart_step() {
  local downtime=0 kv
  for kv in "$@"; do
    case $kv in
      extra=alt) SERVER_EXTRA=${LMCACHE_SERVER_EXTRA_ALT:?LMCACHE_SERVER_EXTRA_ALT is not set};;
      extra=main) SERVER_EXTRA=${LMCACHE_SERVER_EXTRA:-};;
      downtime=*) downtime=${kv#downtime=};;
      *) echo "restart: unknown argument $kv"; return 1;;
    esac
  done
  local registrations; registrations=$(grep -c "Registered KV cache" "$LOG")
  stop_server
  sleep "$downtime"
  start_server || return 1
  wait_reregistered "$registrations"
}

echo "=== $TAG start $(date -u +%T) layerwise=$LW policy=$POLICY server_extra='${LMCACHE_SERVER_EXTRA:-}' vllm_extra='${VLLM_EXTRA:-}'"
for step in "$@"; do
  read -r verb rest <<< "$step"
  case $verb in
    server) start_server || exit 1;;
    vllm) start_vllm "${rest#model=}" || exit 1;;
    vllm_stop) stop_vllm; echo "=== $TAG vLLM stopped $(date -u +%T)";;
    vllm2) start_vllm2 "${rest#model=}" || exit 1;;
    vllm2_stop) stop_vllm2; echo "=== $TAG second vLLM stopped $(date -u +%T)";;
    send) # shellcheck disable=SC2086
      send $rest || exit 1;;
    settle) settle;;
    restart) settle; echo "=== $TAG restarting the LMCache server (L1 is lost) $rest $(date -u +%T)"
      # shellcheck disable=SC2086
      restart_step $rest || exit 1;;
    reset) python "$HERE/l2_keys.py" $L2_ARGS --set "${L2_SET:-kv_chunks}" truncate; sleep 5
      echo "=== $TAG L2 truncated, restarting the LMCache server $(date -u +%T)"
      restart_server || exit 1;;
    keys) settle; python "$HERE/l2_keys.py" $L2_ARGS --set "${L2_SET:-kv_chunks}" dump --out "$OUT/keys_${TAG}_${rest#name=}.json";;
    delkeys) read -r b a <<< "$rest"
      python "$HERE/l2_keys.py" $L2_ARGS --set "${L2_SET:-kv_chunks}" delete "$OUT/keys_${TAG}_${b#before=}.json" "$OUT/keys_${TAG}_${a#after=}.json" || exit 1;;
    wait_bg) wait_bg;;
    wait_log) # shellcheck disable=SC2086
      wait_log $rest || exit 1;;
    kill9) echo "=== $TAG SIGKILL to the LMCache server (pid $SERVER_PID) $(date -u +%T.%N | cut -c1-12)"
      [ "$SERVER_PID" -gt 0 ] && kill -9 "$SERVER_PID"; SERVER_PID=0;;
    server_up) registrations=$(grep -c "Registered KV cache" "$LOG")
      start_server || exit 1; wait_reregistered "$registrations" || exit 1;;
    freeze_server) secs=${rest#secs=}
      echo "=== $TAG SIGSTOP to the LMCache server (pid $SERVER_PID) for $secs s $(date -u +%T.%N | cut -c1-12)"
      kill -STOP "$SERVER_PID"; sleep "$secs"; kill -CONT "$SERVER_PID"
      echo "=== $TAG SIGCONT $(date -u +%T.%N | cut -c1-12)";;
    host) # shellcheck disable=SC2086
      host_action $rest || exit 1;;
    vllm_check) vllm_check "$rest";;
    vllm_ensure) [ "$VLLM_PID" -gt 0 ] || start_vllm "${rest#model=}" || exit 1;;
    l2seg) # shellcheck disable=SC2086
      python "$HERE/l2_segments.py" $L2_ARGS --set "${L2_SET:-kv_chunks}" --corpus "${CORPUS:-}" \
      --model-url http://localhost:8000 delete $rest || exit 1;;
    sleep) sleep "${rest#secs=}";;
    rxe) rdma statistic show link rxe0/1 > "$OUT/rxe_${TAG}_${rest#name=}.txt" 2>&1;;
    *) echo "unknown step: $step"; exit 1;;
  esac
done
curl -sf http://localhost:8080/metrics > "$OUT/metrics_$TAG.txt"
echo "=== $TAG done $(date -u +%T)"
