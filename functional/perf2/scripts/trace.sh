#!/bin/bash
# perf2 follow-up D (D-30): where the sink path loses time. Builds the server
# with a box-only trace patch (TRACE_PATCH, never committed: per-op
# submit / pick / place / post / complete timestamps, and env overrides of the
# in-flight budgets), then on 8k full hits at c=1, one data file each:
#   qp1          queue_pairs 1, default budgets
#   qp16         queue_pairs 16, default budgets
#   qp1_region16m  queue_pairs 1, region budget 16 MiB per queue pair (4x)
#   qp16_place16   queue_pairs 16, 16 placement threads (KV_SINK_PLACE_THREADS,
#                  in the unpatched server too; default 8)
#   qp16_place32   queue_pairs 16, 32 placement threads
# Results: /root/lmc-work/functional/perf2/trace/<run>/ (asd_trace.txt per run).
set -u
T=/root/lmc-work/LMCache
# The server tree's module builds hold absolute paths, so a copy of it does
# not build: the patch is applied in place, built, and reverted, and the
# original binary is put back byte for byte.
SRC=/root/lmc-work/aerospike-server-kvsink-bp
ASD=$SRC/target/Linux-x86_64/bin/asd
KEEP=/root/lmc-work/asd-trace
TRACE_PATCH=${TRACE_PATCH:-/root/lmc-work/functional/perf2/trace_patch.py}
O=/root/lmc-work/functional/perf2/trace
RUNS=${RUNS:-qp1 qp16 qp1_region16m}
export DATA_DIR=/mnt/scratch/perf-aero FS_PCT=200 CONCS=1 STORE_IDS=${STORE_IDS:-0-31}
export PERF_ASD=$KEEP/asd
mkdir -p $O $KEEP
say() { echo "$(date -u +%FT%TZ) trace: $*" | tee -a $O/progress.log; }
build() { docker exec aero-kvsink-bp bash -c "cd $SRC && nice -n 19 make -j16" >> $O/build.txt 2>&1; }
touch_dependents() { grep -rl --include='*.c' 'kv_sink.h' $SRC/as/src | xargs touch; }

if [ ! -x "$PERF_ASD" ]; then
  [ -f $KEEP/asd.orig ] || cp -a $ASD $KEEP/asd.orig || exit 1
  cp -a $SRC/as/src/base/kv_sink.c $KEEP/kv_sink.c.orig && cp -a $SRC/as/include/base/kv_sink.h $KEEP/kv_sink.h.orig || exit 1
  python3 "$TRACE_PATCH" $SRC || { say "patch failed"; exit 1; }
  # The op struct changes size: rebuild everything that includes kv_sink.h.
  touch_dependents
  say "building the trace server in place (original binary saved as $KEEP/asd.orig)"
  build && cp -a $ASD $KEEP/asd.new && mv $KEEP/asd.new $PERF_ASD
  rc=$?
  cp -a $KEEP/kv_sink.c.orig $SRC/as/src/base/kv_sink.c && cp -a $KEEP/kv_sink.h.orig $SRC/as/include/base/kv_sink.h
  touch_dependents
  build
  cp -a $KEEP/asd.orig $ASD
  if [ "$(md5sum < $ASD)" = "$(md5sum < $KEEP/asd.orig)" ]; then
    say "patch reverted and rebuilt; original binary restored ($(md5sum < $ASD | cut -c1-32))"
  else
    say "!! original binary differs from $KEEP/asd.orig"; exit 1
  fi
  [ $rc -eq 0 ] || { say "trace build failed; see build.txt"; rm -f $PERF_ASD; exit 1; }
  say "trace server $(md5sum < $PERF_ASD | cut -c1-32)"
fi

for run in $RUNS; do
  case $run in
    qp1) qp=1 extra="" ;;
    qp16) qp=16 extra="" ;;
    qp1_region16m) qp=1 extra="KV_SINK_REGION_INFLIGHT_KB=16384" ;;
    qp16_place16) qp=16 extra="KV_SINK_PLACE_THREADS=16" ;;
    qp16_place32) qp=16 extra="KV_SINK_PLACE_THREADS=32" ;;
    *) say "unknown run $run"; continue ;;
  esac
  export PERF_OUT=$O/$run
  mkdir -p $PERF_OUT
  rm -f $PERF_OUT/asd_trace.txt
  say "$run: queue_pairs $qp ${extra:-default budgets}"
  PERF_ASD_ENV="KV_SINK_TRACE=$PERF_OUT/asd_trace.txt $extra" \
    bash $T/functional/perf/perf.sh precheck qpstore:8192 qplw:8192:$qp exp_stop
  say "$run: $(($(wc -l < $PERF_OUT/asd_trace.txt 2>/dev/null || echo 1) - 1)) traced ops"
done
say "TRACE DONE"
