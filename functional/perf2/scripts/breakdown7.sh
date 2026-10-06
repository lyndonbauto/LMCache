#!/bin/bash
# perf2 breakdown, day 2 round 6 (Sriram, 2026-10-06 15:29 PT): where asd's
# threads block, and for how long (off-CPU). asd 3b1d52fb3, kvlayers on client
# e8158149, memory namespace, 16 QPs, 32 placers (default), KV_SINK_STATS=1,
# LMCache unchanged.
#
# Configs (CONFIGS, "name:cap:budget_mb"): C128-B4 (defaults) and C512-B16.
# Each is one server cycle with one fill and one kvlayers --qps 16 --duration
# 30 run; the captures start 3 s in and run back to back:
#   1. perf record -e sched:sched_switch -g -p <asd> (5 s, frame pointers),
#      then the same with --call-graph dwarf,8192 (1 s): off-CPU stacks
#   2. perf sched record -a -g (5 s): perf sched timehist -w -g --state,
#      filtered to asd in the parser, and perf sched latency. System-wide,
#      because perf record -p only sees events raised while asd runs, which
#      misses the switch-in that ends each sleep.
# perf data goes to $TMPD on the scratch disk and is deleted after parsing.
# Results: /root/lmc-work/functional/perf2/breakdown7/<config>/.
set -u
T=/root/lmc-work/LMCache
NEW=/root/lmc-work/asd-3b1d52fb3
O=/root/lmc-work/functional/perf2/breakdown7
R=${R:-"python3 $O/../breakdown7_report.py"}
TMPD=/mnt/scratch/breakdown7-tmp
PERF="bash $T/functional/perf/perf.sh"
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
CONFIGS=${CONFIGS:-C128-B4:128:4 C512-B16:512:16}
RUN_S=30
EXCERPT=2000
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O $TMPD
say() { echo "$(date -u +%FT%TZ) breakdown7: $*" | tee -a $O/progress.log; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
now() { date +%s.%N; }

# capture <dir> <asd pid>
capture() {
  local d=$1 p=$2
  [ -n "${NOCAP:-}" ] && { echo "nocap $(now)" > $d/times.txt; return; }
  echo "offcpu_start $(now)" > $d/times.txt
  perf record -e sched:sched_switch -g -p $p -o $TMPD/offcpu_fp.data -- sleep 5 > $d/offcpu_fp_record.txt 2>&1
  echo "offcpu_end $(now)" >> $d/times.txt
  perf record -e sched:sched_switch --call-graph dwarf,8192 -p $p -o $TMPD/offcpu_dwarf.data \
    -- sleep 1 > $d/offcpu_dwarf_record.txt 2>&1
  echo "sched_start $(now)" >> $d/times.txt
  perf sched record -a -g -o $TMPD/sched.data -- sleep 5 > $d/sched_record.txt 2>&1
  echo "sched_end $(now)" >> $d/times.txt
}

# post <dir> <asd pid>
post() {
  local d=$1 p=$2
  perf script -i $TMPD/offcpu_fp.data -F comm,tid,cpu,time,trace,ip,sym 2>/dev/null > $TMPD/offcpu_fp.txt
  perf script -i $TMPD/offcpu_dwarf.data -F comm,tid,cpu,time,trace,ip,sym 2>/dev/null > $TMPD/offcpu_dwarf.txt
  perf script -i $TMPD/sched.data --pid $p -F comm,tid,cpu,time,event,trace,ip,sym 2>/dev/null \
    > $TMPD/sched_script.txt
  perf sched timehist -i $TMPD/sched.data -w -g --state -p $p 2>/dev/null > $TMPD/timehist.txt
  for f in offcpu_fp offcpu_dwarf sched_script timehist; do
    head -n $EXCERPT $TMPD/$f.txt > $d/${f}_excerpt.txt
  done
  $R roles $TMPD/offcpu_dwarf.txt $TMPD/offcpu_fp.txt > $d/tid_roles.txt
  $R stacks $d/tid_roles.txt < $TMPD/offcpu_dwarf.txt > $d/dwarf_stacks.md
  $R sched $d $TMPD/timehist.txt $TMPD/sched_script.txt > $d/tables.md
  perf sched latency -i $TMPD/sched.data --sort max 2>/dev/null | head -n 60 > $d/sched_latency.txt
  if [ -n "${KEEP:-}" ]; then
    mkdir -p $d/raw && mv $TMPD/*.txt $d/raw/; say "$(basename $d): raw text kept in $d/raw"
  fi
  rm -f $TMPD/*.data $TMPD/*.txt $TMPD/*.data.old
  say "$(basename $d): parsed"
}

# config <name> <cap> <budget MiB>; returns 1 if the server or rows fail.
config() {
  local c=$1 cap=$2 mb=$3 d=$O/$1 bg p ok=0
  mkdir -p $d
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF
  export PERF_ASD_ENV="KV_SINK_STATS=1 KV_SINK_MAX_IN_FLIGHT=$cap KV_SINK_PATH_BUDGET_MB=$mb"
  $PERF exp_start > $d/perf_start.txt 2>&1
  if ! grep -q 'started on' $d/progress.log; then
    say "$c: server did not start"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1
  fi
  say "$c: $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  p=$(pgrep -x asd | head -n 1)
  ls /proc/$p/task | wc -l > $d/asd_threads.txt
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  grep -q 'failed rows 0' $d/kvl_fill.txt || { say "$c: fill failed rows"; ok=1; }
  if [ $ok = 0 ]; then
    kvl --qps 16 --duration $RUN_S > $d/kvl.txt 2>&1 & bg=$!
    sleep 3; capture $d $p; wait $bg
    say "$c: $(grep -E '^stream:' $d/kvl.txt)"
    grep -qE 'failed rows 0$' $d/kvl.txt || say "$c: WARNING client rows failed (perf perturbation?)"
  fi
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  grep "stats:" $d/asd-kvsink-bp-perf.log > $d/stats_lines.txt
  [ $ok = 0 ] && [ -z "${NOCAP:-}" ] && post $d $p
  rm -f $TMPD/*.data $TMPD/*.txt $TMPD/*.data.old
  return $ok
}

docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "asd is running; stopping"; exit 1; }
[ -x $NEW/asd ] || { say "no 3b1d52fb3 binary"; exit 1; }
for spec in $CONFIGS; do
  IFS=: read -r name cap mb <<< "$spec"
  config $name $cap $mb || { say "$name failed; skipping the rest"; break; }
done
rmdir $TMPD 2>/dev/null
say "server stopped, data deleted"
$R report $O > $O/report.md
say "BREAKDOWN7 DONE"
