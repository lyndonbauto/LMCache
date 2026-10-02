#!/bin/bash
# CPU-only preflight for T-E2E-11: 64G kv-sink start, LMCache server with 70 GB pinned L1 + 2 x 5 GiB windows.
# Copied from the box (/root/e2e11/preflight.sh); run on the host with the 70B config in /root/e2e11/.
set -u
cd /root/lmc-work/LMCache
CLONE=/root/lmc-work/LMCache . functional/newstack/kvsink_bp_env.sh
OUT=/root/lmc-work/functional/stage6/gpu/e2e11/preflight; mkdir -p $OUT
cp /root/e2e11/aerospike-kvsink-bp-70b.conf $OUT/; export KVSINK_CONF=$OUT/aerospike-kvsink-bp-70b.conf KVSINK_LOG=$OUT/asd-kvsink.log
free -g | head -2
t0=$(date +%s); bash functional/harness/kvsink_server.sh stop; bash functional/harness/kvsink_server.sh start; echo "kv-sink start $(( $(date +%s) - t0 )) s"
docker exec aero-kvsink-bp asinfo -p 3703 -v "namespace/lmcache" -l 2>/dev/null | grep -E "^(objects|data_total_bytes|data_used_bytes)=" || docker exec aero-kvsink-bp asinfo -p 3700 -v "namespace/lmcache" -l | grep -E "^(objects|data_total_bytes|data_used_bytes)="
grep -iE "version|build|kv-sink" $KVSINK_LOG | head -5
free -g | head -2
J='{"type":"aerospike","hosts":"127.0.0.1:3700","namespace":"lmcache","set_name":"kv_chunks","rdma":{"transport":"RC","device_name":"rxe0","gid_index":1,"window_count":2,"window_bytes":5368709120}}'
cat > $OUT/srv.sh <<EOS
cd /work/LMCache; export LMCACHE_LOG_LEVEL=DEBUG
lmcache server --port 6555 --http-host 127.0.0.1 --http-port 8080 --l1-size-gb 70 --eviction-policy LRU --chunk-size 256 --use-layerwise --no-l1-use-lazy --pipelined-fetch --pipelined-max-chunks 64 --l2-adapter '$J' > /work/functional/stage6/gpu/e2e11/preflight/lmcache.log 2>&1 &
echo \$! > /work/functional/stage6/gpu/e2e11/preflight/lmcache.pid
EOS
t0=$(date +%s)
docker exec lmc-c bash /work/functional/stage6/gpu/e2e11/preflight/srv.sh
pid=$(cat $OUT/lmcache.pid)
for i in $(seq 300); do docker exec lmc-c curl -sf http://localhost:8080/metrics >/dev/null && break; grep -q "startup failed" $OUT/lmcache.log && break; sleep 1; done
echo "lmcache http up after $(( $(date +%s) - t0 )) s"
sleep 5
free -g | head -2
amd-smi metric --mem-usage | grep -m1 USED_VRAM
grep -iE "window|rdma|regist|error|Traceback" $OUT/lmcache.log | cut -c1-250 | head -20
docker exec lmc-c kill $pid
for i in $(seq 60); do docker exec lmc-c kill -0 $pid 2>/dev/null || break; sleep 1; done
docker exec lmc-c kill -0 $pid 2>/dev/null && { echo kill9; docker exec lmc-c kill -9 $pid; }
echo "lmcache stopped"
bash functional/harness/kvsink_server.sh stop
free -g | head -2
