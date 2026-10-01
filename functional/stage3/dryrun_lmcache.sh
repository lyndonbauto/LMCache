#!/bin/bash
# CPU-only dry run of one Stage 3 LMCache server configuration, inside lmc-c:
# start `lmcache server` with no GPU visible, on ports clear of a GPU session
# (6655, HTTP 8180, Prometheus 9190), wait for its HTTP endpoint, check that
# the Aerospike adapter came up with RDMA on, then stop it by PID.
# Usage: dryrun_lmcache.sh <out-dir> <name> <server flags...>
# Prints one line: "<name>: ok|FAIL <details>".
set -u
OUT=$1; NAME=$2; shift 2
mkdir -p "$OUT"
LOG=$OUT/lmcache_dry_$NAME.log
export HIP_VISIBLE_DEVICES='' CUDA_VISIBLE_DEVICES='' PYTHONDONTWRITEBYTECODE=1 LMCACHE_LOG_LEVEL=DEBUG
lmcache server --port 6655 --http-host 127.0.0.1 --http-port 8180 --prometheus-port 9190 \
  --l1-size-gb 6 --eviction-policy LRU --chunk-size 256 --use-layerwise "$@" > "$LOG" 2>&1 &
PID=$!
up=0
for _ in $(seq 120); do
  curl -sf http://127.0.0.1:8180/metrics > /dev/null && { up=1; break; }
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
kill "$PID" 2>/dev/null
for _ in $(seq 10); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
kill -9 "$PID" 2>/dev/null
wait "$PID" 2>/dev/null
adapter=$(grep -oE 'Created Aerospike L2 adapter: [^(]*\(workers=[0-9]+, rdma=[A-Z]+\)' "$LOG" | head -n 1)
errors=$(grep -cE 'Traceback|ERROR' "$LOG")
if [ "$up" = 1 ] && echo "$adapter" | grep -q 'rdma=RC' && [ "$errors" = 0 ]; then
  echo "$NAME: ok ($adapter)"
else
  echo "$NAME: FAIL up=$up errors=$errors adapter='$adapter'; last lines:"; tail -n 5 "$LOG"
fi
