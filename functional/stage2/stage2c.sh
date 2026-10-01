#!/bin/bash
# Stage 2c: T-E2E-08, the plain-path end-to-end tests (T-E2E-01/02/03/05/06/07)
# on the hybrid model gpt-oss-120b, plus the references they are judged
# against. Aerospike CE L2, no RDMA, no pipelined fetch. Runs on the host;
# each session runs in lmc-c. Usage: stage2c.sh [section ...]
# Sections: corpus refs1 refs2 lmc reports (default: all, in order).
#   refs1: no-cache baseline at block size 16 (port 8000) alongside vLLM's own
#          prefix cache at block size 16 (port 8001), same send order as the
#          LMCache session.
#   refs2: a second prefix-cache run at block size 16 (determinism) alongside
#          a prefix-cache run at block size 256, whose cached prefixes end
#          where LMCache's 256-token chunks end.
#   lmc:   one LMCache session per layerwise mode covering every test.
set -u
S=/root/lmc-work/functional/stage2
W=/work/functional/stage2
H=/work/LMCache/functional/harness
GPT=openai/gpt-oss-120b
SPEC=/work/LMCache/functional/corpus/corpus_spec.json
C1=/work/functional/corpus/corpus_gpt-oss-120b.json
CG=/work/functional/corpus/corpus_gpt-oss-120b.v2.json
R=$W/gptoss_ref
BASE=$R/base_b16_all.json
L2='--l2-adapter {"type":"aerospike","hosts":"127.0.0.1:3000","namespace":"lmcache","set_name":"kv_chunks"}'
PY314=/root/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14
ALL=P-short-v2,P-exact,P-ragged,P-long,P-shared,P-multi
PC_SENDS=("name=rc sets=P-ragged" "name=rw sets=P-ragged" "name=sc sets=P-shared"
  "name=sw sets=P-shared" "name=mc sets=P-multi" "name=mw sets=P-multi")
vram() { amd-smi metric --mem-usage 2>/dev/null | grep -m1 USED_VRAM | grep -oE '[0-9]+'; }
wait_idle() { for _ in $(seq 60); do u=$(vram); [ -n "$u" ] && [ "$u" -lt 4000 ] && break; sleep 3; done; echo "VRAM ${u} MB"; }
as_stat() { docker exec aerospike-ce asinfo -v "namespace/lmcache" | tr ';' '\n' | grep -E "^$1=" | cut -d= -f2; }
truncate_l2() { docker exec aerospike-ce asinfo -v "truncate:namespace=lmcache;set=kv_chunks" >/dev/null; sleep 5; echo "L2 truncated; objects=$(as_stat objects)"; }
progress() { echo "$(date -u +%FT%TZ) $*" | tee -a $S/progress.log; }

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

# ref <tag> <port> <vllm flags> <send>...: one reference server in the background.
ref() {
  local tag=$1 port=$2 flags=$3; shift 3
  timeout 5400 docker exec -e VLLM_BATCH_INVARIANT=1 -e VLLM_EXTRA="$flags" lmc-c \
    bash $H/run_ref.sh $GPT $CG $R $tag $port "$@" > $S/gptoss_ref/session_$tag.txt 2>&1 &
}
# Start the second server only once the first serves, so their memory
# profiling does not overlap.
wait_serving() { for _ in $(seq 900); do grep -q "serving" $S/gptoss_ref/session_$1.txt 2>/dev/null && return 0; sleep 2; done; return 1; }
cmp() { docker exec lmc-c python $H/compare.py "$R/$1.json" "$R/$2.json" | tail -n 1; }

sec_corpus() {
  echo "##### corpus $(date -u +%T)"
  docker exec -e HF_HOME=/work/hf -e HF_HUB_OFFLINE=1 lmc-c python $H/build_corpus.py --model $GPT \
    --spec $SPEC --out $CG 2>&1 | grep -vi warn | tail -n 9
  docker exec lmc-c python -c "
import json
a = json.load(open('$C1'))['sets']; b = json.load(open('$CG'))['sets']
same = all([p['token_ids'] for p in a[s]] == [p['token_ids'] for p in b[s]] for s in a)
print('v1 sets unchanged in v2:', same, '; P-short-v2 lengths', [p['n_tokens'] for p in b['P-short-v2']])
"
}

sec_refs1() {
  mkdir -p $S/gptoss_ref
  echo "##### refs1 $(date -u +%T) $(wait_idle)"
  ref base_b16 8000 "--no-enable-prefix-caching --block-size 16" "name=all sets=$ALL" "name=conc sets=$ALL conc=8"
  local a=$!
  wait_serving base_b16 || echo "base_b16 did not come up"
  ref pc16_r1 8001 "--enable-prefix-caching --block-size 16" "${PC_SENDS[@]}"
  local b=$!
  wait $a; echo "base_b16 rc=$?"; wait $b; echo "pc16_r1 rc=$?"
  grep -hE "===|correct" $S/gptoss_ref/session_base_b16.txt $S/gptoss_ref/session_pc16_r1.txt
  progress "refs1: baseline vs concurrent-8 on the same server: $(cmp base_b16_all base_b16_conc)"
}

sec_refs2() {
  echo "##### refs2 $(date -u +%T) $(wait_idle)"
  ref pc16_r2 8000 "--enable-prefix-caching --block-size 16" "${PC_SENDS[@]}"
  local a=$!
  wait_serving pc16_r2 || echo "pc16_r2 did not come up"
  ref pc256 8001 "--enable-prefix-caching --block-size 256" "${PC_SENDS[@]}"
  local b=$!
  wait $a; echo "pc16_r2 rc=$?"; wait $b; echo "pc256 rc=$?"
  grep -hE "===|correct|rror" $S/gptoss_ref/session_pc16_r2.txt $S/gptoss_ref/session_pc256.txt | tail -n 30
  for send in rc rw sc sw mc mw; do
    progress "refs2 $send: pc16 r1 vs r2: $(cmp pc16_r1_$send pc16_r2_$send); pc256 vs pc16: $(cmp pc16_r1_$send pc256_$send)"
  done
}

sec_lmc() {
  mkdir -p $S/gptoss_e2e
  for lw in false true; do
    tag=g_lw_$lw
    echo "##### $tag $(date -u +%T) $(wait_idle) $(truncate_l2)"
    watch_hangs $S/gptoss_e2e & watcher=$!
    timeout 9000 docker exec -e VLLM_BATCH_INVARIANT=1 -e LMCACHE_SERVER_EXTRA="$L2" -e CORPUS=$CG \
      -e SEND_TIMEOUT=900 lmc-c bash $H/run_steps.sh $W/gptoss_e2e $tag $lw server "vllm model=$GPT" \
      "send name=shortc sets=P-short-v2 stats=1" "send name=shortw sets=P-short-v2 stats=1" \
      "send name=c02 sets=P-exact,P-ragged" "send name=w02 sets=P-exact,P-ragged" \
      restart "send name=l03 sets=P-exact,P-ragged stats=1" \
      "send name=longc sets=P-long" "send name=longw sets=P-long" \
      "send name=shc sets=P-shared" "send name=shw sets=P-shared" \
      "send name=muc sets=P-multi" "send name=muw sets=P-multi" \
      > $S/gptoss_e2e/session_$tag.txt 2>&1
    echo "rc=$?"; kill $watcher 2>/dev/null
    grep -E "===|correct|exited|did not|FAILED|warning" $S/gptoss_e2e/session_$tag.txt | tail -n 40
    echo "-- error lines: $(grep -cE 'Traceback|ERROR|Error' $S/gptoss_e2e/lmcache_$tag.log $S/gptoss_e2e/vllm_${tag}_*.log | paste -sd' ')"
    ls $S/gptoss_e2e/HANG_* 2>/dev/null && echo "!! a send failed; stacks in $S/gptoss_e2e/pystacks_*"
    progress "$tag session finished"
    reports_for $lw
  done
}

# Per-request reports for one layerwise mode: against the block-256 prefix
# cache (split points equal to LMCache's) and against the block-16 one.
reports_for() {
  local lw=$1 tag=g_lw_$1 ref name sends=()
  for s in shortc shortw c02 w02 l03 longc longw shc shw muc muw; do sends+=("$W/gptoss_e2e/${tag}_$s.json"); done
  for ref in pc256 pc16_r1; do
    name=report_${tag}_$ref
    docker exec lmc-c python $H/hit_report.py --corpus $CG --baseline $BASE \
      --oracle ${tag}_w02=$R/${ref}_rw.json --oracle ${tag}_l03=$R/${ref}_rw.json \
      --oracle ${tag}_shc=$R/${ref}_sc.json --oracle ${tag}_shw=$R/${ref}_sw.json \
      --oracle ${tag}_muc=$R/${ref}_mc.json --oracle ${tag}_muw=$R/${ref}_mw.json \
      --lmcache-log $W/gptoss_e2e/lmcache_$tag.log --out $W/gptoss_e2e/$name.json "${sends[@]}" \
      > $S/gptoss_e2e/$name.md 2>&1
    progress "$tag vs $ref: $(tail -n 1 $S/gptoss_e2e/$name.md | cut -c1-600)"
  done
  for s in shortc shortw l03; do
    echo "-- aerospike traffic during $s: $(docker exec lmc-c python $H/l2_stats.py diff $W/gptoss_e2e/l2stats_${tag}_${s}_before.json $W/gptoss_e2e/l2stats_${tag}_${s}_after.json)"
  done
}

sec_reports() { reports_for false; reports_for true; }

SECTIONS=${*:-corpus refs1 refs2 lmc}
for sec in $SECTIONS; do "sec_$sec"; done
echo "##### STAGE 2c DONE $(date -u +%T) $(wait_idle)"
