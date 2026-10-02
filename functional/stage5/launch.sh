#!/bin/bash
# launch.sh <log-name> <section...>: run stage5.sh on the host, detached, with
# the new kv-sink stack's env (aero-kvsink-bp on 127.0.0.1:3700). Extra env
# (FLT05_CASES, FLT01_PATHS, FLT06_PATHS, SEC7_PATHS, STOP_GRACE) passes through.
NAME=$1; shift
cd /root/lmc-work/LMCache || exit 1
CLONE=/root/lmc-work/LMCache . functional/newstack/kvsink_bp_env.sh
mkdir -p /root/lmc-work/functional/stage5
cd /root/lmc-work/functional/stage5 || exit 1
setsid nohup bash /root/lmc-work/LMCache/functional/stage5/stage5.sh "$@" > "$NAME.txt" 2>&1 < /dev/null &
echo "started $NAME pid $!"
