#!/bin/bash
# Stage 2a: the plain path end to end on Llama-3.1-8B (Aerospike CE L2, no RDMA,
# no pipelined fetch). Runs on the host; each session runs in lmc-c.
# Usage: stage2a.sh [test ...]   tests: e2e01 e2e02 e2e03 e2e05 e2e06 e2e07
# Every test runs layerwise off, then on.
set -u
S=/root/lmc-work/functional/stage2
W=/work/functional/stage2
H=/work/LMCache/functional/harness
MODEL=meta-llama/Llama-3.1-8B-Instruct
CORPUS=/work/functional/corpus/corpus_llama-3.1-8b-instruct.json
BASE=/work/functional/day1/step4/bi_run1.json
L2='--l2-adapter {"type":"aerospike","hosts":"127.0.0.1:3000","namespace":"lmcache","set_name":"kv_chunks"}'
vram() { amd-smi metric --mem-usage 2>/dev/null | grep -m1 USED_VRAM | grep -oE '[0-9]+'; }
wait_idle() { for _ in $(seq 60); do u=$(vram); [ -n "$u" ] && [ "$u" -lt 4000 ] && break; sleep 3; done; echo "VRAM ${u} MB"; }
as_stat() { docker exec aerospike-ce asinfo -v "namespace/lmcache" | tr ';' '\n' | grep -E "^$1=" | cut -d= -f2; }
truncate_l2() { docker exec aerospike-ce asinfo -v "truncate:namespace=lmcache;set=kv_chunks" >/dev/null; sleep 5; echo "L2 truncated; objects=$(as_stat objects)"; }

sets_of() { case $1 in
  e2e01) echo P-short;; e2e02|e2e03) echo P-exact,P-ragged;; e2e05) echo P-long;;
  e2e06) echo P-shared;; e2e07) echo P-multi;; esac; }

TESTS=${*:-e2e01 e2e02 e2e03 e2e05 e2e06 e2e07}
for test in $TESTS; do
  mkdir -p $S/$test
  for lw in false true; do
    tag=${test}_lw_$lw
    restart=0; [ $test = e2e03 ] && restart=1
    echo "##### $tag $(date -u +%T) $(wait_idle) $(truncate_l2)"
    timeout 5400 docker exec -e VLLM_BATCH_INVARIANT=1 -e LMCACHE_SERVER_EXTRA="$L2" \
      -e L2_STATS=1 -e RESTART_SERVER_BEFORE_WARM=$restart lmc-c \
      bash $H/run_lmcache.sh $MODEL $CORPUS $W/$test $tag $lw "$(sets_of $test)" > $S/$test/session_$tag.txt 2>&1
    echo "rc=$?"; grep -E "===|correct|exited|did not|failed|settled|warning" $S/$test/session_$tag.txt | tail -n 12
    echo "-- errors: $(grep -cE 'Traceback|Error|error' $S/$test/lmcache_$tag.log $S/$test/vllm_$tag.log | paste -sd' ')"
    echo "-- L2 store/load log lines: $(grep -ciE 'store task|L2 prefetch lookup submitted|l2 load|Stored .* to adapter' $S/$test/lmcache_$tag.log)"
    for kind in cold warm; do
      [ -f $S/$test/l2stats_${tag}_${kind}_after.json ] && echo "-- aerospike traffic during $kind send (incl. async tail): $(docker exec lmc-c python $H/l2_stats.py diff $W/$test/l2stats_${tag}_${kind}_before.json $W/$test/l2stats_${tag}_${kind}_after.json)"
    done
    [ -f $S/$test/l2stats_${tag}_warm_after.json ] && echo "-- aerospike traffic, whole session (cold before to warm after): $(docker exec lmc-c python $H/l2_stats.py diff $W/$test/l2stats_${tag}_cold_before.json $W/$test/l2stats_${tag}_warm_after.json)"
    [ -f $S/$test/${tag}_warm.json ] && docker exec lmc-c python $H/hit_report.py --corpus $CORPUS --baseline $BASE \
      --lmcache-log $W/$test/lmcache_$tag.log --out $W/$test/report_$tag.json \
      $W/$test/${tag}_cold.json $W/$test/${tag}_warm.json > $S/$test/report_$tag.md 2>&1
    tail -n 1 $S/$test/report_$tag.md
    echo "$(date -u +%FT%TZ) $tag finished: $(tail -n 1 $S/$test/report_$tag.md | cut -c1-300)" >> $S/progress.log
  done
done
echo "##### STAGE 2a DONE $(date -u +%T) $(wait_idle)"
