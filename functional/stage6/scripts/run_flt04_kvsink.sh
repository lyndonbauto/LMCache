#!/bin/bash
# T-FLT-04 (kv-sink half) and the adapter-level T-RDMA-05 check on the 3-node
# kv-sink cluster (3400/3410/3420). Runs on the host.
#  1. kv_sink_fanout_probe registers on all nodes, waits on a gate; node 2 is
#     restarted (and warmed); the probe deregisters (node 2 forgot its
#     region) and registers all nodes again.
#  2. cfg_probe (item 2) with hosts=127.0.0.1:3400: what LMCache's adapter
#     does with pipelined RDMA on a 3-node cluster.
set -u
OUT=/root/lmc-work/functional/stage6/logs
H=/root/lmc-work/LMCache-cpu/functional/harness
GATE=/root/lmc-work/aero-cluster/kvsink/restart.gate
rm -f $GATE
docker exec -d lmc-c bash -c "timeout 600 /work/LMCache-cpu/tests/v1/distributed/rdma/build/kv_sink_fanout_probe --host 127.0.0.1 --port 3400 --device rxe0 --gid 1 --restart-gate /work/aero-cluster/kvsink/restart.gate > /work/functional/stage6/logs/flt04_kvsink_probe.txt 2>&1"
for _ in $(seq 1 60); do grep -q "waiting for" $OUT/flt04_kvsink_probe.txt 2>/dev/null && break; sleep 1; done
$H/kvsink_cluster.sh restart-node 2 > $OUT/flt04_kvsink_restart.txt 2>&1
touch $GATE
for _ in $(seq 1 120); do grep -q "^RESULT" $OUT/flt04_kvsink_probe.txt && break; sleep 1; done
grep -E "kv-sink: (registered|deregister|region)|region .* (not found|unknown)|kv-sink-deregister|kv-sink-register" /root/lmc-work/aero-cluster/kvsink/n2/asd.log | tail -12 > $OUT/flt04_kvsink_n2_serverlog.txt

sed 's/127.0.0.1:3100/127.0.0.1:3400/; s/set_name="cfg_probe"/set_name="cfg_probe_e4"/' \
  /root/lmc-work/functional/item2/scripts/cfg_probe.py > /root/lmc-work/functional/stage6/scripts/rdma05_adapter_probe.py
docker exec -w /work/LMCache-cpu lmc-c env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= PYTHONDONTWRITEBYTECODE=1 \
  nice -n 19 timeout 120 python /work/functional/stage6/scripts/rdma05_adapter_probe.py 1 268435456 4 33554432 \
  > $OUT/rdma05_adapter_probe.txt 2>&1
echo "exit=$?" >> $OUT/rdma05_adapter_probe.txt
