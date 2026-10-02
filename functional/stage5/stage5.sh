#!/bin/bash
# Stage 5: E3 failure injection (functional-test-plan.md 5.7 and section 7)
# on the new kv-sink stack: Llama-3.1-8B, LMCache with the Aerospike adapter
# on two paths:
#   pipe   the pipelined path: RDMA adapter against the batch-read kv-sink
#          server (127.0.0.1:$KVSINK_PORT, 3700; rxe0 GID 1), --use-layerwise
#          --pipelined-fetch --pipelined-max-chunks 4 (64 for flt05)
#   plain  the plain path: the adapter without RDMA against Aerospike CE on
#          127.0.0.1:3000 (its set is truncated before every plain session)
# Runs on the host (launch.sh sources functional/newstack/kvsink_bp_env.sh);
# sessions are run_steps.sh sessions in lmc-c. Uses stage3.sh's helpers.
#
# Usage: stage5.sh [section ...]
# Sections (default: precheck flt01 flt05 flt06 flt07 sec7 count7 idle):
#   flt01   T-FLT-01: fault_inject (gap_tail_ratios [0.5]) over the Aerospike
#           adapter, plain (CE) and rdma (kv-sink, RDMA inner; fault_inject has
#           no pipelined sink, so loads are whole objects). The middle chunk
#           of every warm load fails at lookup/prefetch time: leading run
#           served, the rest recomputed, output equal. recompute (the failure
#           is before the forward pass, so D-17 does not apply)
#   flt05   T-FLT-05: SIGKILL the LMCache server right after a retrieve of
#           P-exact-15 (64 chunks) starts. Cases FLT05_CASES (default
#           "pipe:recompute pipe:fail plain:recompute plain:fail
#           plainnolw:recompute"), each <path>:<policy>[:<when>[:<delay>]]:
#           when = retrieve_start or lookup_start (kill during the
#           lookup/prefetch instead), delay = seconds between the log line
#           and the SIGKILL (later in the retrieve); pipe uses cap 64 so the victim
#           is pipelined; plainnolw is the layerwise-off control. FLT05_SUFFIX
#           tags repeats; a wedged engine gets its stacks captured. Under recompute
#           the request recomputes and is equal (a layerwise failure
#           mid-forward may hit D-17, compared against D17-NARROWING.md); under
#           fail it errors cleanly. vLLM should stay up (an engine stop on the
#           progress timeout is section-7 behaviour, recorded); later requests
#           succeed once the server is back
#   flt06   T-FLT-06: P-multi, LMCache restarted between every turn, plain and
#           pipe: each turn hits the previous turns' chunks from L2, equal
#   flt07   T-FLT-07 stand-in (pipe): the kv-sink server frozen 2 s right after
#           a lookup (the real link-down is not possible on this box, see
#           SUMMARY.md). Under fail: a layer that misses its deadline fails
#           mid-forward, which under recompute would hit D-17. FLT07_MODE=link
#           needs the dedicated link (flt07_rdma_down.sh), not set up
#   sec7    plan section 7: freeze the LMCache server 8 s (past the worker's 5 s
#           per-layer wait) right after a lookup, pipe and plain; record how the
#           engine stops. recompute. A stopped engine's KV cache stays mapped
#           by the LMCache server until it reaps the worker (missed heartbeats,
#           worker_reap_timeout_seconds 120), so a new vLLM on the same server
#           starts only after SEC7_REAP_WAIT (default 130) s
#   count7  count the section-7 errors (generation timeout, progress timeout,
#           stale generation) in every Stage 3 (old and new stack), Stage 4 and
#           Stage 5 vLLM log; any outside a deliberate fault is S1
# Environment: FLT05_CASES, SEC7_PATHS / FLT06_PATHS / FLT01_PATHS (default
#   both paths), STOP_GRACE (default 60, D-18), FLT07_MODE (default freeze).
set -u
TREE_HOST=${TREE_HOST:-/root/lmc-work/LMCache}
# shellcheck source=../stage3/stage3.sh
source "$TREE_HOST/functional/stage3/stage3.sh"
S=/root/lmc-work/functional/stage5
W=/work/functional/stage5
FLT07_MODE=${FLT07_MODE:-freeze}
export STOP_GRACE=${STOP_GRACE:-60}
CE_PORT=3000

# Prompt ids of turn <t> (0-4) of the 10 P-multi conversations.
turn_ids() {
  docker exec lmc-c python -c "print(','.join(f'P-multi-{5 * c + $1:02d}' for c in range(10)))"
}
# ce_json: the adapter spec without RDMA, against Aerospike CE.
ce_json() {
  echo "{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$CE_PORT\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"}"
}
# ce_reset <dir> <name>: empty CE's kv_chunks set so a cold send is cold.
ce_reset() {
  KVDIR=""
  local out; out=$(docker exec lmc-c python $H/l2_keys.py --port $CE_PORT --namespace lmcache --set kv_chunks truncate 2>&1)
  sleep 5
  progress "group $1/$2: CE kv_chunks truncated: $(echo "$out" | tail -n 1)"
}
# use_path <pipe|plain> <dir> <tag> [cap]: SERVER_FLAGS, L2_PORT_S and a fresh
# L2 (kv-sink restarted, or CE truncated) for the next session.
use_path() {
  case $1 in
    pipe) SERVER_FLAGS=$(server_flags "${4:-4}" $LLAMA_CHUNK); L2_PORT_S=$KVSINK_PORT; group "$2" "$3";;
    plain) SERVER_FLAGS="--l2-adapter $(ce_json)"; L2_PORT_S=$CE_PORT; ce_reset "$2" "$3";;
    *) echo "unknown path $1"; return 1;;
  esac
}
# engine_line <dir> <check-file-suffix>: vllm_check's verdict, one line.
engine_line() { paste -sd' ' $S/$1/vllm_check_$2.txt 2>/dev/null | cut -c1-260; }

sec_precheck() {
  local busy; busy=$(ss -ltn | grep -E "127.0.0.1:(8000|8001|6555|6556|8080|$KVSINK_PORT) ")
  [ -z "$busy" ] || { echo "ports in use (another session?):"; echo "$busy"; exit 1; }
  ss -ltn | grep -q "127.0.0.1:$CE_PORT " || { echo "aerospike-ce is not listening on $CE_PORT"; exit 1; }
  echo "ports 8000/8001/6555/6556/8080/$KVSINK_PORT free, CE on $CE_PORT; $(wait_idle)"
}

sec_flt01() {
  local path inner lazy
  for path in ${FLT01_PATHS:-plain rdma}; do
    tag=flt01_$path
    if [ "$path" = plain ]; then
      inner=$(ce_json); lazy=""; L2_PORT_S=$CE_PORT; ce_reset flt01 $tag
    else
      inner=$(l2_json 4 $LLAMA_CHUNK); lazy="--no-l1-use-lazy "; L2_PORT_S=$KVSINK_PORT; group flt01 $tag
    fi
    SERVER_FLAGS="$lazy--l2-adapter {\"type\":\"fault_inject\",\"gap_tail_ratios\":[0.5],\"inner\":$inner}"
    local ids4; ids4=$( { ids P-exact 4 4; ids P-ragged 6 16; } | paste -sd,)
    session flt01 $tag recompute server "vllm model=$LLAMA" \
      "send name=cold sets=P-exact,P-ragged ids=$ids4" settle restart \
      "send name=warm sets=P-exact,P-ragged ids=$ids4 stats=1" "vllm_check name=end"
    report flt01 $tag all --no-hit-check=${tag}_warm cold warm
    progress "flt01 $path: fault_inject lines: $(grep -ciE 'fault.?inject|dropp' $S/flt01/lmcache_$tag.log); \
recomputed requests: $(grep -cE 'recompute|invalid block' $S/flt01/vllm_${tag}_*.log); engine: $(engine_line flt01 ${tag}_end)"
  done
}

# flt05_hang_watch <tag>: if the victim is still waiting 30 s after the
# SIGKILL while vLLM answers /health (a wedged engine, D-21), capture the
# EngineCore's Python stacks into flt05/pystacks_<tag>.txt.
flt05_hang_watch() {
  local f=$S/flt05/session_$1.txt out=$S/flt05/pystacks_$1.txt pid cpid
  for _ in $(seq 900); do grep -q 'SIGKILL to the LMCache' "$f" 2>/dev/null && break; sleep 1; done
  sleep 30
  grep -q 'background send .* exited' "$f" && return 0
  curl -sf -m 3 http://127.0.0.1:8000/health >/dev/null || return 0
  pid=$(pgrep -f '^VLLM::EngineCore' | head -n 1)
  [ -n "$pid" ] || return 0
  cpid=$(awk '/^NSpid/ {print $NF}' /proc/$pid/status)
  { echo "victim still waiting 30 s after the SIGKILL, vLLM /health ok; EngineCore host pid $pid (container $cpid) $(date -u +%T)"
    nsenter -t "$pid" -m -p -- "$PY314" -I $H/pystacks.py "$cpid"; } > "$out" 2>&1
  progress "!! $1: vLLM engine wedged after the SIGKILL (stacks in $out)"
}
# flt05_case <path>:<policy>[:<when>]: when = retrieve_start (default) or
# lookup_start (LMCache killed during the lookup/prefetch, before the
# forward pass).
flt05_case() {
  local path policy when delay lw=true p rargs delay_step=()
  IFS=: read -r path policy when delay <<< "$1"
  when=${when:-retrieve_start}
  p=$path
  [ "$path" = plainnolw ] && { lw=false; p=plain; }
  tag=flt05_${path}_$policy
  [ "$when" = retrieve_start ] || tag=${tag}_${when%_start}
  [ -n "$delay" ] && { tag=${tag}_d${delay//./}; delay_step=("sleep secs=$delay"); }
  [ -n "${FLT05_SUFFIX:-}" ] && tag=${tag}_$FLT05_SUFFIX
  use_path $p flt05 $tag 64
  flt05_hang_watch $tag & local hw=$!
  # server_up comes before vllm_ensure: a vLLM started while LMCache is down
  # would wait for a registration; with vLLM alive, server_up waits for its
  # re-registration (the store send primed its heartbeat).
  LW_S=$lw SEND_TIMEOUT=${FLT05_SEND_TIMEOUT:-150} session flt05 $tag $policy server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=P-exact-15,P-exact-10" settle restart \
    "send name=victim sets=P-exact ids=P-exact-15 bg=1 errors=1" "wait_log what=$when timeout=120" \
    "${delay_step[@]}" kill9 wait_bg "vllm_check name=afterkill" server_up "vllm_ensure model=$LLAMA" \
    "send name=after sets=P-exact ids=P-exact-10 errors=1" "vllm_check name=end"
  kill $hw 2>/dev/null
  rargs=("--no-hit-check=${tag}_victim")
  [ $p = pipe ] && rargs+=("--outcomes=${tag}_after=pipelined,not_deferred")
  [ $policy = fail ] && rargs+=("--allow-error=${tag}_victim")
  report flt05 $tag all "${rargs[@]}" store victim after
  progress "flt05 $path $policy: victim $(docker exec lmc-c python -c "
import json; r = json.load(open('$W/flt05/${tag}_victim.json'))['results']['P-exact-15']
print('ERROR ' + r['error'][:120] if 'error' in r else 'completed in %.1f s' % r['latency_s'])" 2>&1 | tail -n 1); \
victim retrieve outcome: $(grep -oE 'pipelined_outcome=[a-z_]+' $S/flt05/lmcache_$tag.log | sed -n 2p); \
vLLM after the kill: $(engine_line flt05 ${tag}_afterkill); end: $(engine_line flt05 ${tag}_end)"
}
sec_flt05() {
  local c
  for c in ${FLT05_CASES:-pipe:recompute pipe:fail plain:recompute plain:fail plainnolw:recompute}; do
    flt05_case "$c"
  done
}

sec_flt06() {
  local path
  for path in ${FLT06_PATHS:-plain pipe}; do
    tag=flt06_$path
    use_path $path flt06 $tag
    local steps=(server "vllm model=$LLAMA") t sends=() rargs=()
    for t in 0 1 2 3 4; do
      steps+=("send name=turn$t sets=P-multi ids=$(turn_ids $t) stats=1")
      [ $t -lt 4 ] && steps+=(restart)
      sends+=("turn$t")
      [ $path = pipe ] && rargs+=("--outcomes=${tag}_turn$t=pipelined,not_deferred")
    done
    # Turns 1-3 hit 1, 2 and 4 chunks (within the cap): pipelined from L2.
    [ $path = pipe ] && rargs+=("--require=${tag}_turn1=pipelined" "--require=${tag}_turn2=pipelined" "--require=${tag}_turn3=pipelined")
    session flt06 $tag recompute "${steps[@]}" "vllm_check name=end"
    report flt06 $tag all "${rargs[@]}" "${sends[@]}"
  done
}

sec_flt07() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  L2_PORT_S=$KVSINK_PORT
  local action=freeze_kvsink
  if [ "$FLT07_MODE" = link ]; then
    bash $TREE_HOST/functional/stage5/flt07_rdma_down.sh check | grep -q '^ready' \
      || { progress "flt07: FLT07_MODE=link but the dedicated link is not set up"; return 1; }
    action=rdma_down
    # The link mode also needs LMCache on the far side (10.250.0.2, rxefa0).
    SERVER_FLAGS=${FLT07_SERVER_FLAGS:?set FLT07_SERVER_FLAGS for the dedicated link}
  fi
  tag=flt07_$FLT07_MODE
  group flt07 $tag
  # After the stall, the abandoned fetch's window is quarantined for
  # fetch_timeout_seconds (30 s); after/after2 run once it is leased again.
  session flt07 $tag fail server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=$(ids P-exact 4 4)" settle restart \
    "host action=$action secs=2 on=lookup_end" "send name=stall sets=P-exact ids=P-exact-10 errors=1" \
    "sleep secs=4" "vllm_check name=stall" "vllm_ensure model=$LLAMA" "sleep secs=32" \
    "send name=after sets=P-exact ids=P-exact-11" "send name=after2 sets=P-exact ids=P-exact-12" \
    "vllm_check name=end"
  report flt07 $tag all "--allow-error=${tag}_stall" "--no-hit-check=${tag}_stall" \
    "--outcomes=${tag}_stall=fell_back,failed,pipelined,refused" \
    "--outcomes=${tag}_after=pipelined" "--require=${tag}_after=pipelined" \
    "--outcomes=${tag}_after2=pipelined" "--require=${tag}_after2=pipelined" store stall after after2
  progress "flt07 ($FLT07_MODE): fault $(cat $S/flt07/HOSTFAULT_* 2>/dev/null | paste -sd'|'); \
stall outcome: $(grep -oE 'pipelined_outcome=[a-z_]+' $S/flt07/lmcache_$tag.log | sed -n 5p); \
layer timeouts: $(grep -c 'will never arrive' $S/flt07/lmcache_$tag.log); quarantine lines: $(grep -ciE 'quarantin' $S/flt07/lmcache_$tag.log); \
engine at stall: $(engine_line flt07 ${tag}_stall)"
}

sec_sec7() {
  local path
  for path in ${SEC7_PATHS:-pipe plain}; do
    tag=sec7_$path
    use_path $path sec7 $tag
    session sec7 $tag recompute server "vllm model=$LLAMA" \
      "send name=store sets=P-exact ids=P-exact-10,P-exact-11" settle restart \
      "send name=victim sets=P-exact ids=P-exact-10 bg=1 errors=1" "wait_log what=lookup_end timeout=120" \
      "freeze_server secs=8" wait_bg "vllm_check name=stalled" "sleep secs=${SEC7_REAP_WAIT:-130}" \
      "vllm_ensure model=$LLAMA" \
      "send name=after sets=P-exact ids=P-exact-11 errors=1" "vllm_check name=end"
    local rargs=("--allow-error=${tag}_victim" "--no-hit-check=${tag}_victim")
    [ $path = pipe ] && rargs+=("--outcomes=${tag}_after=pipelined,not_deferred")
    report sec7 $tag all "${rargs[@]}" store victim after
    progress "sec7 $path: $(engine_line sec7 ${tag}_stalled); section-7 lines in the vLLM log: \
$(grep -hoE 'LayerProgress[A-Za-z]+Error' $S/sec7/vllm_${tag}_*.log | sort | uniq -c | sed 's/^ *//' | paste -sd' ')"
  done
}

sec_count7() {
  local f n total=0 outside=0 pat='LayerProgress(RetrieveGenerationTimeout|RetrieveProgressTimeout|StaleGeneration)Error'
  for f in /root/lmc-work/functional/stage3/*/vllm_*.log /root/lmc-work/functional/stage3-newstack/*/vllm_*.log \
      /root/lmc-work/functional/stage4/*/vllm*_*.log /root/lmc-work/functional/stage5/*/vllm*_*.log; do
    [ -f "$f" ] || continue
    n=$(grep -cE "$pat" "$f")
    [ "$n" -gt 0 ] || continue
    total=$((total + n))
    case $f in
      */stage5/sec7/*) echo "deliberate stall (sec7): $f: $n $(grep -hoE "$pat" "$f" | sort | uniq -c | paste -sd' ')";;
      */stage5/flt05/*|*/stage5/flt07/*|*/stage3*/pipe0[56]/*|*/stage3/d17/*)
        echo "deliberate fault: $f: $n $(grep -hoE "$pat" "$f" | sort | uniq -c | paste -sd' ')";;
      *) outside=$((outside + n)); echo "OUTSIDE a deliberate fault: $f: $n";;
    esac
  done
  progress "count7: section-7 error lines: $total, outside deliberate faults: $outside (any is S1)"
}

sec_dry() {
  local rc=0
  bash -n $TREE_HOST/functional/stage5/stage5.sh && bash -n $TREE_HOST/functional/harness/run_steps.sh || rc=1
  docker exec lmc-c python -c "
import json, sys
from lmcache.v1.distributed.l2_adapters.fault_inject_l2_adapter import FaultInjectL2AdapterConfig
for s in sys.argv[1:]:
    c = FaultInjectL2AdapterConfig.from_dict(json.loads(s))
    print('fault_inject config ok: gap_tail_ratios', c.gap_tail_ratios, 'inner', type(c.inner_config).__name__)" \
    "{\"type\":\"fault_inject\",\"gap_tail_ratios\":[0.5],\"inner\":$(l2_json 4 $LLAMA_CHUNK)}" \
    "{\"type\":\"fault_inject\",\"gap_tail_ratios\":[0.5],\"inner\":$(ce_json)}" || rc=1
  local t; for t in 0 1 2 3 4; do echo "P-multi turn $t: $(turn_ids $t)"; done
  echo "flt01 prompts: $( { ids P-exact 4 4; ids P-ragged 6 16; } | paste -sd,)"
  echo "KVSINK_PORT=$KVSINK_PORT STOP_GRACE=$STOP_GRACE"
  return $rc
}

mkdir -p $S
# shellcheck disable=SC2048
for sec in ${*:-precheck flt01 flt05 flt06 flt07 sec7 count7 idle}; do
  progress "section $sec started"
  sec_$sec
  progress "section $sec finished"
done
echo "##### STAGE 5 DONE $(date -u +%T) $(wait_idle)"
