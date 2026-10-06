#!/bin/bash
# perf2 breakdown (Sriram, 2026-10-05): why lw reaches only 54-64% of raw
# ib_write_bw. Server sriram/kv-sink-batch-prio 314564cfb (KV_SINK_STATS,
# KV_SINK_MAX_IN_FLIGHT), client 5a24afdb, LMCache unchanged.
#
# The server tree's module builds hold absolute paths, so 314564cfb's four
# kv-sink files (SRC_TAR, a git archive) are built in place over 9c16972132's,
# the binary kept in $NEW, and the tree and the 9c16972132 binary restored.
#
# Steps (STEPS), each server cycle on its own namespace (memory unless dev*),
# KV_SINK_STATS=1, prompts 0-3 stored by LMCache, kvlayers rows filled once:
#   build      the 314564cfb binary
#   A          ib_write_bw at the server's depths (perf/ibbw.sh sink_depths)
#   mem32      B (kvlayers, --qps 1 8 16), C (lw 8k c=1 timeline at 1 8 16),
#              E at 8 QPs during B and C (mpstat, top -H, perf)
#   mem128     B and C at 16 QPs, KV_SINK_MAX_IN_FLIGHT=128
#   mem128p16  the same plus KV_SINK_PLACE_THREADS=16
#   dev32, dev128  device namespace, B and C at 16 QPs, cap 32 and 128
#   mem32e     E again at 8 QPs, keeping perf's full report (perf_full.txt)
#   devp16, devp32, devp64  device namespace, cap 128, 16/32/64 placement
#              threads, B and C at 16 QPs (day 2, Q3 G)
# BD_OUT: the results directory (default .../perf2/breakdown).
# C's per-layer timeline uses the box-only E3 print patch in pump.py, applied
# for the C sessions and reverted (never committed); the script stops if
# lmcache/ or csrc/ are not clean afterwards.
# Results: /root/lmc-work/functional/perf2/breakdown/<step>/.
set -u
T=/root/lmc-work/LMCache
SRC=/root/lmc-work/aerospike-server-kvsink-bp
ASD=$SRC/target/Linux-x86_64/bin/asd
ASD_ORIG_MD5=0b953fb421486d003e28ccc90dde2d7b
NEW=/root/lmc-work/asd-314564cfb
SRC_TAR=${SRC_TAR:-$NEW/src-314564cfb.tar}
O=${BD_OUT:-/root/lmc-work/functional/perf2/breakdown}
PERF="bash $T/functional/perf/perf.sh"
E3_PATCH=${E3_PATCH:-/root/lmc-work/functional/perf2/e3_pump_patch.py}
PUMP=lmcache/v1/layerwise/pump.py
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
DEV_CONF=$T/functional/configs/aerospike-kvsink-bp-perf.conf.in
KVL_DIR=/work/kvlayers-build/client/examples/kv_sink
STEPS=${STEPS:-build A mem32 mem128 mem128p16 dev32 dev128}
# 16G namespace or data file: 4 prompts (4 GiB of KV) plus kvlayers' 1 GiB.
export DATA_DIR=/mnt/scratch/perf-aero CONCS=1 STORE_IDS=0-3 FS_PCT=50 PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown: $*" | tee -a $O/progress.log; }
clean_tree() { [ -z "$(git -C $T status --porcelain -- lmcache csrc)" ]; }
touch_dependents() { grep -rl --include='*.c' 'kv_sink.h' $SRC/as/src | xargs touch; }
make_asd() { docker exec aero-kvsink-bp bash -c "cd $SRC && nice -n 19 make -j16" >> $O/build.txt 2>&1; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
# mark <dir> <name>: the server log's line count, to slice its stats lines per run.
mark() { echo "$2 $(date -u +%FT%TZ) $(wc -l < $1/asd-kvsink-bp-perf.log)" >> $1/marks.txt; }

step_build() {
  if [ -x $NEW/asd ]; then say "314564cfb binary already built"; return 0; fi
  local files rc
  files=$(tar tf $SRC_TAR | grep -v '/$')
  mkdir -p $NEW/orig
  [ -f $NEW/asd.9c16972132 ] || cp -a $ASD $NEW/asd.9c16972132 || return 1
  for f in $files; do mkdir -p $NEW/orig/$(dirname $f); cp -a $SRC/$f $NEW/orig/$f || return 1; done
  tar xf $SRC_TAR -C $SRC && (cd $SRC && touch $files) && touch_dependents
  say "building 314564cfb in place"
  make_asd && cp -a $ASD $NEW/asd.new && mv $NEW/asd.new $NEW/asd
  rc=$?
  for f in $files; do cp -a $NEW/orig/$f $SRC/$f; done
  touch_dependents
  make_asd
  cp -a $NEW/asd.9c16972132 $ASD
  if [ "$(md5sum < $ASD | cut -c1-32)" != $ASD_ORIG_MD5 ]; then say "!! 9c16972132 binary not restored"; return 1; fi
  for f in $files; do cmp -s $SRC/$f $NEW/orig/$f || { say "!! $f not restored"; return 1; }; done
  say "tree and 9c16972132 binary restored ($ASD_ORIG_MD5)"
  [ $rc -eq 0 ] || { say "314564cfb build failed; see build.txt"; rm -f $NEW/asd; return 1; }
  strings $NEW/asd | grep -q 'kv-sink: stats' || { say "!! new binary has no stats line"; rm -f $NEW/asd; return 1; }
  say "314564cfb binary $(md5sum < $NEW/asd | cut -c1-32)"
}

step_A() {
  docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "A: asd is running; skipped"; return 1; }
  IBBW_SET=sink_depths IBBW_OUT=$O/A bash $T/functional/perf/ibbw.sh > $O/A_run.txt 2>&1
  say "A: $(grep -E '^write_' $O/A_run.txt | paste -sd' ')"
}

# sample <dir>: E's samples, about 5 s.
sample() {
  mkdir -p $1
  mpstat -P ALL 1 7 > $1/mpstat.txt 2>&1 &
  local m=$!
  top -H -b -n 3 -d 1 -w 250 2>&1 | awk '/^top -/{n=0} {n++} n<=40' > $1/top_H.txt &
  local t=$!
  perf record -a -g -o $1/perf.data -- sleep 5 > $1/perf_record.txt 2>&1
  wait $m $t
  perf report -i $1/perf.data --no-children --sort comm,dso,symbol -g none --stdio 2>/dev/null |
    grep -E '^ +[0-9]' > $1/perf_full.txt
  head -n 30 $1/perf_full.txt > $1/perf_top30.txt
  rm -f $1/perf.data
}
# sample_on_traffic <dir> <step-dir>: wait for a new stats line, then sample.
sample_on_traffic() {
  local from; from=$(wc -l < $2/asd-kvsink-bp-perf.log)
  for _ in $(seq 900); do
    tail -n +$((from + 1)) $2/asd-kvsink-bp-perf.log | grep -q 'kv-sink: stats:' && { sample $1; return; }
    sleep 1
  done
  echo "no stats line in 900 s" > $1.missing
}

# cycle <step> <conf> <env> <B qps> <C qps> <E: 1 or 0>
cycle() {
  local step=$1 conf=$2 env=$3 bq=$4 cq=$5 e=$6 d=$O/$1 q bg
  [ -x $NEW/asd ] || { say "$step: no 314564cfb binary"; return 1; }
  export PERF_OUT=$d CONF_TMPL=$conf PERF_ASD_ENV="KV_SINK_STATS=1 $env"
  mkdir -p $d; rm -f $d/marks.txt
  say "$step: $(basename $conf), ${env:-defaults}; B at $bq QPs, C at $cq QPs"
  $PERF precheck qpstore:8192 > $d/perf_store.txt 2>&1
  grep -q 'started on' $d/progress.log || { say "$step: server did not start"; return 1; }
  say "$step: $(grep -o 'storage-engine=[a-z]*' $d/progress.log | tail -n 1); $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  say "$step: kvlayers fill exit=$?: $(grep -E '^row results|failed' $d/kvl_fill.txt | head -n 2 | paste -sd' ')"
  for q in $bq; do
    mark $d B_qp${q}_start
    kvl --qps $q --duration 6 > $d/kvl_qp$q.txt 2>&1
    mark $d B_qp${q}_end
    say "$step: B qp=$q $(grep -E '^stream:' $d/kvl_qp$q.txt)"
  done
  if [ "$e" = 1 ]; then
    mark $d E_B_qp8_start
    kvl --qps 8 --duration 15 > $d/kvl_qp8_E.txt 2>&1 & bg=$!
    sleep 4; sample $d/E_B_qp8; wait $bg
    mark $d E_B_qp8_end
    say "$step: E during B qp=8 ($(grep -E '^stream:' $d/kvl_qp8_E.txt))"
  fi
  clean_tree || { say "$step: lmcache/ or csrc/ not clean before C; stopping"; return 1; }
  python3 "$E3_PATCH" "$T/$PUMP" || return 1
  for q in $cq; do
    [ "$e" = 1 ] && [ "$q" = 8 ] && { sample_on_traffic $d/E_C_qp8 $d & bg=$!; }
    mark $d C_qp${q}_start
    $PERF timeline:$q > $d/perf_C_qp$q.txt 2>&1
    mark $d C_qp${q}_end
    [ "$e" = 1 ] && [ "$q" = 8 ] && wait $bg
    say "$step: C qp=$q $(grep -h '^point' $d/E_timeline_qp$q/session_*.txt | cut -c1-120)"
  done
  git -C $T checkout -- $PUMP
  clean_tree || { say "!! $step: tree not clean after the E3 revert"; return 1; }
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  say "$step: server stopped, data deleted"
}

for s in $STEPS; do
  case $s in
    build) step_build || { say "build failed; stopping"; exit 1; } ;;
    A) step_A ;;
    mem32) cycle mem32 $MEM_CONF "" "1 8 16" "1 8 16" 1 ;;
    mem128) cycle mem128 $MEM_CONF "KV_SINK_MAX_IN_FLIGHT=128" "16" "16" 0 ;;
    mem128p16) cycle mem128p16 $MEM_CONF "KV_SINK_MAX_IN_FLIGHT=128 KV_SINK_PLACE_THREADS=16" "16" "16" 0 ;;
    dev32) cycle dev32 $DEV_CONF "" "16" "16" 0 ;;
    dev128) cycle dev128 $DEV_CONF "KV_SINK_MAX_IN_FLIGHT=128" "16" "16" 0 ;;
    mem32e) cycle mem32e $MEM_CONF "" "8" "8" 1 ;;
    devp16|devp32|devp64) cycle $s $DEV_CONF "KV_SINK_MAX_IN_FLIGHT=128 KV_SINK_PLACE_THREADS=${s#devp}" "16" "16" 0 ;;
    *) say "unknown step $s" ;;
  esac
done
say "BREAKDOWN DONE"
