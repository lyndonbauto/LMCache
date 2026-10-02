#!/bin/bash
# Stage 3 on the new kv-sink stack: sanity checks before the Stage 3 rerun.
# Sources stage3.sh for its helpers; source functional/newstack/kvsink_bp_env.sh
# first so groups restart the batch-read server (aero-kvsink-bp, 3700).
#
# Usage: STAGE_DIR=stage3-newstack sanity.sh [section ...]
#   its     the layerwise, RDMA and Aerospike integration suites in lmc-c with
#           the GPU visible, against a fresh kv-sink on $KVSINK_PORT
#   e2e123  T-E2E-01..03 on the plain path (L2 = Aerospike CE on 3000, no
#           pipelining), layerwise on and off: P-short-v2 twice (no L2 hit),
#           P-exact twice (L1 hit), LMCache restart, P-exact (L2 hit)
#   d15     D-15: clean LMCache shutdowns (SIGTERM) right after pipelined
#           fetches and three times during one (twice at the lookup's end, when
#           the fetch is issued, once at retrieve start); the exit status and
#           any crash lines in its log. D15_GRACE (default 60) is the wait before
#           SIGKILL: a clean shutdown takes about 14 s (telemetry flush timeouts)
set -u
# shellcheck source=../stage3/stage3.sh
source "$(dirname "$0")/../stage3/stage3.sh"
CE_L2='--l2-adapter {"type":"aerospike","hosts":"127.0.0.1:3000","namespace":"lmcache","set_name":"kv_chunks"}'
CRASH='Fatal Python error|Segmentation fault|core dumped|Aborted|SIGSEGV|SIGABRT|double free|corrupted'

sec_its() {
  group its kvsink
  mkdir -p $S/its
  docker exec -w $TREE_CTR lmc-c env PYTHONDONTWRITEBYTECODE=1 RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
    RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=$KVSINK_PORT \
    AEROSPIKE_TEST_NAMESPACE=lmcache AEROSPIKE_TEST_EVICT_NAMESPACE=lmcache_evict \
    bash -c "timeout 5400 python -m pytest -p no:cacheprovider -q -rfEs --junitxml=$W/its/junit.xml \
      --durations=20 tests/v1/layerwise tests/v1/distributed/rdma tests/v1/distributed/test_aerospike_*.py" \
    > $S/its/pytest.txt 2>&1
  progress "its: $(tail -n 1 $S/its/pytest.txt); GPU visible: $(docker exec lmc-c python -c 'import torch; print(torch.cuda.device_count())' 2>/dev/null)"
}

sec_e2e123() {
  local lw tag
  SERVER_FLAGS=$CE_L2
  for lw in true false; do
    tag=e2e123_lw_$lw
    LW_S=$lw L2_PORT_S=3000 session e2e123 $tag fail server reset "vllm model=$LLAMA" \
      "send name=shortc sets=P-short-v2 stats=1" "send name=shortw sets=P-short-v2 stats=1" \
      "send name=c02 sets=P-exact stats=1" "send name=w02 sets=P-exact stats=1" restart \
      "send name=l03 sets=P-exact stats=1"
    report e2e123 $tag all --outcomes=${tag}_l03=not_deferred --outcomes=${tag}_w02=not_deferred \
      shortc shortw c02 w02 l03
    progress "$tag: aerospike reads during shortw: $(docker exec lmc-c python $H/l2_stats.py diff \
$W/e2e123/l2stats_${tag}_shortw_before.json $W/e2e123/l2stats_${tag}_shortw_after.json 2>&1 | tail -n 1 | cut -c1-200)"
  done
}

sec_d15() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  local grace=${D15_GRACE:-60} tag ids4 i
  tag=d15_g$grace
  ids4=$(ids P-exact 4 4)
  group d15 $tag
  local steps=(server "vllm model=$LLAMA" "send name=store sets=P-exact ids=$ids4" settle restart
    "send name=l2a sets=P-exact ids=$ids4 stats=1" "term grace=$grace" server_up)
  local -A on=([1]=lookup_end [2]=lookup_end [3]=retrieve_start)
  for i in 1 2 3; do
    steps+=("send name=mid$i sets=P-exact ids=$ids4 bg=1 errors=1" "wait_log what=${on[$i]} timeout=120"
      "term grace=$grace" wait_bg server_up "vllm_check name=mid$i" "vllm_ensure model=$LLAMA")
  done
  steps+=("send name=after sets=P-exact ids=$ids4" "vllm_check name=end")
  session d15 $tag fail "${steps[@]}"
  report d15 $tag all "--allow-error=${tag}_mid1" "--allow-error=${tag}_mid2" "--allow-error=${tag}_mid3" \
    "--no-hit-check=${tag}_mid1" "--no-hit-check=${tag}_mid2" "--no-hit-check=${tag}_mid3" \
    --outcomes=${tag}_l2a=pipelined --require=${tag}_l2a=pipelined \
    --outcomes=${tag}_after=pipelined --require=${tag}_after=pipelined store l2a mid1 mid2 mid3 after
  progress "d15: shutdowns: $(grep -oE 'stopped \((TERM|kill -9)\) exit=[0-9]+' $S/d15/session_$tag.txt | sort | uniq -c | sed 's/^ *//' | paste -sd';'); \
crash lines in the LMCache log: $(grep -cE "$CRASH" $S/d15/lmcache_$tag.log); \
'Shutting down' lines: $(grep -c 'Shutting down' $S/d15/lmcache_$tag.log)"
}

mkdir -p $S
# shellcheck disable=SC2048
for sec in ${*:-its e2e123 d15}; do
  progress "section $sec started"
  sec_$sec
  progress "section $sec finished"
done
echo "##### sanity DONE $(date -u +%T) $(wait_idle)"
