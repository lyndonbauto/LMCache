#!/bin/bash
# perf2 follow-up A (D-27): the worker's layer wait as a no-progress timeout
# (commit 2eefa049). Reruns the cached sections at c = 16 and 32 with the
# default 5 s wait, where the sweep's lw stopped the engine at c = 32.
# Results: /root/lmc-work/functional/perf2/fixa/.
set -u
T=/root/lmc-work/LMCache
QP=${QP:-16}
export PERF_OUT=/root/lmc-work/functional/perf2/fixa DATA_DIR=/mnt/scratch/perf-aero FS_PCT=200 QUEUE_PAIRS=$QP
export CONCS=${CONCS:-"16 32"}
mkdir -p $PERF_OUT
echo "$(date -u +%FT%TZ) fixa: queue_pairs $QP, concurrencies $CONCS" >> $PERF_OUT/progress.log
bash $T/functional/perf/perf.sh precheck cached:8192 cached:16384
echo "$(date -u +%FT%TZ) fixa: FIXA DONE" >> $PERF_OUT/progress.log
