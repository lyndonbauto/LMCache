#!/bin/bash
# launch.sh <log-name> <section...>: run stage6gpu.sh on the host, detached,
# with the new kv-sink stack's env (aero-kvsink-bp on 127.0.0.1:3700). Extra
# env (S6_CAP, TWO_VLLM_UTIL, SHR02_ROUNDS, SHR02_RF, EVT04_L1_GB, STOP_GRACE,
# REAP_WAIT) passes through.
NAME=$1; shift
cd /root/lmc-work/LMCache || exit 1
CLONE=/root/lmc-work/LMCache . functional/newstack/kvsink_bp_env.sh
mkdir -p /root/lmc-work/functional/stage6/gpu
cd /root/lmc-work/functional/stage6/gpu || exit 1
setsid nohup bash /root/lmc-work/LMCache/functional/stage6/stage6gpu.sh "$@" > "$NAME.txt" 2>&1 < /dev/null &
echo "started $NAME pid $!"
