#!/bin/bash
# Control 2 for lwaon: no-cache TTFT at the partial-hit prompts (cached + new
# tokens as one uncached prompt), so a partial-hit lw win over aon can be
# checked against plain recompute. No kv-sink server.
set -u
T=/root/lmc-work/LMCache; O=/root/lmc-work/functional/perf2/lwaon
PERF="bash $T/functional/perf/perf.sh"
say() { echo "$(date -u +%FT%TZ) lwaon_ctl2: $*" | tee -a $O/progress.log; }
say "control 2: nocache at the partial-hit prompts"
PERF_OUT=$O/ctl_nocache_part \
  EXP_PART_POINTS="point pre=2048 len=2048 c=1|point pre=2048 len=8192 c=1|point pre=8192 len=2048 c=1|point pre=8192 len=8192 c=1|point pre=16384 len=2048 c=1|point pre=16384 len=8192 c=1|point pre=2048 len=8192 c=4|point pre=8192 len=8192 c=4|point pre=16384 len=8192 c=4" \
  $PERF precheck exp_nocache > $O/ctl_nocache_part.txt 2>&1
say "nocache partial: $(grep -h "^point" $O/ctl_nocache_part/*/session_*.txt | cut -c1-110 | paste -sd";")"
say "CTL2 DONE"
