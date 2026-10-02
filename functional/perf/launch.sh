#!/bin/bash
# launch.sh <log-name> <section...>: run perf.sh on the host, detached. Extra
# env (LENGTHS, CONCS, CAP, WINDOW_COUNT, L1_GEN_GB, MIN_FREE_GB, STOP_GRACE,
# STORE_CONC) passes through.
NAME=$1; shift
mkdir -p /root/lmc-work/functional/perf
cd /root/lmc-work/functional/perf || exit 1
setsid nohup bash /root/lmc-work/LMCache/functional/perf/perf.sh "$@" > "$NAME.txt" 2>&1 < /dev/null &
echo "started $NAME pid $!"
