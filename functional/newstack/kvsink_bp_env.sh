#!/bin/bash
# Source on the host to point functional/harness/kvsink_server.sh at the new
# kv-sink server (sriram/kv-sink-batch-prio 046e8558d) on 127.0.0.1:3700-3703:
#   . functional/newstack/kvsink_bp_env.sh
#   functional/harness/kvsink_server.sh start
# CLONE is the LMCache tree on the host whose configs/ to use.
CLONE=${CLONE:-/root/lmc-work/LMCache-1a}
export KVSINK_CTR=${KVSINK_CTR:-aero-kvsink-bp}
export KVSINK_ASD=/root/lmc-work/aerospike-server-kvsink-bp/target/Linux-x86_64/bin/asd
export KVSINK_CONF=$CLONE/functional/configs/aerospike-kvsink-bp.conf
export KVSINK_LOG=${KVSINK_LOG:-/root/lmc-work/functional/newstack/logs/asd-kvsink-bp.log}
export KVSINK_DATA=/root/lmc-work/aerospike-kvsink-bp-data/work
export KVSINK_PORT=3700
export KVSINK_RDMA_DEVICE=rxe0
export KVSINK_GID_INDEX=1
# kvsink_restart (host_actions.sh): the server has no lazy registration to warm.
export KVSINK_WARM=0
# stage3.sh: the RC path MTU (rxe0's active MTU, which this client and server
# negotiate; the old server sent 1 KiB packets). KVSINK_PORT above is also
# stage3.sh's L2 port.
export RXE_PKT=4096
