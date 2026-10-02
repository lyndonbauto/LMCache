#!/bin/bash
# launch.sh <log-name> <section...>: run stage4.sh on the host, detached, with
# the new kv-sink stack's env (aero-kvsink-bp on 127.0.0.1:3700). Extra env
# (TWO_VLLM_UTIL, P10_POLICY, PIPE08_ROUNDS, STOP_GRACE) passes through.
NAME=$1; shift
cd /root/lmc-work/LMCache || exit 1
CLONE=/root/lmc-work/LMCache . functional/newstack/kvsink_bp_env.sh
mkdir -p /root/lmc-work/functional/stage4
cd /root/lmc-work/functional/stage4 || exit 1
setsid nohup bash /root/lmc-work/LMCache/functional/stage4/stage4.sh "$@" > "$NAME.txt" 2>&1 < /dev/null &
echo "started $NAME pid $!"
