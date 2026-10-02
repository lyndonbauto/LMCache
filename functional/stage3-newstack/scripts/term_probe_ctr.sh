#!/bin/bash
# term_probe_ctr.sh <dir> <name> <flags...>: CPU-only LMCache server on side ports;
# SIGTERM once up, mark READY after 3 s, record how long it takes to exit (max 90 s).
D=$1; N=$2; shift 2; mkdir -p $D
source /work/LMCache/functional/harness/loopback_env.sh
export HIP_VISIBLE_DEVICES='' CUDA_VISIBLE_DEVICES='' PYTHONDONTWRITEBYTECODE=1 LMCACHE_LOG_LEVEL=DEBUG
rm -f $D/$N.ready $D/$N.result
lmcache server --port 6655 --http-host 127.0.0.1 --http-port 8180 --prometheus-port 9190 \
  --l1-size-gb 6 --eviction-policy LRU --chunk-size 256 --use-layerwise "$@" > $D/$N.log 2>&1 &
P=$!
for _ in $(seq 120); do curl -sf http://127.0.0.1:8180/metrics >/dev/null && break; sleep 1; done
t0=$(date +%s.%N); kill $P; sleep 3; echo $P > $D/$N.ready
for _ in $(seq 870); do kill -0 $P 2>/dev/null || break; sleep 0.1; done
if kill -0 $P 2>/dev/null; then kill -9 $P; how="still alive after 90 s, kill -9"; else how="exited"; fi
wait $P; rc=$?
echo "$N: $how rc=$rc after $(echo "$(date +%s.%N) - $t0" | bc) s" > $D/$N.result
