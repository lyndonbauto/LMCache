#!/bin/bash
# perf_session.sh <out_dir> <tag> <mode> <step>...: one perf session in lmc-c
# (run by functional/perf/perf.sh on the host). Starts vLLM (and, for the
# cached modes, the LMCache server), sends one warm-up request, then runs the
# steps. Every process is stopped on exit.
# Modes:
#   nocache  vLLM without the LMCache connector
#   aon      LMCache connector, layerwise off (server and connector), no
#            pipelined fetch; L2 = kv-sink server on 127.0.0.1:3700, plain reads
#   lw       LMCache connector, layerwise on (server --use-layerwise and
#            lmcache.mp.use_layerwise), --pipelined-fetch over RDMA (rxe0)
# Steps (one argument each; words after the verb are key=value):
#   store len=<L> ids=<a-b> conc=<c>   send prompts once (not a measurement),
#                                      then wait until L2 writes stop
#   point len=<L> c=<c> [n=<n>] [pre=<P>]
#                                      (pre: partial hit, the stored prompt of
#                                      length P plus L new tokens; name
#                                      P<P>_L<L>_c<c>)
#                                      one measurement: n = max(4, c) prompts
#                                      0..n-1, c in flight. In aon/lw the
#                                      LMCache server is restarted first (L1
#                                      empty, every hit from L2), vLLM must
#                                      re-register, and one short unrelated
#                                      prompt primes it (excluded)
#   restart                            restart the LMCache server only
#   sleep secs=<s>
# Environment: LMC_EXTRA (LMCache server flags after the common ones), L1_GB
# (default 100), VLLM_UTIL (0.9), MAX_LEN (131072), STOP_GRACE (60, D-18),
# L2_PORT (3700), SEND_TIMEOUT (3600), LW_WAIT_TIMEOUT (unset: the connector's
# lmcache.mp.layerwise_wait_timeout_seconds default, 5 s; a timeout stops
# vLLM's engine, section 7 / D-24), STOP_ON_ENGINE_STOP (0; 1: if vLLM is
# dead after a point, write engine_stopped_<tag>.txt and skip the remaining
# steps).
# Before each point, a dead vLLM is restarted (with a fresh LMCache server).
set -u
OUT=$1; TAG=$2; MODE=$3; shift 3
HERE=$(cd "$(dirname "$0")" && pwd)
HARNESS=$(cd "$HERE/../harness" && pwd)
mkdir -p "$OUT"
# shellcheck source=../harness/loopback_env.sh
source "$HARNESS/loopback_env.sh"
export HF_HOME=${HF_HOME:-/work/hf} HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
export LMCACHE_LOG_LEVEL=${LMCACHE_LOG_LEVEL:-DEBUG}
unset VLLM_BATCH_INVARIANT
MODEL=meta-llama/Llama-3.1-8B-Instruct
L1_GB=${L1_GB:-100}
STOP_GRACE=${STOP_GRACE:-60}
L2_PORT=${L2_PORT:-3700}
LOG=$OUT/lmcache_$TAG.log
VLOG=$OUT/vllm_$TAG.log
SERVER_PID=0; VLLM_PID=0; WARM_SEQ=0
case $MODE in
  nocache) LW=false; CONNECTOR=0;;
  aon) LW=false; CONNECTOR=1;;
  lw) LW=true; CONNECTOR=1;;
  *) echo "unknown mode $MODE"; exit 2;;
esac
if [ "$LW" = true ]; then SFLAG=--use-layerwise; else SFLAG=--no-use-layerwise; fi
say() { echo "=== $TAG $* $(date -u +%T)"; }
listen_check() { bash "$HARNESS/listen_check.sh" "$TAG $1" "$OUT/listeners_$TAG.txt"; }

start_server() {
  [ "$CONNECTOR" = 1 ] || return 0
  echo "=== $TAG server flags: $SFLAG ${LMC_EXTRA:-}" >> "$LOG"
  # shellcheck disable=SC2086
  lmcache server --port 6555 --http-host 127.0.0.1 --http-port 8080 --l1-size-gb "$L1_GB" \
    --eviction-policy LRU --chunk-size 256 $SFLAG ${LMC_EXTRA:-} >> "$LOG" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 300); do
    curl -sf http://127.0.0.1:8080/metrics >/dev/null && { say "LMCache up (pid $SERVER_PID)"; listen_check lmcache; return; }
    kill -0 "$SERVER_PID" 2>/dev/null || { say "LMCache server exited"; tail -n 20 "$LOG"; return 1; }
    sleep 1
  done
  say "LMCache server did not come up"; return 1
}
# stop_server: SIGTERM, kill -9 after STOP_GRACE s (a clean exit takes ~14 s,
# D-18); logs the exit status (143 clean, 137 needed kill -9).
stop_server() {
  [ "$SERVER_PID" -gt 0 ] || return 0
  local how=TERM rc
  kill "$SERVER_PID" 2>/dev/null
  for _ in $(seq $((STOP_GRACE * 10))); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.1; done
  kill -0 "$SERVER_PID" 2>/dev/null && { how="kill -9"; kill -9 "$SERVER_PID" 2>/dev/null; }
  wait "$SERVER_PID" 2>/dev/null; rc=$?
  say "LMCache server pid $SERVER_PID stopped ($how) exit=$rc"
  SERVER_PID=0
}
start_vllm() {
  local kv=() wait_cfg=""
  [ -n "${LW_WAIT_TIMEOUT:-}" ] && wait_cfg=",\"lmcache.mp.layerwise_wait_timeout_seconds\":$LW_WAIT_TIMEOUT"
  if [ "$CONNECTOR" = 1 ]; then
    kv=(--kv-transfer-config "{\"kv_connector\":\"LMCacheMPConnector\",\"kv_role\":\"kv_both\",\
\"kv_connector_module_path\":\"lmcache.integration.vllm.lmcache_mp_connector\",\
\"kv_load_failure_policy\":\"fail\",\"kv_connector_extra_config\":{\
\"lmcache.mp.host\":\"tcp://localhost\",\"lmcache.mp.port\":6555,\"lmcache.mp.use_layerwise\":$LW$wait_cfg}}")
  fi
  vllm serve "$MODEL" --host 127.0.0.1 --port 8000 --seed 0 --no-enable-prefix-caching \
    --async-scheduling --max-model-len "${MAX_LEN:-131072}" --gpu-memory-utilization "${VLLM_UTIL:-0.9}" \
    "${kv[@]}" >> "$VLOG" 2>&1 &
  VLLM_PID=$!
  for _ in $(seq 1800); do
    curl -sf http://127.0.0.1:8000/health >/dev/null && break
    kill -0 "$VLLM_PID" 2>/dev/null || { say "vLLM exited during startup"; tail -n 30 "$VLOG"; return 1; }
    sleep 1
  done
  curl -sf http://127.0.0.1:8000/health >/dev/null || { say "vLLM did not come up"; return 1; }
  say "vLLM up (pid $VLLM_PID) $(grep -m1 -oE 'GPU KV cache size: [0-9,]+ tokens' "$VLOG"); \
cudagraph: $(grep -m1 -oE 'cudagraph_mode[^,]*' "$VLOG")"
  listen_check vllm
}
stop_vllm() {
  [ "$VLLM_PID" -gt 0 ] || return 0
  kill "$VLLM_PID" 2>/dev/null
  for _ in $(seq 60); do kill -0 "$VLLM_PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$VLLM_PID" 2>/dev/null
  pkill -9 -f "VLLM::" 2>/dev/null
  VLLM_PID=0
  sleep 10
}
cleanup() { stop_vllm; stop_server; }
trap cleanup EXIT

registrations() { grep -c "Registered KV cache" "$LOG" 2>/dev/null; }
# warm <why> [tokens]: one unrelated prompt (excluded from metrics): short
# text, or a fixed prompt of <tokens> token IDs.
warm() {
  WARM_SEQ=$((WARM_SEQ + 1))
  python "$HERE/perf_client.py" --warmup "Warm-up request $WARM_SEQ of session $TAG ($1). Say hello." \
    --warmup-tokens "${2:-0}" --tag "${TAG}_warm$WARM_SEQ" --out "$OUT/warm_${TAG}_$WARM_SEQ.json"
}
# restart_server: vLLM re-registers with a restarted LMCache server when its
# worker's heartbeat PING finds itself unregistered (every 10 s); lookups
# before that fail with "No GPU context found". The worker heartbeat starts
# on its first store or retrieve, which a prompt under one chunk never
# causes with layerwise on, so the warm-up after the vLLM start is a fixed
# 600-token prompt (two chunks). Short primes are sent while waiting.
restart_server() {
  local before; before=$(registrations)
  stop_server
  start_server || return 1
  local t0 primes=0; t0=$(date +%s)
  while [ "$primes" -lt 6 ] && [ "$(registrations)" -le "$before" ]; do
    primes=$((primes + 1))
    warm "prime $primes after LMCache restart" || return 1
    for _ in $(seq 20); do [ "$(registrations)" -gt "$before" ] && break; sleep 1; done
  done
  if [ "$(registrations)" -gt "$before" ]; then
    say "vLLM re-registered $(( $(date +%s) - t0 )) s after the restart (short primes sent: $primes)"
  else
    say "error: vLLM did not re-register within 6 primes"; return 1
  fi
  # The fixed 600-token prompt is in L2 (stored by the first session's
  # warm-up), so this exercises the L2 read and GPU retrieve path once
  # before the measured requests.
  warm "retrieve warm-up after re-registration" 600 || return 1
  sleep 2
}
# ensure_vllm: if vLLM died (e.g. a layerwise wait timeout stopped its engine),
# record it, stop it, restart the LMCache server (a fresh server holds no dead
# engine's memory, so the D-23 reap does not apply) and start vLLM again.
ensure_vllm() {
  curl -sf http://127.0.0.1:8000/health >/dev/null && return 0
  say "vLLM is dead before the next step; restarting it (engine errors: \
$(grep -cE 'EngineDeadError|LayerProgress[A-Za-z]*TimeoutError' "$VLOG"))"
  echo "vllm_restart $(date -u +%T)" >> "$OUT/vllm_restarts_$TAG.txt"
  [ "$VLLM_PID" -gt 0 ] && kill -9 "$VLLM_PID" 2>/dev/null
  pkill -9 -f "VLLM::" 2>/dev/null
  VLLM_PID=0; sleep 10
  stop_server
  start_server || return 1
  start_vllm || return 1
  warm "after vLLM restart" 600
}
settle() { python "$HARNESS/wait_l2_settle.py" --port "$L2_PORT" --namespace lmcache || say "warning: L2 writes had not settled"; }
metrics_urls() {
  if [ "$CONNECTOR" = 1 ]; then echo http://127.0.0.1:8000/metrics,http://127.0.0.1:8080/metrics
  else echo http://127.0.0.1:8000/metrics; fi
}
# send <name> <len> <ids> <conc> [prefix-len]
send() {
  local name=$1 len=$2 ids=$3 conc=$4 pre=${5:-0} mark=0
  [ -f "$LOG" ] && mark=$(wc -l < "$LOG")
  echo "$mark" > "$OUT/logmark_${TAG}_$name.txt"
  python "$HERE/perf_client.py" --length "$len" --prefix-length "$pre" --ids "$ids" --concurrency "$conc" \
    --timeout "${SEND_TIMEOUT:-3600}" --metrics-urls "$(metrics_urls)" --tag "${TAG}_$name" \
    --out "$OUT/${TAG}_$name.json"
  local rc=$?
  if [ -f "$LOG" ]; then
    tail -n +"$((mark + 1))" "$LOG" | grep -oE 'pipelined_outcome=[A-Za-z_]+' | sort | uniq -c \
      | awk '{printf "%s%s:%s", (NR>1?",":""), substr($2,19), $1}' > "$OUT/outcomes_${TAG}_$name.txt"
    tail -n +"$((mark + 1))" "$LOG" | grep -cE 'Traceback|ERROR' > "$OUT/lmcerrors_${TAG}_$name.txt"
    say "outcomes $name: $(cat "$OUT/outcomes_${TAG}_$name.txt"); LMCache error lines: $(cat "$OUT/lmcerrors_${TAG}_$name.txt")"
  fi
  return $rc
}

say "start mode=$MODE layerwise=$LW L1=${L1_GB}GB extra='${LMC_EXTRA:-}'"
start_server || exit 1
start_vllm || exit 1
warm "after vLLM start" 600 || exit 1
for step in "$@"; do
  read -r verb rest <<< "$step"
  len=8192; ids=0-31; conc=8; c=1; n=""; pre=0
  for kv in $rest; do
    case $kv in len=*) len=${kv#len=};; ids=*) ids=${kv#ids=};; conc=*) conc=${kv#conc=};;
      pre=*) pre=${kv#pre=};;
      c=*) c=${kv#c=};; n=*) n=${kv#n=};; secs=*) secs=${kv#secs=};; esac
  done
  case $verb in
    store) say "store len=$len ids=$ids conc=$conc"
      send "store_${len}_$ids" "$len" "$ids" "$conc"; settle
      python "$HARNESS/as_info.py" "$L2_PORT" namespace/lmcache > "$OUT/l2stat_${TAG}_store_$len.txt"
      say "store done: $(tr ';' '\n' < "$OUT/l2stat_${TAG}_store_$len.txt" | grep -E '^(objects|data_used_bytes)=' | paste -sd' ')";;
    point) [ -n "$n" ] || n=$(( c > 4 ? c : 4 ))
      ensure_vllm || exit 1
      [ "$CONNECTOR" = 1 ] && { restart_server || exit 1; }
      pname="L${len}_c${c}"; [ "$pre" -gt 0 ] && pname="P${pre}_$pname"
      say "point $pname len=$len pre=$pre c=$c n=$n"
      send "$pname" "$len" "0-$((n - 1))" "$c" "$pre" || say "point $pname had errors"
      if [ "${STOP_ON_ENGINE_STOP:-0}" = 1 ]; then
        sleep 5
        if ! curl -sf http://127.0.0.1:8000/health >/dev/null; then
          echo "c=$c len=$len" > "$OUT/engine_stopped_$TAG.txt"
          say "engine stopped at c=$c; skipping the remaining steps"
          break
        fi
      fi;;
    restart) restart_server || exit 1;;
    sleep) sleep "$secs";;
    *) say "unknown step $step"; exit 1;;
  esac
done
[ "$CONNECTOR" = 1 ] && curl -sf http://127.0.0.1:8080/metrics > "$OUT/metrics_lmcache_$TAG.txt"
curl -sf http://127.0.0.1:8000/metrics > "$OUT/metrics_vllm_$TAG.txt"
say "done"
