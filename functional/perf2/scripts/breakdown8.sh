#!/bin/bash
# perf2 breakdown, day 2 round 7 (Sriram, 2026-10-06 16:08 PT): server
# 55d6ae8d8 posts RC writes without the per-QP post lock (round 6 found
# placers asleep on it). kvlayers on client e8158149, memory namespace, 16 QPs,
# 512 KiB, 32 placers (default), KV_SINK_STATS=1, LMCache unchanged.
#
# Steps (STEPS):
#   build   asd 55d6ae8d8 in place over 9c16972132 (as breakdown6.sh), the
#           binary kept in $NEW, tree and binary restored
#   runs    per config in CONFIGS ("name:cap:budget_mb"): a server cycle, one
#           fill, then three kvlayers --qps 16 --duration 25 runs, each
#           measured 3 s in: <name>/clean (mpstat 10 s), <name>/bt
#           (breakdown5's bpftrace exits, 10 s) and <name>/offcpu
#           (breakdown7's capture: sched_switch dwarf 1 s for the thread ->
#           role map, then perf sched record -a -g 5 s; parsed by
#           breakdown7_report.py after the server stops)
#   lw      lw 8k c=1 timeline at 16 QPs with the faster config (clean-run
#           kvlayers GiB/s), as breakdown3.sh's run 4: prompts 0-3 stored by
#           LMCache, E3 print patch applied and reverted, tree checked clean
#   ab      (not in the default STEPS) same-session A/B: clean runs
#           alternating 3b1d52fb3 and 55d6ae8d8, see step_ab
# Runs stay at 25 s: longer streams fail the high layers' rows at the end
# (strict per-sink priority under nonstop resubmission, Sriram 16:08).
# Results: /root/lmc-work/functional/perf2/breakdown8/.
set -u
T=/root/lmc-work/LMCache
SRC=/root/lmc-work/aerospike-server-kvsink-bp
ASD=$SRC/target/Linux-x86_64/bin/asd
ASD_ORIG_MD5=0b953fb421486d003e28ccc90dde2d7b
PREV_MD5=$(md5sum < /root/lmc-work/asd-3b1d52fb3/asd | cut -c1-32)
NEW=/root/lmc-work/asd-55d6ae8d8
SRC_TAR=$NEW/src-55d6ae8d8.tar
O=/root/lmc-work/functional/perf2/breakdown8
R="python3 $O/../breakdown8_report.py"
R7="python3 $O/../breakdown7_report.py"
BT=$O/../breakdown5/exits.bt
TMPD=/mnt/scratch/breakdown8-tmp
PERF="bash $T/functional/perf/perf.sh"
E3_PATCH=/root/lmc-work/functional/perf2/e3_pump_patch.py
PUMP=lmcache/v1/layerwise/pump.py
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
KO=/root/rxe-build/v6.11/rdma_rxe.ko
STEPS=${STEPS:-build runs lw}
CONFIGS=${CONFIGS:-N128:128:4 N512:512:16}
RUN_S=25
WIN_S=10
EXCERPT=2000
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown8: $*" | tee -a $O/progress.log; }
clean_tree() { [ -z "$(git -C $T status --porcelain -- lmcache csrc)" ]; }
touch_dependents() { grep -rl --include='*.c' 'kv_sink.h' $SRC/as/src | xargs touch; }
make_asd() { docker exec aero-kvsink-bp bash -c "cd $SRC && nice -n 19 make -j16" >> $O/build.txt 2>&1; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
now() { date +%s.%N; }

step_build() {
  local files rc
  if [ -x $NEW/asd ]; then say "55d6ae8d8 binary already built"; return 0; fi
  files=$(tar tf $SRC_TAR | grep -v '/$')
  mkdir -p $NEW/orig
  [ "$(md5sum < $ASD | cut -c1-32)" = $ASD_ORIG_MD5 ] || { say "!! tree binary is not 9c16972132"; return 1; }
  for f in $files; do mkdir -p $NEW/orig/$(dirname $f); cp -a $SRC/$f $NEW/orig/$f || return 1; done
  cp -a $ASD $NEW/asd.9c16972132 || return 1
  tar xf $SRC_TAR -C $SRC && (cd $SRC && touch $files) && touch_dependents
  say "building 55d6ae8d8 in place"
  make_asd && cp -a $ASD $NEW/asd.new && mv $NEW/asd.new $NEW/asd
  rc=$?
  for f in $files; do cp -a $NEW/orig/$f $SRC/$f; done
  touch_dependents
  make_asd
  cp -a $NEW/asd.9c16972132 $ASD
  if [ "$(md5sum < $ASD | cut -c1-32)" != $ASD_ORIG_MD5 ]; then say "!! 9c16972132 binary not restored"; return 1; fi
  for f in $files; do cmp -s $SRC/$f $NEW/orig/$f || { say "!! $f not restored"; return 1; }; done
  say "tree and 9c16972132 binary restored ($ASD_ORIG_MD5)"
  [ $rc -eq 0 ] || { say "55d6ae8d8 build failed; see build.txt"; rm -f $NEW/asd; return 1; }
  strings $NEW/asd | grep -q 'MiB per path' || { say "!! new binary has no path budget"; rm -f $NEW/asd; return 1; }
  [ "$(md5sum < $NEW/asd | cut -c1-32)" != "$PREV_MD5" ] || { say "!! new binary equals 3b1d52fb3"; rm -f $NEW/asd; return 1; }
  say "55d6ae8d8 binary $(md5sum < $NEW/asd | cut -c1-32)"
}

# capture <dir> <asd pid>: breakdown7's off-CPU capture, perf data in $TMPD.
capture() {
  local d=$1 p=$2
  perf record -e sched:sched_switch --call-graph dwarf,8192 -p $p -o $TMPD/offcpu_dwarf.data \
    -- sleep 1 > $d/offcpu_dwarf_record.txt 2>&1
  echo "sched_start $(now)" > $d/times.txt
  perf sched record -a -g -o $TMPD/sched.data -- sleep 5 > $d/sched_record.txt 2>&1
  echo "sched_end $(now)" >> $d/times.txt
}

# post <dir> <asd pid>: breakdown7's parsing, then the perf data is deleted.
post() {
  local d=$1 p=$2 f
  perf script -i $TMPD/offcpu_dwarf.data -F comm,tid,cpu,time,trace,ip,sym 2>/dev/null > $TMPD/offcpu_dwarf.txt
  perf script -i $TMPD/sched.data --pid $p -F comm,tid,cpu,time,event,trace,ip,sym 2>/dev/null \
    > $TMPD/sched_script.txt
  perf sched timehist -i $TMPD/sched.data -w -g --state -p $p 2>/dev/null > $TMPD/timehist.txt
  for f in offcpu_dwarf sched_script timehist; do head -n $EXCERPT $TMPD/$f.txt > $d/${f}_excerpt.txt; done
  $R7 roles $TMPD/offcpu_dwarf.txt > $d/tid_roles.txt
  $R7 stacks $d/tid_roles.txt < $TMPD/offcpu_dwarf.txt > $d/dwarf_stacks.md
  $R7 sched $d $TMPD/timehist.txt $TMPD/sched_script.txt > $d/tables.md
  rm -f $TMPD/*.data $TMPD/*.txt $TMPD/*.data.old
}

# run <dir> <clean|bt|offcpu> <asd pid>
run() {
  local d=$1 kind=$2 p=$3 bg
  mkdir -p $d
  kvl --qps 16 --duration $RUN_S > $d/kvl.txt 2>&1 & bg=$!
  sleep 3
  case $kind in
    clean)
      echo "start $(now)" > $d/times.txt
      mpstat -P ALL 1 $WIN_S > $d/mpstat.txt 2>&1
      echo "end $(now)" >> $d/times.txt ;;
    bt)
      echo "start $(now)" > $d/times.txt
      bpftrace -f json $BT > $d/bpftrace.json 2> $d/bpftrace.err
      echo "end $(now)" >> $d/times.txt ;;
    offcpu) capture $d $p ;;
  esac
  wait $bg
  say "$(basename $(dirname $d))/$kind: $(grep -E '^stream:' $d/kvl.txt)"
  grep -qE 'failed rows 0$' $d/kvl.txt || say "$(basename $(dirname $d))/$kind: WARNING client rows failed"
}

# config <name> <cap> <budget MiB>; returns 1 if the server or the fill fails.
config() {
  local c=$1 cap=$2 mb=$3 d=$O/$1 p
  mkdir -p $d $TMPD
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF
  export PERF_ASD_ENV="KV_SINK_STATS=1 KV_SINK_MAX_IN_FLIGHT=$cap KV_SINK_PATH_BUDGET_MB=$mb"
  $PERF exp_start > $d/perf_start.txt 2>&1
  if ! grep -q 'started on' $d/progress.log; then
    say "$c: server did not start"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1
  fi
  say "$c: $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  p=$(pgrep -x asd | head -n 1)
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  if ! grep -q 'failed rows 0' $d/kvl_fill.txt; then
    say "$c: fill failed rows"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1
  fi
  run $d/clean clean $p
  run $d/bt bt $p
  run $d/offcpu offcpu $p
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  grep "stats:" $d/asd-kvsink-bp-perf.log > $d/stats_lines.txt
  cp $d/stats_lines.txt $d/offcpu/
  post $d/offcpu $p
  say "$c: off-CPU parsed"
}

step_runs() {
  [ -x $NEW/asd ] || { say "no 55d6ae8d8 binary"; return 1; }
  [ "$(cat /sys/module/rdma_rxe/srcversion)" = "$(modinfo -F srcversion $KO)" ] || {
    say "loaded rdma_rxe is not $KO; bpftrace offsets would be wrong"; return 1; }
  [ -f $BT ] || { say "no $BT"; return 1; }
  local spec
  for spec in $CONFIGS; do
    IFS=: read -r name cap mb <<< "$spec"
    config $name $cap $mb || say "$name failed"
  done
  rm -rf $TMPD
  say "server stopped, data deleted"
}

step_lw() {
  local d=$O/lw best spec name cap mb
  best=$($R faster $O)
  [ -n "$best" ] || { say "lw: no clean runs to pick from"; return 1; }
  for spec in $CONFIGS; do
    IFS=: read -r name cap mb <<< "$spec"
    [ "$name" = "$best" ] && break
  done
  mkdir -p $d
  echo "$best" > $d/config.txt
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF CONCS=1 STORE_IDS=0-3 FS_PCT=50
  export PERF_ASD_ENV="KV_SINK_STATS=1 KV_SINK_MAX_IN_FLIGHT=$cap KV_SINK_PATH_BUDGET_MB=$mb"
  say "lw: 8k c=1 at 16 QPs with $best"
  $PERF precheck qpstore:8192 > $d/perf_store.txt 2>&1
  grep -q 'started on' $d/progress.log || { say "lw: server did not start"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1; }
  clean_tree || { say "lw: lmcache/ or csrc/ not clean before the timeline; stopping"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1; }
  python3 "$E3_PATCH" "$T/$PUMP" || { $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1; }
  $PERF timeline:16 > $d/perf_timeline_qp16.txt 2>&1
  git -C $T checkout -- $PUMP
  clean_tree || { say "!! lw: tree not clean after the E3 revert"; }
  say "lw: $(grep -h '^point' $d/E_timeline_qp16/session_*.txt | cut -c1-120)"
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  grep "stats:" $d/asd-kvsink-bp-perf.log > $d/stats_lines.txt
  say "lw: server stopped, data deleted"
}

# step_ab: same-session A/B, clean runs only, alternating 3b1d52fb3 (old) and
# 55d6ae8d8 (new) per config in AB ("bin:config" pairs, config from CONFIGS).
step_ab() {
  local pair bin cfg spec name cap mb d
  for pair in ${AB:-old:N128 new:N128 old:N512 new:N512 old:N128 new:N128}; do
    IFS=: read -r bin cfg <<< "$pair"
    for spec in $CONFIGS; do IFS=: read -r name cap mb <<< "$spec"; [ "$name" = "$cfg" ] && break; done
    d=$O/ab/$(date -u +%H%M%S)-$bin-$cfg
    mkdir -p $d
    if [ $bin = old ]; then export PERF_ASD=/root/lmc-work/asd-3b1d52fb3/asd; else export PERF_ASD=$NEW/asd; fi
    export PERF_OUT=$d CONF_TMPL=$MEM_CONF
    export PERF_ASD_ENV="KV_SINK_STATS=1 KV_SINK_MAX_IN_FLIGHT=$cap KV_SINK_PATH_BUDGET_MB=$mb"
    $PERF exp_start > $d/perf_start.txt 2>&1
    grep -q 'started on' $d/progress.log || { say "ab $bin $cfg: server did not start"; $PERF exp_stop > $d/perf_stop.txt 2>&1; continue; }
    kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
    run $d/clean clean 0
    $PERF exp_stop > $d/perf_stop.txt 2>&1
    grep "stats:" $d/asd-kvsink-bp-perf.log > $d/stats_lines.txt
  done
  export PERF_ASD=$NEW/asd
  say "ab: server stopped, data deleted"
}

docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "asd is running; stopping"; exit 1; }
while pgrep -f 'perf[.]sh' >/dev/null; do sleep 10; done
for s in $STEPS; do
  case $s in
    build) step_build || { say "build failed; stopping"; exit 1; } ;;
    runs) step_runs || { say "runs failed; stopping"; exit 1; } ;;
    lw) step_lw ;;
    ab) step_ab ;;
    *) say "unknown step $s" ;;
  esac
done
$R report $O > $O/report.md
say "BREAKDOWN8 DONE"
