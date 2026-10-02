#!/bin/bash
# launch.sh <log-name> <driver> <sections...>: run a driver on the host with the new-stack env.
NAME=$1; DRV=$2; shift 2
cd /root/lmc-work/LMCache
CLONE=/root/lmc-work/LMCache . functional/newstack/kvsink_bp_env.sh
export STAGE_DIR=stage3-newstack
mkdir -p /root/lmc-work/functional/stage3-newstack
cd /root/lmc-work/functional/stage3-newstack
setsid nohup bash /root/lmc-work/LMCache/$DRV "$@" > $NAME.txt 2>&1 < /dev/null &
echo "started $NAME pid $!"
