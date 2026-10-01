#!/bin/bash
# Stage 5: E3 failure injection (functional-test-plan.md 5.7 and section 7)
# on the Stage 3 setup: Llama-3.1-8B, LMCache with the Aerospike RDMA adapter
# against the kv-sink server (127.0.0.1:3100, rxe0 GID 1), --use-layerwise
# --pipelined-fetch --pipelined-max-chunks 4. Runs on the host; sessions are
# run_steps.sh sessions in lmc-c. Uses stage3.sh's helpers.
#
# Usage: stage5.sh [section ...]
# Sections (default: precheck flt01 flt05 flt06 flt07 sec7 count7 idle):
#   flt01   T-FLT-01: fault_inject (gap_tail_ratios [0.5]) over the Aerospike
#           adapter; the middle chunk of every warm load fails: leading run
#           served, the rest recomputed, output equal
#   flt05   T-FLT-05: SIGKILL the LMCache server right after a retrieve starts,
#           under kv_load_failure_policy recompute (request recomputes, equal)
#           and fail (request errors cleanly); vLLM must stay up, and later
#           requests succeed once the server is back
#   flt06   T-FLT-06: P-multi, LMCache restarted between every turn: each turn
#           hits the previous turns' chunks from L2, output equal
#   flt07   T-FLT-07: RDMA path down 2 s during a fetch. FLT07_MODE=link uses
#           flt07_rdma_down.sh (needs the dedicated link, see its header);
#           the default stand-in freezes the kv-sink server for 2 s instead
#   sec7    plan section 7: freeze the LMCache server 8 s (past the worker's 5 s
#           per-layer wait) right after a lookup; the engine must stop with
#           LayerProgressRetrieveGenerationTimeoutError and nothing else
#   count7  count that error in every Stage 3 and Stage 5 vLLM log; any outside
#           sec7 is S1 (plan section 7)
# All under recompute (plan section 7) except flt05's fail half.
set -u
TREE_HOST=${TREE_HOST:-/root/lmc-work/LMCache}
# shellcheck source=../stage3/stage3.sh
source "$TREE_HOST/functional/stage3/stage3.sh"
S=/root/lmc-work/functional/stage5
W=/work/functional/stage5
FLT07_MODE=${FLT07_MODE:-freeze}

# Prompt ids of turn <t> (0-4) of the 10 P-multi conversations.
turn_ids() {
  docker exec lmc-c python -c "print(','.join(f'P-multi-{5 * c + $1:02d}' for c in range(10)))"
}

sec_flt01() {
  local inner; inner=$(l2_json 4 $LLAMA_CHUNK)
  SERVER_FLAGS="--l2-adapter {\"type\":\"fault_inject\",\"gap_tail_ratios\":[0.5],\"inner\":$inner}"
  tag=flt01
  group flt01 $tag
  local ids4; ids4=$( { ids P-exact 4 4; ids P-ragged 6 16; } | paste -sd,)
  session flt01 $tag recompute server "vllm model=$LLAMA" \
    "send name=cold sets=P-exact,P-ragged ids=$ids4" settle restart \
    "send name=warm sets=P-exact,P-ragged ids=$ids4 stats=1"
  report flt01 $tag all --no-hit-check=${tag}_warm cold warm
  progress "flt01: fault_inject lines: $(grep -ciE 'fault.?inject|dropp' $S/flt01/lmcache_$tag.log); \
LMCache L2 hit tokens vs vLLM ext hit per request in report_${tag}_all.md (hit columns not checked)"
}

sec_flt05() {
  local policy rargs
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  for policy in recompute fail; do
    tag=flt05_$policy
    group flt05 $tag
    SEND_TIMEOUT=300 session flt05 $tag $policy server "vllm model=$LLAMA" \
      "send name=store sets=P-exact ids=P-exact-15,P-exact-10" settle restart \
      "send name=victim sets=P-exact ids=P-exact-15 bg=1 errors=1" "wait_log what=retrieve_start timeout=120" \
      kill9 wait_bg "vllm_check name=afterkill" "vllm_ensure model=$LLAMA" server_up \
      "send name=after sets=P-exact ids=P-exact-10 errors=1" "vllm_check name=end"
    rargs=("--no-hit-check=${tag}_victim" "--outcomes=${tag}_after=pipelined,not_deferred")
    [ $policy = fail ] && rargs+=("--allow-error=${tag}_victim")
    report flt05 $tag all "${rargs[@]}" store victim after
    progress "flt05 $policy: victim $(docker exec lmc-c python -c "
import json; r = json.load(open('$W/flt05/${tag}_victim.json'))['results']['P-exact-15']
print('ERROR ' + r['error'][:120] if 'error' in r else 'completed in %.1f s' % r['latency_s'])"); \
vLLM after the kill: $(paste -sd' ' $S/flt05/vllm_check_${tag}_afterkill.txt | cut -c1-160)"
  done
}

sec_flt06() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  tag=flt06
  group flt06 $tag
  local steps=(server "vllm model=$LLAMA") t sends=() rargs=()
  for t in 0 1 2 3 4; do
    steps+=("send name=turn$t sets=P-multi ids=$(turn_ids $t) stats=1")
    [ $t -lt 4 ] && steps+=(restart)
    sends+=("turn$t")
    rargs+=("--outcomes=${tag}_turn$t=pipelined,not_deferred")
  done
  # Turns 1-3 hit 1, 2 and 4 chunks (within the cap): pipelined from L2.
  rargs+=("--require=${tag}_turn1=pipelined" "--require=${tag}_turn2=pipelined" "--require=${tag}_turn3=pipelined")
  session flt06 $tag recompute "${steps[@]}"
  report flt06 $tag all "${rargs[@]}" "${sends[@]}"
}

sec_flt07() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
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
  session flt07 $tag recompute server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=$(ids P-exact 4 4)" settle restart \
    "host action=$action secs=2 on=lookup_end" "send name=stall sets=P-exact ids=P-exact-10 errors=1" \
    "sleep secs=4" "vllm_check name=stall" "vllm_ensure model=$LLAMA" \
    "send name=after sets=P-exact ids=P-exact-11" "send name=after2 sets=P-exact ids=P-exact-12" \
    "vllm_check name=end"
  report flt07 $tag all "--allow-error=${tag}_stall" "--outcomes=${tag}_stall=fell_back,failed,pipelined" \
    "--outcomes=${tag}_after=pipelined" "--require=${tag}_after=pipelined" \
    "--outcomes=${tag}_after2=pipelined" "--require=${tag}_after2=pipelined" store stall after after2
  progress "flt07 ($FLT07_MODE): fault $(cat $S/flt07/HOSTFAULT_* 2>/dev/null | paste -sd'|'); \
rxe retry_exceeded/link_downed: $(docker exec lmc-c rdma statistic show link rxe0/1 | grep -oE '(retry_exceeded_err|link_downed) [0-9]+' | paste -sd' ')"
}

sec_sec7() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  tag=sec7
  group sec7 $tag
  session sec7 $tag recompute server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=P-exact-10,P-exact-11" settle restart \
    "send name=victim sets=P-exact ids=P-exact-10 bg=1 errors=1" "wait_log what=lookup_end timeout=120" \
    "freeze_server secs=8" wait_bg "vllm_check name=stalled" "vllm_ensure model=$LLAMA" \
    "send name=after sets=P-exact ids=P-exact-11 errors=1" "vllm_check name=end"
  report sec7 $tag all "--allow-error=${tag}_victim" "--no-hit-check=${tag}_victim" \
    "--outcomes=${tag}_after=pipelined,not_deferred" store victim after
  progress "sec7: $(paste -sd' ' $S/sec7/vllm_check_${tag}_stalled.txt | cut -c1-300)"
}

sec_count7() {
  local f n total=0 outside=0
  for f in /root/lmc-work/functional/stage3/*/vllm_*.log /root/lmc-work/functional/stage5/*/vllm_*.log; do
    [ -f "$f" ] || continue
    n=$(grep -c LayerProgressRetrieveGenerationTimeoutError "$f")
    [ "$n" -gt 0 ] || continue
    total=$((total + n))
    case $f in */stage5/sec7/*) echo "deliberate: $f: $n";; *) outside=$((outside + n)); echo "OUTSIDE sec7: $f: $n";; esac
  done
  progress "count7: LayerProgressRetrieveGenerationTimeoutError lines: $total, outside the deliberate stall: $outside (any is S1)"
}

sec_dry() {
  local rc=0
  bash -n $TREE_HOST/functional/stage5/stage5.sh && bash -n $TREE_HOST/functional/stage5/flt07_rdma_down.sh || rc=1
  docker exec lmc-c python -c "
import json, sys
from lmcache.v1.distributed.l2_adapters.fault_inject_l2_adapter import FaultInjectL2AdapterConfig
c = FaultInjectL2AdapterConfig.from_dict(json.loads(sys.argv[1]))
print('fault_inject config ok: gap_tail_ratios', c.gap_tail_ratios, 'inner', type(c.inner_config).__name__)" \
    "{\"type\":\"fault_inject\",\"gap_tail_ratios\":[0.5],\"inner\":$(l2_json 4 $LLAMA_CHUNK)}" || rc=1
  local t; for t in 0 1 2 3 4; do echo "P-multi turn $t: $(turn_ids $t)"; done
  echo "flt01 prompts: $( { ids P-exact 4 4; ids P-ragged 6 16; } | paste -sd,)"
  bash $TREE_HOST/functional/stage5/flt07_rdma_down.sh check
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
