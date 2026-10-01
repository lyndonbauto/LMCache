#!/bin/bash
# Stage 2b: prefix semantics (T-LKP-03), tenant isolation (T-LKP-04), model
# isolation (T-LKP-05, model half), the concurrency regression for the
# layerwise + async-scheduling deadlock fix, and T-E2E-01 on P-short v2.
# Plain path: Aerospike CE L2, no RDMA, no pipelined fetch. Runs on the host;
# each session runs in lmc-c. Usage: stage2b.sh [section ...]
# Sections: corpus base e2e01 lkp03 lkp04 conc lkp05 (default: all, in order).
set -u
S=/root/lmc-work/functional/stage2
W=/work/functional/stage2
H=/work/LMCache/functional/harness
LLAMA=meta-llama/Llama-3.1-8B-Instruct
GPT=openai/gpt-oss-120b
SPEC=/work/LMCache/functional/corpus/corpus_spec.json
C1=/work/functional/corpus/corpus_llama-3.1-8b-instruct.json
C2=/work/functional/corpus/corpus_llama-3.1-8b-instruct.v2.json
CB=/work/functional/corpus/corpus_llama-3.1-8b-instruct.stage2b.json
BASE1=/work/functional/day1/step4/bi_run1.json
BASE2=$W/base2b/bi_run1.json
L2='--l2-adapter {"type":"aerospike","hosts":"127.0.0.1:3000","namespace":"lmcache","set_name":"kv_chunks"}'
PY314=/root/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14
vram() { amd-smi metric --mem-usage 2>/dev/null | grep -m1 USED_VRAM | grep -oE '[0-9]+'; }
wait_idle() { for _ in $(seq 60); do u=$(vram); [ -n "$u" ] && [ "$u" -lt 4000 ] && break; sleep 3; done; echo "VRAM ${u} MB"; }
as_stat() { docker exec aerospike-ce asinfo -v "namespace/lmcache" | tr ';' '\n' | grep -E "^$1=" | cut -d= -f2; }
truncate_l2() { docker exec aerospike-ce asinfo -v "truncate:namespace=lmcache;set=kv_chunks" >/dev/null; sleep 5; echo "L2 truncated; objects=$(as_stat objects)"; }
progress() { echo "$(date -u +%FT%TZ) $*" | tee -a $S/progress.log; }

# Capture Python stacks of every LMCache/vLLM process when run_steps.sh
# reports a failed (hung) send, then let it clean up.
watch_hangs() {
  local dir=$1 root; root=$(docker inspect -f '{{.State.Pid}}' lmc-c)
  while :; do
    for hang in "$dir"/HANG_*; do
      [ -f "$hang" ] || continue
      run=${hang##*/HANG_}
      [ -f "$dir/STACKS_DONE_$run" ] && continue
      pids=$(tr ' ,' '\n\n' < "$hang" | grep -oE '[0-9]+$' | sort -u)
      for pid in $pids; do
        nsenter -t "$root" -m -p -- $PY314 -I $H/pystacks.py "$pid" > "$dir/pystacks_${run}_$pid.txt" 2>&1
      done
      touch "$dir/STACKS_DONE_$run"
    done
    sleep 5
  done
}

# session <dir> <tag> <layerwise> <step>...: one run_steps.sh session.
session() {
  local dir=$1 tag=$2 lw=$3; shift 3
  mkdir -p $S/$dir
  echo "##### $tag $(date -u +%T) $(wait_idle) $(truncate_l2)"
  watch_hangs $S/$dir & local watcher=$!
  timeout 7200 docker exec -e VLLM_BATCH_INVARIANT=1 -e LMCACHE_SERVER_EXTRA="$L2" -e CORPUS=$CB \
    -e SEND_TIMEOUT=${SEND_TIMEOUT:-900} -e VLLM_EXTRA="${VLLM_EXTRA:-}" lmc-c \
    bash $H/run_steps.sh $W/$dir $tag $lw "$@" > $S/$dir/session_$tag.txt 2>&1
  local rc=$?
  kill $watcher 2>/dev/null
  echo "rc=$rc"; grep -E "===|correct|concurrency|exited|did not|FAILED|warning|records" $S/$dir/session_$tag.txt | tail -n 40
  echo "-- error lines: $(grep -cE 'Traceback|ERROR|Error' $S/$dir/lmcache_$tag.log $S/$dir/vllm_${tag}_*.log | paste -sd' ')"
  ls $S/$dir/HANG_* 2>/dev/null && echo "!! a send failed; stacks in $S/$dir/pystacks_*"
  return $rc
}
# report <dir> <tag> <name> [hit_report args] <send>...: per-request report.
report() {
  local dir=$1 tag=$2 name=$3; shift 3
  local args=() runs=()
  for a in "$@"; do
    case $a in --*) args+=("$a");; *) runs+=("$W/$dir/${tag}_$a.json");; esac
  done
  docker exec lmc-c python $H/hit_report.py --corpus $CB --baseline $BASE1 --baseline $BASE2 \
    --lmcache-log $W/$dir/lmcache_$tag.log --out $W/$dir/report_${tag}_$name.json "${args[@]}" "${runs[@]}" \
    > $S/$dir/report_${tag}_$name.md 2>&1
  progress "$tag $name: $(tail -n 1 $S/$dir/report_${tag}_$name.md | cut -c1-400)"
}
l2diff() { docker exec lmc-c python $H/l2_stats.py diff $W/$1/l2stats_${2}_before.json $W/$1/l2stats_${2}_after.json; }

sec_corpus() {
  echo "##### corpus $(date -u +%T)"
  docker exec -e HF_HOME=/work/hf -e HF_HUB_OFFLINE=1 lmc-c python $H/build_corpus.py --model $LLAMA \
    --spec $SPEC --out $C2 2>&1 | grep -vi warn | tail -n 9
  docker exec lmc-c python -c "
import json
a = json.load(open('$C1'))['sets']; b = json.load(open('$C2'))['sets']
same = all([p['token_ids'] for p in a[s]] == [p['token_ids'] for p in b[s]] for s in a)
print('v1 sets unchanged in v2:', same, '; P-short-v2 lengths', [p['n_tokens'] for p in b['P-short-v2']])
"
  docker exec lmc-c python $H/prefix_corpus.py --corpus $C2 --out $CB
}

sec_base() {
  mkdir -p $S/base2b
  for run in 1 2; do
    echo "##### base2b run$run $(date -u +%T) $(wait_idle)"
    timeout 3600 docker exec -e VLLM_BATCH_INVARIANT=1 lmc-c bash $H/run_baseline.sh $LLAMA $CB $W/base2b bi_run$run \
      P-short-v2,P-prefix > $S/base2b/session_bi_run$run.txt 2>&1
    echo "rc=$?"; grep -E "===|correct" $S/base2b/session_bi_run$run.txt | tail -n 5
  done
  docker exec lmc-c python $H/compare.py $W/base2b/bi_run1.json $W/base2b/bi_run2.json --out $W/base2b/compare_base.json \
    | tee $S/base2b/compare_base.md | tail -n 5
  progress "base2b: batch-invariant baselines for P-short-v2 and P-prefix recorded; $(tail -n 1 $S/base2b/compare_base.md)"
}

sec_e2e01() {
  mkdir -p $S/e2e01v2
  for lw in false true; do
    tag=e2e01v2_lw_$lw
    echo "##### $tag $(date -u +%T) $(wait_idle) $(truncate_l2)"
    timeout 3600 docker exec -e VLLM_BATCH_INVARIANT=1 -e LMCACHE_SERVER_EXTRA="$L2" -e L2_STATS=1 lmc-c \
      bash $H/run_lmcache.sh $LLAMA $CB $W/e2e01v2 $tag $lw P-short-v2 > $S/e2e01v2/session_$tag.txt 2>&1
    echo "rc=$?"; grep -E "===|correct|exited|did not|failed" $S/e2e01v2/session_$tag.txt | tail -n 6
    for kind in cold warm; do
      echo "-- aerospike traffic during $kind send: $(l2diff e2e01v2 ${tag}_$kind)"
    done
    echo "-- aerospike traffic, whole session: $(docker exec lmc-c python $H/l2_stats.py diff $W/e2e01v2/l2stats_${tag}_cold_before.json $W/e2e01v2/l2stats_${tag}_warm_after.json)"
    echo "-- objects in kv_chunks after the session: $(as_stat objects)"
    echo "-- L2 store/lookup log lines: $(grep -ciE 'store task|L2 prefetch lookup submitted|l2 load|Stored .* to adapter' $S/e2e01v2/lmcache_$tag.log)"
    report e2e01v2 $tag all cold warm
  done
}

sec_lkp03() {
  for lw in false true; do
    tag=lkp03_lw_$lw
    session lkp03 $tag $lw server "vllm model=$LLAMA" \
      "send name=store sets=P-prefix ids=P-prefix-00" \
      "send name=probe sets=P-prefix ids=P-prefix-01,P-prefix-02,P-prefix-03,P-prefix-04,P-prefix-00" \
      "send name=gtwo sets=P-prefix ids=P-prefix-06" "keys name=k1" \
      "send name=gthree sets=P-prefix ids=P-prefix-07" "keys name=k2" \
      "send name=gfull sets=P-prefix ids=P-prefix-05" settle \
      "delkeys before=k1 after=k2" restart \
      "send name=gapprobe sets=P-prefix ids=P-prefix-05 stats=1" \
      "send name=healed sets=P-prefix ids=P-prefix-05"
    echo "-- aerospike traffic during the gap probe: $(l2diff lkp03 ${tag}_gapprobe)"
    report lkp03 $tag all --expect=${tag}_gapprobe:P-prefix-05=512 store probe gtwo gthree gfull gapprobe healed
  done
}

sec_lkp04() {
  for lw in false true; do
    tag=lkp04_lw_$lw
    session lkp04 $tag $lw server "vllm model=$LLAMA" \
      "send name=aone sets=P-shared salt=tenant-a" \
      "send name=bone sets=P-shared salt=tenant-b" \
      "send name=atwo sets=P-shared salt=tenant-a" \
      reset \
      "send name=athree sets=P-shared salt=tenant-a" restart \
      "send name=btwo sets=P-shared salt=tenant-b stats=1" \
      "send name=afour sets=P-shared salt=tenant-a"
    echo "-- aerospike traffic during salt B's first pass after the restart: $(l2diff lkp04 ${tag}_btwo)"
    report lkp04 $tag l1 aone bone atwo
    report lkp04 $tag l2 athree btwo afour
  done
}

sec_conc() {
  local ids4=P-ragged-16,P-ragged-17,P-ragged-18,P-ragged-19
  local ids8=P-ragged-12,P-ragged-13,P-ragged-14,P-ragged-15,$ids4
  local ids16=P-ragged-04,P-ragged-05,P-ragged-06,P-ragged-07,P-ragged-08,P-ragged-09,P-ragged-10,P-ragged-11,$ids8
  tag=conc_lw_true
  SEND_TIMEOUT=600 session conc $tag true server "vllm model=$LLAMA" \
    "send name=cold sets=P-ragged" \
    "send name=lone sets=P-ragged ids=$ids4 conc=4" \
    "send name=ltwo sets=P-ragged ids=$ids8 conc=8" \
    "send name=lthree sets=P-ragged ids=$ids16 conc=16" \
    restart "send name=rone sets=P-ragged ids=$ids4 conc=4 stats=1" \
    restart "send name=rtwo sets=P-ragged ids=$ids8 conc=8 stats=1" \
    restart "send name=rthree sets=P-ragged ids=$ids16 conc=16 stats=1"
  report conc $tag all cold lone ltwo lthree rone rtwo rthree
}

sec_lkp05() {
  for lw in false true; do
    tag=lkp05_lw_$lw
    VLLM_EXTRA="--gpu-memory-utilization 0.45" session lkp05 $tag $lw server "vllm model=$LLAMA" \
      "send name=llama sets=P-exact,P-shared" settle vllm_stop \
      "vllm model=$GPT" "send name=gptlone sets=P-exact" \
      restart "send name=gptltwo sets=P-shared stats=1" vllm_stop \
      "vllm model=$LLAMA" "send name=llamaback sets=P-exact,P-shared"
    echo "-- aerospike traffic during gpt-oss's P-shared send after the restart: $(l2diff lkp05 ${tag}_gptltwo)"
    report lkp05 $tag all llama gptlone gptltwo llamaback
  done
}

for sec in ${*:-corpus base e2e01 lkp03 lkp04 conc lkp05}; do
  progress "section $sec started"
  sec_$sec
  progress "section $sec finished"
done
echo "##### STAGE 2b DONE $(date -u +%T) $(wait_idle)"
