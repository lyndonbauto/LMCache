#!/bin/bash
# CAP_IPC_LOCK is dropped so RLIMIT_MEMLOCK applies (lmc-c adds the cap).
# Usage: run_cfg_probe.sh <log-name> <memlock-bytes|unlimited> <gid> <l1_bytes> <count> <window_bytes>
set -u
NAME=$1; ML=$2; shift 2
OUT=/root/lmc-work/functional/item2/logs
docker exec -w /work/LMCache-cpu lmc-c env WRITE_TTL=${WRITE_TTL:-600} HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= PYTHONDONTWRITEBYTECODE=1 \
  setpriv --bounding-set -ipc_lock --inh-caps -ipc_lock \
  prlimit --memlock=$ML:$ML nice -n 19 timeout 120 python /work/functional/item2/scripts/cfg_probe.py "$@" > $OUT/$NAME.txt 2>&1
echo "exit=$?" >> $OUT/$NAME.txt
grep -E "RLIMIT|gid_index=|STARTUP|pipelined node|raised|ERROR|WARN|exit=|rror" $OUT/$NAME.txt | cut -c1-400 | head -30
