#!/bin/bash
# perf2 breakdown, day 2 round 5 (Sriram, 2026-10-06 14:00 PT): is the
# in-flight cap what starves the QPs? Server 3b1d52fb3 (KV_SINK_MAX_IN_FLIGHT
# up to 512, KV_SINK_PATH_BUDGET_MB = byte budget per QP, default 4), kvlayers
# on client e8158149, memory namespace, 16 QPs, 512 KiB, 32 placers (default),
# KV_SINK_STATS=1, LMCache unchanged.
#
# Steps (STEPS):
#   build   asd 3b1d52fb3 in place over 9c16972132 (as breakdown3.sh), the
#           binary kept in $NEW, tree and binary restored
#   runs    per config in CONFIGS ("name:cap:budget_mb"): a server cycle with
#           KV_SINK_MAX_IN_FLIGHT=cap KV_SINK_PATH_BUDGET_MB=budget, one fill,
#           then two kvlayers --qps 16 --duration 25 runs with a 10 s window
#           3 s in: <name>/clean (mpstat) and <name>/bt (breakdown5's bpftrace
#           exits). A config whose server doesn't start, or whose fill or run
#           fails rows, stops the larger configs after it.
# Results: /root/lmc-work/functional/perf2/breakdown6/<config>/.
set -u
T=/root/lmc-work/LMCache
SRC=/root/lmc-work/aerospike-server-kvsink-bp
ASD=$SRC/target/Linux-x86_64/bin/asd
ASD_ORIG_MD5=0b953fb421486d003e28ccc90dde2d7b
NEW=/root/lmc-work/asd-3b1d52fb3
SRC_TAR=$NEW/src-3b1d52fb3.tar
O=/root/lmc-work/functional/perf2/breakdown6
R="python3 $O/../breakdown6_report.py"
BT=$O/../breakdown5/exits.bt
PERF="bash $T/functional/perf/perf.sh"
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
KO=/root/rxe-build/v6.11/rdma_rxe.ko
STEPS=${STEPS:-build runs}
CONFIGS=${CONFIGS:-C128-B4:128:4 C256-B4:256:4 C256-B8:256:8 C512-B16:512:16}
RUN_S=25
WIN_S=10
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown6: $*" | tee -a $O/progress.log; }
touch_dependents() { grep -rl --include='*.c' 'kv_sink.h' $SRC/as/src | xargs touch; }
make_asd() { docker exec aero-kvsink-bp bash -c "cd $SRC && nice -n 19 make -j16" >> $O/build.txt 2>&1; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
now() { date +%s.%N; }

step_build() {
  local files rc
  if [ -x $NEW/asd ]; then say "3b1d52fb3 binary already built"; return 0; fi
  files=$(tar tf $SRC_TAR | grep -v '/$')
  mkdir -p $NEW/orig
  [ "$(md5sum < $ASD | cut -c1-32)" = $ASD_ORIG_MD5 ] || { say "!! tree binary is not 9c16972132"; return 1; }
  for f in $files; do mkdir -p $NEW/orig/$(dirname $f); cp -a $SRC/$f $NEW/orig/$f || return 1; done
  cp -a $ASD $NEW/asd.9c16972132 || return 1
  tar xf $SRC_TAR -C $SRC && (cd $SRC && touch $files) && touch_dependents
  say "building 3b1d52fb3 in place"
  make_asd && cp -a $ASD $NEW/asd.new && mv $NEW/asd.new $NEW/asd
  rc=$?
  for f in $files; do cp -a $NEW/orig/$f $SRC/$f; done
  touch_dependents
  make_asd
  cp -a $NEW/asd.9c16972132 $ASD
  if [ "$(md5sum < $ASD | cut -c1-32)" != $ASD_ORIG_MD5 ]; then say "!! 9c16972132 binary not restored"; return 1; fi
  for f in $files; do cmp -s $SRC/$f $NEW/orig/$f || { say "!! $f not restored"; return 1; }; done
  say "tree and 9c16972132 binary restored ($ASD_ORIG_MD5)"
  [ $rc -eq 0 ] || { say "3b1d52fb3 build failed; see build.txt"; rm -f $NEW/asd; return 1; }
  strings $NEW/asd | grep -q 'MiB per path' || { say "!! new binary has no path budget"; rm -f $NEW/asd; return 1; }
  say "3b1d52fb3 binary $(md5sum < $NEW/asd | cut -c1-32)"
}

# run <dir> <with bpftrace: 1 or 0>
run() {
  local d=$1 bt=$2 bg m
  mkdir -p $d
  kvl --qps 16 --duration $RUN_S > $d/kvl.txt 2>&1 & bg=$!
  sleep 3
  echo "start $(now)" > $d/times.txt
  if [ "$bt" = 1 ]; then
    bpftrace -f json $BT > $d/bpftrace.json 2> $d/bpftrace.err
  else
    mpstat -P ALL 1 $WIN_S > $d/mpstat.txt 2>&1
  fi
  echo "end $(now)" >> $d/times.txt
  wait $bg
  say "$(basename $(dirname $d))/$(basename $d): $(grep -E '^stream:' $d/kvl.txt)"
  grep -qE 'failed rows 0$' $d/kvl.txt
}

# config <name> <cap> <budget MiB>; returns 1 if the server or rows fail.
config() {
  local c=$1 cap=$2 mb=$3 d=$O/$1
  mkdir -p $d
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF
  export PERF_ASD_ENV="KV_SINK_STATS=1 KV_SINK_MAX_IN_FLIGHT=$cap KV_SINK_PATH_BUDGET_MB=$mb"
  $PERF exp_start > $d/perf_start.txt 2>&1
  if ! grep -q 'started on' $d/progress.log; then
    say "$c: server did not start"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1
  fi
  say "$c: $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  local ok=0
  grep -q 'failed rows 0' $d/kvl_fill.txt || { say "$c: fill failed rows"; ok=1; }
  if [ $ok = 0 ]; then
    run $d/clean 0 || ok=1
    run $d/bt 1 || ok=1
  fi
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  grep "stats:" $d/asd-kvsink-bp-perf.log > $d/stats_lines.txt
  return $ok
}

step_runs() {
  [ "$(cat /sys/module/rdma_rxe/srcversion)" = "$(modinfo -F srcversion $KO)" ] || {
    say "loaded rdma_rxe is not $KO; bpftrace offsets would be wrong"; return 1; }
  [ -f $BT ] || { say "no $BT"; return 1; }
  cp $BT $O/exits.bt
  local spec
  for spec in $CONFIGS; do
    IFS=: read -r name cap mb <<< "$spec"
    config $name $cap $mb || { say "$name failed; skipping larger configs"; break; }
  done
  say "server stopped, data deleted"
  $R $O > $O/report.md
}

docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "asd is running; stopping"; exit 1; }
for s in $STEPS; do
  case $s in
    build) step_build || { say "build failed; stopping"; exit 1; } ;;
    runs) step_runs || { say "runs failed"; exit 1; } ;;
  esac
done
say "BREAKDOWN6 DONE"
