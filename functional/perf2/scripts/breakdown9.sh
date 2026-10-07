#!/bin/bash
# perf2 breakdown, day 2 round 8 (Sriram, 2026-10-06 16:39 PT): per-QP depth
# sweep for the no-lock server. new = asd 55d6ae8d8, old = asd 3b1d52fb3 (both
# built by breakdown6.sh / breakdown8.sh). kvlayers on client e8158149, memory
# namespace, 16 QPs, 512 KiB, 32 placers (default), KV_SINK_STATS=1, LMCache
# unchanged. At 16 QPs writes per QP = KV_SINK_PATH_BUDGET_MB / 512 KiB.
#
# Points (POINTS, "name:old|new:env", env with commas for spaces), in session
# order. Each is one server cycle, in breakdown3.sh's order:
#   1. LMCache stores prompts 0-3 (perf.sh precheck qpstore:8192)
#   2. kvlayers --fill --reps 1, then --qps 16 --duration 25 with mpstat 10 s
#      3 s in
#   3. lw 8k c=1 timeline at 16 QPs (E3 print patch applied for the session
#      and reverted, tree checked clean before and after)
# Results: /root/lmc-work/functional/perf2/breakdown9/<nn>-<name>/.
set -u
T=/root/lmc-work/LMCache
O=/root/lmc-work/functional/perf2/breakdown9
R="python3 $O/../breakdown9_report.py"
PERF="bash $T/functional/perf/perf.sh"
E3_PATCH=/root/lmc-work/functional/perf2/e3_pump_patch.py
PUMP=lmcache/v1/layerwise/pump.py
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
OLD=/root/lmc-work/asd-3b1d52fb3/asd
NEW=/root/lmc-work/asd-55d6ae8d8/asd
POINTS=${POINTS:-old-def:old: new-B1:new:KV_SINK_PATH_BUDGET_MB=1 new-B2:new:KV_SINK_PATH_BUDGET_MB=2 \
new-def:new: new-C32:new:KV_SINK_MAX_IN_FLIGHT=32 old-def2:old:}
RUN_S=25
WIN_S=10
export DATA_DIR=/mnt/scratch/perf-aero CONCS=1 STORE_IDS=0-3 FS_PCT=50
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown9: $*" | tee -a $O/progress.log; }
clean_tree() { [ -z "$(git -C $T status --porcelain -- lmcache csrc)" ]; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
now() { date +%s.%N; }

# point <dir> <old|new> <env>; returns 1 if the tree is left dirty.
point() {
  local d=$1 bin=$2 env=$3 bg
  mkdir -p $d/clean
  if [ $bin = old ]; then export PERF_ASD=$OLD; else export PERF_ASD=$NEW; fi
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF PERF_ASD_ENV="KV_SINK_STATS=1 ${env//,/ }"
  echo "$bin $PERF_ASD_ENV" > $d/point.txt
  $PERF precheck qpstore:8192 > $d/perf_store.txt 2>&1
  if ! grep -q 'started on' $d/progress.log; then
    say "$(basename $d): server did not start"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 0
  fi
  say "$(basename $d): $bin; $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  grep -q 'failed rows 0' $d/kvl_fill.txt || say "$(basename $d): WARNING fill failed rows"
  kvl --qps 16 --duration $RUN_S > $d/clean/kvl.txt 2>&1 & bg=$!
  sleep 3
  echo "start $(now)" > $d/clean/times.txt
  mpstat -P ALL 1 $WIN_S > $d/clean/mpstat.txt 2>&1
  echo "end $(now)" >> $d/clean/times.txt
  wait $bg
  say "$(basename $d): $(grep -E '^stream:' $d/clean/kvl.txt)"
  if ! clean_tree; then
    say "$(basename $d): lmcache/ or csrc/ not clean before the timeline; stopping"
    $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1
  fi
  python3 "$E3_PATCH" "$T/$PUMP" || { $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1; }
  $PERF timeline:16 > $d/perf_timeline_qp16.txt 2>&1
  git -C $T checkout -- $PUMP
  clean_tree || { say "!! $(basename $d): tree not clean after the E3 revert"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1; }
  say "$(basename $d): $(grep -h '^point' $d/E_timeline_qp16/session_*.txt | cut -c1-110)"
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  grep "stats:" $d/asd-kvsink-bp-perf.log > $d/stats_lines.txt
}

docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "asd is running; stopping"; exit 1; }
[ -x $OLD ] && [ -x $NEW ] || { say "missing a binary"; exit 1; }
[ -f $E3_PATCH ] || { say "no $E3_PATCH"; exit 1; }
while pgrep -f 'perf[.]sh' >/dev/null; do sleep 10; done
i=0
for spec in $POINTS; do
  IFS=: read -r name bin env <<< "$spec"
  i=$((i + 1))
  point $O/$(printf %02d $i)-$name $bin "$env" || { say "stopping after $name"; break; }
done
say "server stopped, data deleted"
$R $O > $O/report.md
say "BREAKDOWN9 DONE"
