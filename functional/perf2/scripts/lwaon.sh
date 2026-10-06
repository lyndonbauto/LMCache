#!/bin/bash
# perf2 day 2, task 2: look for a setup where lw beats aon. Server
# sriram/kv-sink-batch-prio 314564cfb tuned by env (SINK_ENV), client
# 5a24afdb, LMCache prototype-stage-1b unchanged, lw at QUEUE_PAIRS 16, device
# namespace on the scratch disk. aon and lw run in the same server cycle, on the
# same stored prompts; every point empties L1 first (perf_session.sh).
#
# Steps (STEPS), each twice (REPS "a b"), results in $O/<step>_<rep>/:
#   full   8k and 16k full hits, c = 1 2 4 (qpstore + qplw)
#   part   partial hits (exp2_aon / exp2_lw): cached prefixes 2k, 8k, 16k plus
#          2k or 8k new tokens, c = 1 and 4
#   long   32k, 64k and 128k (130816) full hits at c=1 (cached2, 4 prompts,
#          no long-wait session)
set -u
T=/root/lmc-work/LMCache
O=${LWAON_OUT:-/root/lmc-work/functional/perf2/lwaon}
PERF="bash $T/functional/perf/perf.sh"
STEPS=${STEPS:-full part long}
REPS=${REPS:-a b}
PLACERS=${PLACERS:-32}
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=/root/lmc-work/asd-314564cfb/asd QUEUE_PAIRS=16
export PERF_ASD_ENV=${SINK_ENV:-KV_SINK_STATS=1 KV_SINK_MAX_IN_FLIGHT=128 KV_SINK_PLACE_THREADS=$PLACERS}
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) lwaon: $*" | tee -a $O/progress.log; }
points() { grep -h '^point' $1/*/session_*.txt 2>/dev/null | grep -v store | cut -c1-110 | paste -sd';'; }

say "server env: $PERF_ASD_ENV; queue_pairs $QUEUE_PAIRS"
for rep in $REPS; do
  for s in $STEPS; do
    d=$O/${s}_$rep
    export PERF_OUT=$d
    mkdir -p $d
    say "$s $rep started"
    case $s in
      full)
        CONCS="1 2 4" $PERF precheck qpstore:8192 qplw:8192:16 exp_stop \
          qpstore:16384 qplw:16384:16 exp_stop > $d/run.txt 2>&1 ;;
      part)
        EXP_STORE_LENS="2048 8192 16384" \
        EXP_PART_POINTS="point pre=2048 len=2048 c=1|point pre=2048 len=8192 c=1|point pre=8192 len=2048 c=1|point pre=8192 len=8192 c=1|point pre=16384 len=2048 c=1|point pre=16384 len=8192 c=1|point pre=2048 len=8192 c=4|point pre=8192 len=8192 c=4|point pre=16384 len=8192 c=4" \
          $PERF precheck exp_start exp2_aon exp2_lw exp_stop > $d/run.txt 2>&1 ;;
      long)
        CONCS=1 STORE_COUNT=4 CACHED2_LWWAIT=0 \
          $PERF precheck cached2:32768 cached2:65536 cached2:130816 > $d/run.txt 2>&1 ;;
      *) say "unknown step $s"; continue ;;
    esac
    say "$s $rep done: $(points $d)"
  done
done
say "LWAON DONE"
