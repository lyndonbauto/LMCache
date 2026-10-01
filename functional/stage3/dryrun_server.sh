#!/bin/bash
# CPU-only dry run of the Stage 3 / Stage 5 plumbing, on the host. No vLLM,
# no GPU, and none of a GPU session's ports (8000, 6555, 8080).
#   1. kv-sink server: restart and warm (host_actions.sh kvsink_restart)
#   2. every LMCache server configuration stage3.sh uses starts on the CPU
#      against it, with the Aerospike RDMA adapter on (dryrun_lmcache.sh)
#   3. the armed freeze: a request armed on a fake LMCache log freezes the
#      kv-sink server when the trigger line appears, and the server answers
#      again afterwards
#   4. kv-sink server stopped
# Usage: TREE_HOST=... TREE_CTR=... dryrun_server.sh
set -u
TREE_HOST=${TREE_HOST:-/root/lmc-work/LMCache}
TREE_CTR=${TREE_CTR:-/work/LMCache}
export PY314=/root/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14
D=/root/lmc-work/functional/stage3/dry
DC=/work/functional/stage3/dry
mkdir -p $D
# shellcheck source=../harness/host_actions.sh
source "$TREE_HOST/functional/harness/host_actions.sh"
rc=0

echo "== 1. kv-sink restart and warm-up"
kvsink_restart $D/kvsink || exit 1

echo "== 2. LMCache server configurations (CPU)"
L=32 G=18
j() {  # j <cap> <chunk MiB> [adapter-extra]
  echo "{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:3100\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"${3:+,$3},\"rdma\":{\"transport\":\"RC\",\"device_name\":\"rxe0\",\"gid_index\":1,\"window_count\":2,\"window_bytes\":$(( $1 * $2 * 1048576 ))}}"
}
run() { docker exec -w "$TREE_CTR" lmc-c bash "$TREE_CTR/functional/stage3/dryrun_lmcache.sh" $DC "$@" || rc=1; }
run llama_cap64 --pipelined-fetch --pipelined-max-chunks 64 --l2-adapter "$(j 64 $L)"
run llama_cap4 --pipelined-fetch --pipelined-max-chunks 4 --l2-adapter "$(j 4 $L)"
run llama_cap4_rec256k --pipelined-fetch --pipelined-max-chunks 4 --l2-adapter "$(j 4 $L '"max_record_bytes":262144')"
run gptoss_cap4 --separate-object-groups --pipelined-fetch --pipelined-max-chunks 4 --l2-adapter "$(j 4 $G)"
run llama_cap4_recompute_fault --pipelined-fetch --pipelined-max-chunks 4 \
  --l2-adapter "{\"type\":\"fault_inject\",\"gap_tail_ratios\":[0.5],\"inner\":$(j 4 $L)}"

echo "== 3. armed freeze of the kv-sink server"
: > $D/lmcache_dryfreeze.log
rm -f $D/HOSTREQ_dryfreeze_1 $D/HOSTDONE_dryfreeze_1 $D/HOSTFAULT_dryfreeze_1.txt
watch_host $D & watcher=$!
echo "action=freeze_kvsink secs=1 on=lookup_end" > $D/HOSTREQ_dryfreeze_1
for _ in $(seq 50); do [ -f $D/HOSTDONE_dryfreeze_1 ] && break; sleep 0.1; done
echo "answer: $(cat $D/HOSTDONE_dryfreeze_1 2>/dev/null)"
sleep 0.5
echo "[x] LMCache DEBUG: MP lookup/prefetch end: session=dry-1 found_count=4" >> $D/lmcache_dryfreeze.log
sleep 0.3
state=$(awk '{print $3}' "/proc/$(kvsink_host_pid)/stat")
echo "kv-sink process state 0.3 s after the trigger: $state (T = stopped)"
[ "$state" = T ] || rc=1
sleep 1.5
echo "fault record: $(cat $D/HOSTFAULT_dryfreeze_1.txt 2>/dev/null)"
docker exec aero-kvsink python -c "
import aerospike
c = aerospike.client({'hosts': [('127.0.0.1', 3100)]}).connect()
print('kv-sink answers after the freeze:', c.info_random_node('status').strip())
c.close()" || rc=1
kill $watcher 2>/dev/null

echo "== 4. stop the kv-sink server"
KVSINK_CONF=$TREE_HOST/functional/configs/aerospike-kvsink.conf KVSINK_LOG=$D/kvsink/asd-kvsink.log \
  bash "$TREE_HOST/functional/harness/kvsink_server.sh" stop
echo "dry run rc=$rc"
exit $rc
