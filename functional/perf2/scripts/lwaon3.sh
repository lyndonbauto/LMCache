#!/bin/bash
# perf2 day 3: D-27 A/B. The LW-VS-AON grid on server 3f3940e42 (defaults,
# KV_SINK_STATS=1), client 5a24afdb, lw at QUEUE_PAIRS 16, device namespace on
# the scratch disk, for the LMCache build currently installed in lmc-c
# (prototype-stage-1b or prototype-stage-1c; the caller builds it first).
#
# Usage: lwaon3.sh <label>    results in $O/<label>/<step>/
# Steps (STEPS, in order; one session each, so a host power-off loses at most
# one step and the rest can be rerun with STEPS):
#   store8   start the server, store 32 8k prompts, aon full hits c = 1-32
#   lw8      lw full hits at 8k, c = 1-32 (needs store8's server)
#   store16, lw16   the same at 16k (store16 replaces the 8k data file)
#   resume8, resume16   restart the server on store8's / store16's data file
#            after a reboot, instead of storing again
#   stop     stop the server, delete the data file
#   part     partial hits (exp2_aon / exp2_lw) on 32 stored 8k prefixes (plus
#            4 of 2k and 16k): pre=8192 len=8192 at c = 1 2 4 8 16 32, and the
#            day-2 points pre=2048 / 16384 len=8192 at c = 1 and 4; own data
#            file, stopped and deleted at the end
set -u
LABEL=${1:?usage: lwaon3.sh <label>}
T=/root/lmc-work/LMCache
O=${LWAON_OUT:-/root/lmc-work/functional/perf2/lwaon3}/$LABEL
PERF="bash $T/functional/perf/perf.sh"
STEPS=${STEPS:-store8 lw8 store16 lw16 stop part}
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=/root/lmc-work/asd-3f3940e42/asd QUEUE_PAIRS=16
export PERF_ASD_ENV=${SINK_ENV:-KV_SINK_STATS=1}
export CONCS="1 2 4 8 16 32"
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) lwaon3 $LABEL: $*" | tee -a $O/progress.log; }
points() { grep -h '^point' $1/*/session_*.txt 2>/dev/null | grep -v store | cut -c1-110 | paste -sd';'; }

say "LMCache $(git -C $T log --oneline -1 | cut -c1-60); server env: $PERF_ASD_ENV; queue_pairs $QUEUE_PAIRS"
for s in $STEPS; do
  d=$O/$s
  export PERF_OUT=$d
  mkdir -p $d
  say "$s started"
  case $s in
    store8) $PERF precheck qpstore:8192 > $d/run.txt 2>&1 ;;
    lw8) $PERF qplw:8192:16 > $d/run.txt 2>&1 ;;
    store16) $PERF qpstore:16384 > $d/run.txt 2>&1 ;;
    lw16) $PERF qplw:16384:16 > $d/run.txt 2>&1 ;;
    resume8) $PERF resume:8192 > $d/run.txt 2>&1 ;;
    resume16) $PERF resume:16384 > $d/run.txt 2>&1 ;;
    stop) $PERF exp_stop > $d/run.txt 2>&1 ;;
    part)
      EXP_FS_PCT=300 EXP_CHECK_N=32 \
      EXP_STORES="store len=8192 ids=0-31 conc=8|store len=2048 ids=0-3 conc=4|store len=16384 ids=0-3 conc=4" \
      EXP_PART_POINTS="point pre=8192 len=8192 c=1|point pre=8192 len=8192 c=2|point pre=8192 len=8192 c=4|point pre=8192 len=8192 c=8|point pre=8192 len=8192 c=16|point pre=8192 len=8192 c=32|point pre=2048 len=8192 c=1|point pre=2048 len=8192 c=4|point pre=16384 len=8192 c=1|point pre=16384 len=8192 c=4" \
        $PERF precheck exp_start exp2_aon exp2_lw exp_stop > $d/run.txt 2>&1 ;;
    *) say "unknown step $s"; continue ;;
  esac
  say "$s done: $(points $d)"
done
say "LWAON3 DONE"
