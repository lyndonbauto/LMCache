#!/bin/bash
# Stage 4: concurrency (functional-test-plan.md 5.5 and 5.6): T-PIPE-08,
# T-PIPE-10 and T-E2E-09 on the Stage 3 setup. Llama-3.1-8B, LMCache with the
# Aerospike RDMA adapter against the kv-sink server (127.0.0.1:3100, RC on
# rxe0 GID 1), --use-layerwise, vLLM async scheduling on (vLLM's default; the
# harness never passes --no-async-scheduling), VLLM_BATCH_INVARIANT=1.
# Runs on the host; each session is a run_steps.sh session in lmc-c. Uses
# stage3.sh's helpers (session, report, group, ids, wait_idle).
#
# Why two vLLM instances: LMCache runs RETRIEVE as a blocking handler on an
# affinity thread chosen by the ZMQ client identity (mq.py, affinity_pool.py),
# so every retrieve of one vLLM (TP=1) runs on one thread, one after the
# other, and each pipelined fetch releases its window before the next starts.
# One vLLM can therefore never hold more windows than one, nor have two
# fetches of the same key in flight. T-PIPE-08 and T-PIPE-10 run a second
# vLLM on 127.0.0.1:8001 against the same LMCache server, which runs with
# --max-gpu-workers 2 so each instance gets its own retrieve thread. pipe08s
# keeps the single-instance case as a record of that serialization.
#
# Usage: stage4.sh [section ...]
# Sections (default: precheck pipe08s pipe08 pipe10 ref16 e2e09 idle):
#   precheck  refuse to run if 8000/8001/6555/8080/3100 are taken
#   pipe08s   T-PIPE-08 single instance: 8 concurrent <=4-chunk L2 hits,
#             window_count 2. Expected: retrieves serialize, so 0 `refused`
#             (recorded, not a failure); all outputs equal
#   pipe08    T-PIPE-08: two instances, window_count 1, 4 + 4 concurrent
#             <=4-chunk L2 hits on distinct keys, 3 rounds. Pass: at least
#             one `refused`, each served whole, every output equal
#   pipe10    T-PIPE-10: two instances send the same 4-chunk prompt at once
#             (5 prompts), under --pipelined-shared-keys recompute and wait.
#             recompute: a pair with `pipelined` + `shared_keys_busy`;
#             wait: a pair with `reused`, no `shared_keys_busy`; all equal
#   ref16     no-LMCache reference at concurrency 16 with T-E2E-09's arrival
#             pattern (run_ref.sh, batch invariant, no prefix caching), twice;
#             compare.py against the batch-1 baseline decides whether token
#             equality is a valid oracle for T-E2E-09
#   e2e09     T-E2E-09: 16 concurrent clients over P-shared + P-multi (60
#             prompts): cold, from L1, and from L2 after a restart; plain path
#             and pipelined path (cap 4). Oracles: token equality with the
#             batch-1 baseline (valid while ref16 matches it) and top-1
#             agreement >= 99.9% (logprob_agree.py)
#   idle      wait for the GPU to be idle and print USED_VRAM
#   dry       CPU-only: script syntax, prompt ids, adapter configs, every
#             LMCache server configuration starts against kv-sink (ports
#             6655/8180/9190), logprob_agree.py on recorded runs
# Environment: TREE_HOST / TREE_CTR (default /root/lmc-work/LMCache, /work/LMCache),
#   PIPE08_ROUNDS (default 3), TWO_VLLM_UTIL (default 0.3: each of the two
#   instances' --gpu-memory-utilization).
# Policies: pipe10 runs under kv_load_failure_policy recompute, because the
# plan's `shared_keys_busy` outcome makes vLLM recompute; the rest under fail,
# so a bad load errors instead of recomputing silently.
set -u
TREE_HOST=${TREE_HOST:-/root/lmc-work/LMCache}
# shellcheck source=../stage3/stage3.sh
source "$TREE_HOST/functional/stage3/stage3.sh"
S=/root/lmc-work/functional/stage4
W=/work/functional/stage4
PIPE08_ROUNDS=${PIPE08_ROUNDS:-3}
TWO_VLLM_UTIL=${TWO_VLLM_UTIL:-0.3}
# T-PIPE-08 prompts: 8 distinct prompts of at most 4 chunks (D-12).
P08_A=P-exact-10,P-exact-11,P-exact-12,P-exact-13
P08_B=P-exact-14,P-ragged-08,P-ragged-09,P-ragged-10
P10_PROMPTS="10 11 12 13 14"
E2E09_SETS=P-shared,P-multi

# l2_json_w <windows> <cap> <chunk-bytes>: stage3's adapter spec with
# window_count <windows>.
l2_json_w() {
  echo "{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:3100\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\",\"rdma\":{\"transport\":\"RC\",\"device_name\":\"rxe0\",\"gid_index\":1,\"window_count\":$1,\"window_bytes\":$(($2 * $3))}}"
}
# outcome_counts <dir> <tag>: "N outcome" pairs of the session's LMCache log.
outcome_counts() {
  grep -oE 'pipelined_outcome=[a-z_]+' $S/$1/lmcache_$2.log | sort | uniq -c | sed 's/^ *//;s/pipelined_outcome=//' | paste -sd' '
}
# outcomes_of <dir> <tag> <run>: the outcomes of one send's requests (by the
# request ids client.py recorded), one per line as "<prompt> <outcome>".
outcomes_of() {
  docker exec lmc-c python - "$W/$1/lmcache_$2.log" "$W/$1/${2}_$3.json" <<'EOF'
import json, re, sys
log = open(sys.argv[1]).read()
res = json.load(open(sys.argv[2]))["results"]
pat = re.compile(r"MP retrieve end: session=(\S+) .*?pipelined_outcome=([a-z_]+)")
ends = pat.findall(log)
for pid, r in res.items():
    rid = r.get("request_id", "")
    outs = sorted({o for s, o in ends if rid and s.startswith(rid)})
    print(pid, ",".join(outs) or "-")
EOF
}
two_vllm_errors() {
  echo "-- second vLLM error lines: $(grep -cE 'Traceback|ERROR|Error' $S/$1/vllm2_${2}_*.log 2>/dev/null | paste -sd' ')"
}

sec_precheck() {
  local busy; busy=$(ss -ltn | grep -E '127.0.0.1:(8000|8001|6555|8080|3100) ')
  [ -z "$busy" ] || { echo "ports in use (another session?):"; echo "$busy"; exit 1; }
  echo "ports 8000/8001/6555/8080/3100 free; $(wait_idle)"
}

sec_pipe08s() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  tag=pipe08s
  group pipe08s $tag
  session pipe08s $tag fail server "vllm model=$LLAMA" \
    "send name=store sets=P-exact,P-ragged ids=$P08_A,$P08_B" settle restart \
    "send name=conc8 sets=P-exact,P-ragged ids=$P08_A,$P08_B conc=8 stats=1"
  report pipe08s $tag all --outcomes=${tag}_conc8=pipelined,refused,not_deferred store conc8
  progress "pipe08s (one vLLM, window_count 2, 8 concurrent): outcomes $(outcome_counts pipe08s $tag); \
'Pipelined fetch refused' lines: $(grep -c 'Pipelined fetch refused' $S/pipe08s/lmcache_$tag.log) \
(0 expected: one affinity thread per vLLM)"
}

sec_pipe08() {
  SERVER_FLAGS="--max-gpu-workers 2 --pipelined-fetch --pipelined-max-chunks 4 --l2-adapter $(l2_json_w 1 4 $LLAMA_CHUNK)"
  tag=pipe08
  group pipe08 $tag
  local steps=(server "vllm model=$LLAMA" "vllm2 model=$LLAMA"
    "send name=storea sets=P-exact ids=$P08_A"
    "send name=storeb sets=P-exact,P-ragged ids=$P08_B port=8001" settle)
  local r sends=(storea storeb) rargs=()
  for r in $(seq 1 "$PIPE08_ROUNDS"); do
    steps+=(restart
      "send name=a$r sets=P-exact ids=$P08_A conc=4 bg=1"
      "send name=b$r sets=P-exact,P-ragged ids=$P08_B conc=4 bg=1 port=8001" wait_bg)
    sends+=("a$r" "b$r")
    rargs+=("--outcomes=${tag}_a$r=pipelined,refused" "--outcomes=${tag}_b$r=pipelined,refused"
            "--no-hit-check=${tag}_a$r" "--no-hit-check=${tag}_b$r")
  done
  VLLM_EXTRA="--gpu-memory-utilization $TWO_VLLM_UTIL" session pipe08 $tag fail "${steps[@]}"
  two_vllm_errors pipe08 $tag
  report pipe08 $tag all "${rargs[@]}" "${sends[@]}"
  local refused; refused=$(grep -c 'pipelined_outcome=refused' $S/pipe08/lmcache_$tag.log)
  progress "pipe08 (two vLLMs, window_count 1, 4+4 concurrent x $PIPE08_ROUNDS): outcomes $(outcome_counts pipe08 $tag); \
refused retrieves: $refused (pass needs >= 1 and every output equal in report_${tag}_all.md); \
affinity slots: $(grep -c 'AffinityThreadPool: affinity_key=' $S/pipe08/lmcache_$tag.log)"
}

# pipe10_mode <recompute|wait>
pipe10_mode() {
  local mode=$1 p
  SERVER_FLAGS="--max-gpu-workers 2 --pipelined-shared-keys $mode $(server_flags 4 $LLAMA_CHUNK)"
  tag=pipe10_$mode
  group pipe10 $tag
  local ids5; ids5=$(for p in $P10_PROMPTS; do printf 'P-exact-%s,' "$p"; done | sed 's/,$//')
  local steps=(server "vllm model=$LLAMA" "vllm2 model=$LLAMA" "send name=store sets=P-exact ids=$ids5" settle restart)
  local sends=(store) rargs=("--outcomes=*=pipelined,reused,shared_keys_busy,not_deferred")
  for p in $P10_PROMPTS; do
    steps+=("send name=a$p sets=P-exact ids=P-exact-$p bg=1"
            "send name=b$p sets=P-exact ids=P-exact-$p bg=1 port=8001" wait_bg)
    sends+=("a$p" "b$p")
    rargs+=("--no-hit-check=${tag}_a$p" "--no-hit-check=${tag}_b$p")
  done
  VLLM_EXTRA="--gpu-memory-utilization $TWO_VLLM_UTIL" session pipe10 $tag recompute "${steps[@]}"
  two_vllm_errors pipe10 $tag
  report pipe10 $tag all "${rargs[@]}" "${sends[@]}"
  local pairs="" a b busy_pair=0 reused_pair=0
  for p in $P10_PROMPTS; do
    a=$(outcomes_of pipe10 $tag a$p | awk '{print $2}')
    b=$(outcomes_of pipe10 $tag b$p | awk '{print $2}')
    pairs="$pairs P-exact-$p:$a/$b"
    case "$a/$b" in *pipelined*shared_keys_busy*|*shared_keys_busy*pipelined*) busy_pair=$((busy_pair + 1));; esac
    case "$a/$b" in *reused*) reused_pair=$((reused_pair + 1));; esac
  done
  local nbusy; nbusy=$(grep -c 'pipelined_outcome=shared_keys_busy' $S/pipe10/lmcache_$tag.log)
  local verdict
  if [ "$mode" = recompute ]; then
    [ "$busy_pair" -ge 1 ] && verdict="outcome ok" || verdict="no overlapping pair (rerun; not a failure by itself)"
  else
    if [ "$nbusy" -gt 0 ]; then verdict="FAIL: shared_keys_busy under wait"
    elif [ "$reused_pair" -ge 1 ]; then verdict="outcome ok"
    else verdict="no reused pair (overlap missed or D-13 eviction made it fetch again)"; fi
  fi
  progress "pipe10 $mode: pairs (instance 1 / instance 2):$pairs; pipelined+busy pairs $busy_pair, \
reused pairs $reused_pair, shared_keys_busy total $nbusy: $verdict; output equality in report_${tag}_all.md; \
'Cannot retrieve keys' lines (one per busy retrieve, expected under recompute): \
$(grep -c 'Cannot retrieve keys due to exception' $S/pipe10/lmcache_$tag.log)"
}
sec_pipe10() {
  pipe10_mode recompute
  pipe10_mode wait
}

sec_ref16() {
  mkdir -p $S/ref16
  local run
  for run in a b; do
    echo "##### ref16 $run $(date -u +%T) $(wait_idle)"
    docker exec -e VLLM_BATCH_INVARIANT=1 -e GPU_UTIL=0.6 -e VLLM_EXTRA=--no-enable-prefix-caching lmc-c \
      bash $H/run_ref.sh $LLAMA $CB $W/ref16 ref16$run 8000 "name=c16 sets=$E2E09_SETS conc=16" \
      > $S/ref16/session_ref16$run.txt 2>&1
    echo "rc=$?"
    docker exec lmc-c python $H/compare.py $BASE1 $W/ref16/ref16${run}_c16.json \
      --out $W/ref16/compare_ref16${run}.json > $S/ref16/compare_ref16$run.md 2>&1
  done
  docker exec lmc-c python $H/compare.py $W/ref16/ref16a_c16.json $W/ref16/ref16b_c16.json \
    > $S/ref16/compare_ref16a_b.md 2>&1
  progress "ref16 (no LMCache, concurrency 16, batch invariant) vs batch 1: \
a: $(grep '^Total' $S/ref16/compare_ref16a.md); b: $(grep '^Total' $S/ref16/compare_ref16b.md); \
a vs b: $(grep '^Total' $S/ref16/compare_ref16a_b.md); \
token equality is T-E2E-09's oracle only if a and b are 60/60 exact"
}

# e2e09_variant <plain|pipe>
e2e09_variant() {
  local v=$1 rargs=()
  if [ "$v" = plain ]; then
    SERVER_FLAGS="--l2-adapter $(l2_json 4 $LLAMA_CHUNK)"
  else
    SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
    rargs=("--outcomes=*=pipelined,not_deferred")
  fi
  tag=e2e09_$v
  group e2e09 $tag
  session e2e09 $tag fail server "vllm model=$LLAMA" \
    "send name=cold sets=$E2E09_SETS conc=16" settle \
    "send name=l1 sets=$E2E09_SETS conc=16 stats=1" restart \
    "send name=l2 sets=$E2E09_SETS conc=16 stats=1"
  # The cold send's hits depend on which in-flight requests stored first.
  report e2e09 $tag all "${rargs[@]}" "--no-hit-check=${tag}_cold" cold l1 l2
  docker exec lmc-c python $H/logprob_agree.py --baseline $BASE1 --out $W/e2e09/agree_$tag.json \
    $W/e2e09/${tag}_cold.json $W/e2e09/${tag}_l1.json $W/e2e09/${tag}_l2.json > $S/e2e09/agree_$tag.md 2>&1
  progress "e2e09 $v: $(tail -n 1 $S/e2e09/report_${tag}_all.md | cut -c1-300); $(tail -n 1 $S/e2e09/agree_$tag.md); \
outcomes $(outcome_counts e2e09 $tag)"
}
sec_e2e09() {
  e2e09_variant plain
  e2e09_variant pipe
}

sec_dry() {
  local f rc=0 cfg
  for f in $TREE_HOST/functional/harness/run_steps.sh $TREE_HOST/functional/stage4/stage4.sh; do
    bash -n "$f" && echo "syntax ok: $f" || rc=1
  done
  for cfg in "$(l2_json_w 1 4 $LLAMA_CHUNK)" "$(l2_json_w 2 4 $LLAMA_CHUNK)"; do
    docker exec lmc-c python -c "
import json, sys
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import AerospikeL2AdapterConfig
c = AerospikeL2AdapterConfig.from_dict(json.loads(sys.argv[1]))
print('adapter config ok: window_count', c.rdma.window_plan.window_count, 'window_bytes', c.rdma.window_plan.window_bytes)" "$cfg" || rc=1
  done
  echo "pipe08 A=$P08_A (chunks: $(docker exec lmc-c python -c "
import json
d = json.load(open('$CB'))
n = {p['id']: p['n_tokens'] for s in d['sets'].values() for p in s}
print(' '.join(f'{i}={n[i] // 256}+{n[i] % 256}' for i in '$P08_A,$P08_B'.split(',')))"))"
  echo "e2e09 prompts: $(docker exec lmc-c python -c "
import json
d = json.load(open('$CB'))
print(sum(len(d['sets'][s]) for s in '$E2E09_SETS'.split(',')))") (P-shared + P-multi)"
  local D=/root/lmc-work/functional/stage4/dry DC=/work/functional/stage4/dry
  mkdir -p $D
  kvsink_restart $D/kvsink || rc=1
  run4() { docker exec -w "$TREE_CTR" lmc-c bash "$TREE_CTR/functional/stage3/dryrun_lmcache.sh" $DC "$@" || rc=1; }
  run4 pipe08_w1_gpu2 --max-gpu-workers 2 --pipelined-fetch --pipelined-max-chunks 4 --l2-adapter "$(l2_json_w 1 4 $LLAMA_CHUNK)"
  run4 pipe10_recompute --max-gpu-workers 2 --pipelined-shared-keys recompute $(server_flags 4 $LLAMA_CHUNK)
  run4 pipe10_wait --max-gpu-workers 2 --pipelined-shared-keys wait $(server_flags 4 $LLAMA_CHUNK)
  run4 e2e09_plain --l2-adapter "$(l2_json 4 $LLAMA_CHUNK)"
  grep -hE 'AffinityThreadPool|pipelined_shared|shared.keys|layer publish budget' $D/lmcache_dry_pipe08_w1_gpu2.log \
    $D/lmcache_dry_pipe10_wait.log | cut -c1-220 | sort | uniq | head -n 8
  KVSINK_CONF=$TREE_HOST/functional/configs/aerospike-kvsink.conf KVSINK_LOG=$D/kvsink/asd-kvsink.log \
    bash "$TREE_HOST/functional/harness/kvsink_server.sh" stop
  echo "-- logprob_agree.py on recorded runs (Stage 2b concurrency vs the batch-1 baseline):"
  docker exec lmc-c python $H/logprob_agree.py --baseline $BASE1 \
    /work/functional/stage2/conc/conc_lw_true_lthree.json /work/functional/stage2/conc/conc_lw_true_rthree.json | tail -n 4
  return $rc
}

mkdir -p $S
# shellcheck disable=SC2048
for sec in ${*:-precheck pipe08s pipe08 pipe10 ref16 e2e09 idle}; do
  progress "section $sec started"
  sec_$sec
  progress "section $sec finished"
done
echo "##### STAGE 4 DONE $(date -u +%T) $(wait_idle)"
