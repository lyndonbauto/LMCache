#!/bin/bash
# perf2 step 6: the full sweeps at the best queue_pairs from step 4 (QP), with
# the new server, as functional/perf phase 1 and LW-EXPERIMENTS.md:
#   cached:<L>   aon control + lw (default 5 s wait), c = 1..32
#   lwwait:<L>   lw with the layerwise wait raised to 600 s, c = 1..32
#   exp_*        E4 partial hits (2k + 8k, 8k + 8k; c = 1, 4) for nocache,
#                aon, tcp-lw (E2) and lw, on one 8k data file
# for L = 8192 and 16384. Results: /root/lmc-work/functional/perf2/sweep/.
set -u
T=/root/lmc-work/LMCache
QP=${QP:?QP (queue_pairs) required}
export PERF_OUT=/root/lmc-work/functional/perf2/sweep DATA_DIR=/mnt/scratch/perf-aero FS_PCT=200 QUEUE_PAIRS=$QP
mkdir -p $PERF_OUT
echo "$(date -u +%FT%TZ) sweep: queue_pairs $QP" >> $PERF_OUT/progress.log
bash $T/functional/perf/perf.sh precheck cached:8192 lwwait:8192 cached:16384 lwwait:16384 \
  exp_start exp_aon exp_nocache exp_tcplw exp_lw exp_stop
echo "$(date -u +%FT%TZ) sweep: SWEEP DONE" >> $PERF_OUT/progress.log
