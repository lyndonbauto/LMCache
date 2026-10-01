#!/bin/bash
# Host-side helpers for the Stage 3 and Stage 5 drivers. Source it from a
# driver that runs on the host; it needs TREE_HOST and TREE_CTR (host and
# lmc-c paths of the LMCache tree the sessions run from) and PY314.
#
#   kvsink_restart <log-dir>   stop the kv-sink server, start it, warm it
#                              (kvsink_smoke.sh; server issue 5). A restart
#                              also empties it (storage-engine memory) and
#                              frees the regions dead clients leaked (issue 9)
#   kvsink_host_pid            host PID of the kv-sink asd (the one in aero-kvsink)
#   watch_host <dir>           serve run_steps.sh's requests in <dir> until
#                              killed: HANG_* (capture Python stacks, then
#                              STACKS_DONE_*) and HOSTREQ_* (run the action,
#                              then HOSTDONE_* with a one-line answer)
#
# Actions (the words of a run_steps.sh `host` step):
#   action=freeze_kvsink secs=<s>
#       SIGSTOP the kv-sink server for s seconds, then SIGCONT.
#   action=restart_kvsink      kvsink_restart (registrations on it are lost)
#   action=rdma_down secs=<s>  functional/stage5/flt07_rdma_down.sh down <s>
#                              (refuses unless the topology it needs exists)
# Any action with on=<w> is armed instead: the request is answered at once,
# and the action runs at the next matching line of the session's LMCache log
# (w = lookup_end, retrieve_start), read with tail -F on the host so no
# container round trip delays it. Its answer goes to HOSTFAULT_<run>.txt.
set -u
KVSINK_CTR=aero-kvsink

kvsink_restart() {
  local dir=$1
  mkdir -p "$dir"
  KVSINK_CONF=$TREE_HOST/functional/configs/aerospike-kvsink.conf \
    KVSINK_LOG=$dir/asd-kvsink.log bash "$TREE_HOST/functional/harness/kvsink_server.sh" stop
  KVSINK_CONF=$TREE_HOST/functional/configs/aerospike-kvsink.conf \
    KVSINK_LOG=$dir/asd-kvsink.log bash "$TREE_HOST/functional/harness/kvsink_server.sh" start || return 1
  # The smoke's pytest process has crashed (abort/segfault) in its second test
  # after the first fell back on the cold server; one retry finishes the warm-up.
  local passed attempt
  for attempt in 1 2; do
    docker exec -e LMCACHE_DIR="$TREE_HOST" "$KVSINK_CTR" \
      bash "$TREE_HOST/functional/harness/kvsink_smoke.sh" "$dir/warm$attempt" > "$dir/warm$attempt.txt" 2>&1
    passed=$(grep -oE '[0-9]+ passed' "$dir/warm$attempt/pipelined_it.txt" | grep -oE '[0-9]+')
    grep -q "Fatal Python error" "$dir/warm$attempt/pipelined_it.txt" \
      && echo "warm-up attempt $attempt: pytest crashed ($(grep -m1 'Fatal Python error' "$dir/warm$attempt/pipelined_it.txt"))"
    [ "${passed:-0}" -ge 2 ] && break
  done
  echo "kv-sink restarted and warmed (pipelined smoke attempt $attempt: ${passed:-0} of 3 passed; 2 on a cold server is expected)"
}

kvsink_host_pid() {
  local pids; pids=$(docker top "$KVSINK_CTR" -eo pid,comm | awk '$2=="asd"{print $1}')
  [ "$(echo "$pids" | grep -c .)" = 1 ] || { echo "expected one asd in $KVSINK_CTR, got: $pids" >&2; return 1; }
  echo "$pids"
}

_freeze_kvsink() {
  local secs=$1 pid; pid=$(kvsink_host_pid) || return 1
  kill -STOP "$pid"
  local t0; t0=$(date -u +%T.%N | cut -c1-12)
  sleep "$secs"
  kill -CONT "$pid"
  echo "kv-sink pid $pid stopped at $t0 for $secs s, continued $(date -u +%T.%N | cut -c1-12)"
}

# _run_action <dir> <run> <action> <secs>: run one action now; print its answer.
_run_action() {
  local dir=$1 run=$2 action=$3 secs=$4
  case $action in
    freeze_kvsink) _freeze_kvsink "$secs" 2>&1;;
    restart_kvsink) kvsink_restart "$dir/kvsink_$run" 2>&1 | tail -n 1;;
    rdma_down) bash "$TREE_HOST/functional/stage5/flt07_rdma_down.sh" down "$secs" 2>&1 | tail -n 1;;
    *) echo "unknown action '$action'";;
  esac
}

_host_request() {
  local dir=$1 req=$2 run log words action="" secs=0 on="" w pattern answer
  run=${req##*/HOSTREQ_}
  log=$dir/lmcache_${run%_*}.log
  words=$(cat "$req")
  for w in $words; do
    case $w in action=*) action=${w#action=};; secs=*) secs=${w#secs=};; on=*) on=${w#on=};; esac
  done
  if [ -n "$on" ]; then
    case $on in
      lookup_end) pattern="MP lookup/prefetch end:";;
      retrieve_start) pattern="MP retrieve start:";;
      *) pattern="";;
    esac
    if [ -z "$pattern" ]; then
      answer="unknown on=$on"
    else
      ( if timeout 600 tail -n 0 -F "$log" 2>/dev/null | grep -m 1 -F "$pattern" > /dev/null; then
          _run_action "$dir" "$run" "$action" "$secs" > "$dir/HOSTFAULT_$run.txt"
        else
          echo "trigger '$pattern' never seen in 600 s; $action not run" > "$dir/HOSTFAULT_$run.txt"
        fi ) &
      answer="armed: $action secs=$secs at the next '$pattern'"
    fi
  else
    answer=$(_run_action "$dir" "$run" "$action" "$secs")
  fi
  echo "$answer" > "$dir/HOSTDONE_$run"
  echo "$(date -u +%T) host request $run ($words): $answer" >> "$dir/host_actions.log"
}

watch_host() {
  local dir=$1 root hang run pids pid req
  root=$(docker inspect -f '{{.State.Pid}}' lmc-c)
  while :; do
    for hang in "$dir"/HANG_*; do
      [ -f "$hang" ] || continue
      run=${hang##*/HANG_}
      [ -f "$dir/STACKS_DONE_$run" ] && continue
      pids=$(tr ' ,' '\n\n' < "$hang" | grep -oE '[0-9]+$' | sort -u)
      for pid in $pids; do
        nsenter -t "$root" -m -p -- "$PY314" -I "$TREE_CTR/functional/harness/pystacks.py" "$pid" \
          > "$dir/pystacks_${run}_$pid.txt" 2>&1
      done
      touch "$dir/STACKS_DONE_$run"
    done
    for req in "$dir"/HOSTREQ_*; do
      [ -f "$req" ] || continue
      case $req in *.tmp) continue;; esac
      run=${req##*/HOSTREQ_}
      [ -f "$dir/HOSTDONE_$run" ] && continue
      _host_request "$dir" "$req"
    done
    sleep 0.05
  done
}
