#!/bin/bash
# S1 diagnostic (pipe05 wrong output under recompute): rerun pipe05 with
# vLLM async scheduling off, in its own directory, to tell whether the token
# sampled in the step whose layerwise load failed leaks into the output.
# Usage: s1_diag.sh   (host; same requirements as stage3.sh)
set -u
# shellcheck source=stage3.sh
source "${TREE_HOST:-/root/lmc-work/LMCache}/functional/stage3/stage3.sh"
SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
tag=pipe05_noasync
group pipe05_noasync $tag
VLLM_EXTRA="--no-async-scheduling" session pipe05_noasync $tag recompute server "vllm model=$LLAMA" \
  "send name=store sets=P-exact ids=P-exact-10" settle restart \
  "l2seg prompt=P-exact-10 chunk=1 seg=5" \
  "send name=probe sets=P-exact ids=P-exact-10 stats=1" "send name=again sets=P-exact ids=P-exact-10"
report pipe05_noasync $tag all --outcomes=${tag}_probe=fell_back,failed \
  --outcomes=${tag}_again=pipelined,fell_back,failed,not_deferred store probe again
progress "s1_diag: async scheduling in vLLM log: $(grep -oE "async_scheduling=[A-Za-z]+" $S/pipe05_noasync/vllm_${tag}_*.log | sort -u | paste -sd' ')"
KVSINK_CONF=$TREE_HOST/functional/configs/aerospike-kvsink.conf KVSINK_LOG=$KVDIR/asd-kvsink.log \
  bash "$TREE_HOST/functional/harness/kvsink_server.sh" stop
echo "##### s1_diag DONE $(date -u +%T) $(wait_idle)"
