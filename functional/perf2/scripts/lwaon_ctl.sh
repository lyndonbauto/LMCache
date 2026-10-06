#!/bin/bash
# Control for lwaon: is aon slower today because of the server build/knobs or the box?
set -u
T=/root/lmc-work/LMCache; O=/root/lmc-work/functional/perf2/lwaon
PERF="bash $T/functional/perf/perf.sh"
export DATA_DIR=/mnt/scratch/perf-aero FS_PCT=200
say() { echo "$(date -u +%FT%TZ) lwaon_ctl: $*" | tee -a $O/progress.log; }
while ! grep -q "LWAON DONE" $O/progress.log; do sleep 15; done
say "control: nocache 8k c=1, aon 8k c=1 2 4 with 9c16972132 and with 314564cfb (no knobs)"
PERF_OUT=$O/ctl_nocache LENGTHS=8192 CONCS=1 $PERF precheck nocache > $O/ctl_nocache.txt 2>&1
say "nocache: $(grep -h "^point" $O/ctl_nocache/*/session_*.txt | cut -c1-110)"
PERF_OUT=$O/ctl_9c CONCS="1 2 4" $PERF precheck qpstore:8192 exp_stop > $O/ctl_9c.txt 2>&1
say "aon 9c16972132: $(grep -h "^point" $O/ctl_9c/*/session_*.txt | grep -v store | cut -c1-110 | paste -sd";")"
PERF_OUT=$O/ctl_new PERF_ASD=/root/lmc-work/asd-314564cfb/asd PERF_ASD_ENV="KV_SINK_STATS=1" CONCS="1 2 4" \
  $PERF precheck qpstore:8192 exp_stop > $O/ctl_new.txt 2>&1
say "aon 314564cfb defaults: $(grep -h "^point" $O/ctl_new/*/session_*.txt | grep -v store | cut -c1-110 | paste -sd";")"
say "CTL DONE"
