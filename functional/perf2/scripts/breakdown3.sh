#!/bin/bash
# perf2 breakdown, day 2 round 2 (Sriram, 2026-10-06 10:16 PT). Server
# sriram/kv-sink-batch-prio 0c9703931 (least-loaded queue pair, "busy qps X of
# Y" in the stats line, up to 32 QPs, defaults 32 placers and cap 128), client
# e8158149 (up to 32 RC QPs). LMCache unchanged; it allows at most 16 queue
# pairs (rdma.queue_pairs), so LMCache keeps its 5a24afdb client and run 4 is
# at 16 QPs only. KV_SINK_STATS=1, server defaults otherwise, memory namespace.
#
# Steps (STEPS):
#   build   asd 0c9703931 in place over 9c16972132 (as breakdown.sh's
#           step_build), the binary kept in $NEW, tree and binary restored;
#           kvlayers relinked against e8158149 in its own client copy ($KVN)
#   raw     run 3: ib_write_bw -s 524288 -t 2 at -q 16 (reference), 24, 32, 64
#   mem     one server cycle: prompts 0-3 stored by LMCache, kvlayers fill,
#           run 1 (kvlayers --qps 16 --duration 8, twice), run 2 (--qps 24,
#           --qps 32), run 5 (kvlayers --qps 16 again with mpstat and perf
#           record -a -g -F 999 for 5 s), run 4 (lw 8k c=1 timeline at 16 QPs,
#           E3 print patch applied and reverted as in breakdown.sh)
# Results: /root/lmc-work/functional/perf2/breakdown3/<step>/.
set -u
T=/root/lmc-work/LMCache
SRC=/root/lmc-work/aerospike-server-kvsink-bp
ASD=$SRC/target/Linux-x86_64/bin/asd
ASD_ORIG_MD5=0b953fb421486d003e28ccc90dde2d7b
NEW=/root/lmc-work/asd-0c9703931
SRC_TAR=$NEW/src-0c9703931.tar
KVO=/root/lmc-work/kvlayers-build/client
KVN=/root/lmc-work/kvlayers-e8158149/client
CLI_TAR=/root/lmc-work/kvlayers-e8158149/src-e8158149.tar
O=/root/lmc-work/functional/perf2/breakdown3
PERF="bash $T/functional/perf/perf.sh"
E3_PATCH=${E3_PATCH:-/root/lmc-work/functional/perf2/e3_pump_patch.py}
PUMP=lmcache/v1/layerwise/pump.py
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
STEPS=${STEPS:-build raw mem}
export DATA_DIR=/mnt/scratch/perf-aero CONCS=1 STORE_IDS=0-3 FS_PCT=50 PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown3: $*" | tee -a $O/progress.log; }
clean_tree() { [ -z "$(git -C $T status --porcelain -- lmcache csrc)" ]; }
touch_dependents() { grep -rl --include='*.c' 'kv_sink.h' $SRC/as/src | xargs touch; }
make_asd() { docker exec aero-kvsink-bp bash -c "cd $SRC && nice -n 19 make -j16" >> $O/build.txt 2>&1; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
mark() { echo "$2 $(date -u +%FT%TZ) $(wc -l < $1/asd-kvsink-bp-perf.log)" >> $1/marks.txt; }

# prof <dir>: mpstat and perf record -a -g -F 999 for 5 s, sample-count reports.
prof() {
  mkdir -p $1
  mpstat -P ALL 1 5 > $1/mpstat.txt 2>&1 &
  local m=$!
  perf record -a -g -F 999 -o $1/perf.data -- sleep 5 > $1/perf_record.txt 2>&1
  wait $m
  perf report -i $1/perf.data -n --no-children --sort comm,dso,symbol -g none --stdio 2>/dev/null \
    > $1/perf_full.txt
  perf report -i $1/perf.data -n --no-children --sort comm,dso -g none --stdio 2>/dev/null \
    > $1/perf_comm_dso.txt
  rm -f $1/perf.data
}

# pt <dir> <name> <secs> <ib_write_bw args>: perftest pair in lmc-c, the
# server's socket on 127.0.0.1 (checked; anything else is killed).
PT_PORT=18800
pt() {
  local d=$1 name=$2 secs=$3 l; shift 3
  PT_PORT=$((PT_PORT + 1))
  local common="-d rxe0 -x 1 -m 4096 -s 524288 -D $secs -F --report_gbits -p $PT_PORT"
  docker exec -d lmc-c bash -c "ib_write_bw $common --bind_source_ip 127.0.0.1 $* > /work${d#/root/lmc-work}/${name}_server.txt 2>&1"
  sleep 1
  l=$(ss -ltnp | grep ":$PT_PORT ")
  echo "$name listener: $l" >> $d/listeners.txt
  if ! echo "$l" | grep -q "127.0.0.1:$PT_PORT "; then
    say "!! $name: perftest not on loopback ($l); killing it"
    for p in $(docker exec lmc-c pgrep -x ib_write_bw); do docker exec lmc-c kill -9 "$p"; done
    exit 1
  fi
  docker exec lmc-c bash -c "ib_write_bw $common $* 127.0.0.1 > /work${d#/root/lmc-work}/${name}_client.txt 2>&1"
  say "$name: $(grep -E '^ *524288' $d/${name}_client.txt | tr -s ' ')"
}

step_build() {
  local files rc
  if [ -x $NEW/asd ]; then
    say "0c9703931 binary already built"
  else
    files=$(tar tf $SRC_TAR | grep -v '/$')
    mkdir -p $NEW/orig
    [ "$(md5sum < $ASD | cut -c1-32)" = $ASD_ORIG_MD5 ] || { say "!! tree binary is not 9c16972132"; return 1; }
    for f in $files; do mkdir -p $NEW/orig/$(dirname $f); cp -a $SRC/$f $NEW/orig/$f || return 1; done
    cp -a $ASD $NEW/asd.9c16972132 || return 1
    tar xf $SRC_TAR -C $SRC && (cd $SRC && touch $files) && touch_dependents
    say "building 0c9703931 in place"
    make_asd && cp -a $ASD $NEW/asd.new && mv $NEW/asd.new $NEW/asd
    rc=$?
    for f in $files; do cp -a $NEW/orig/$f $SRC/$f; done
    touch_dependents
    make_asd
    cp -a $NEW/asd.9c16972132 $ASD
    if [ "$(md5sum < $ASD | cut -c1-32)" != $ASD_ORIG_MD5 ]; then say "!! 9c16972132 binary not restored"; return 1; fi
    for f in $files; do cmp -s $SRC/$f $NEW/orig/$f || { say "!! $f not restored"; return 1; }; done
    say "tree and 9c16972132 binary restored ($ASD_ORIG_MD5)"
    [ $rc -eq 0 ] || { say "0c9703931 build failed; see build.txt"; rm -f $NEW/asd; return 1; }
    strings $NEW/asd | grep -q 'busy qps' || { say "!! new binary has no busy-qps stat"; rm -f $NEW/asd; return 1; }
    say "0c9703931 binary $(md5sum < $NEW/asd | cut -c1-32)"
  fi
  if [ -x $KVN/examples/kv_sink/kvlayers ]; then say "e8158149 kvlayers already built"; return 0; fi
  rm -rf $KVN && mkdir -p $(dirname $KVN) && cp -a $KVO $KVN || return 1
  tar xf $CLI_TAR -C $KVN && (cd $KVN && touch $(tar tf $CLI_TAR | grep -v '/$')) || return 1
  for f in $(tar tf $CLI_TAR | grep -v '/$'); do
    cmp -s $KVN/$f $KVO/$f && { say "!! e8158149 source not applied ($f)"; return 1; }
  done
  docker exec lmc-c bash -c "cd ${KVN/\/root\/lmc-work/\/work} && nice -n 19 make -j16 EVENT_LIB=libuv && cd examples/kv_sink && rm -f kvlayers && make kvlayers" \
    >> $O/build_client.txt 2>&1 || { say "e8158149 kvlayers build failed; see build_client.txt"; return 1; }
  say "e8158149 kvlayers $(md5sum < $KVN/examples/kv_sink/kvlayers | cut -c1-32); old kvlayers $(md5sum < $KVO/examples/kv_sink/kvlayers | cut -c1-32)"
}

step_raw() {
  local d=$O/raw q
  mkdir -p $d; rm -f $d/listeners.txt
  docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "raw: asd is running; skipped"; return 1; }
  for q in 16 24 32 64; do pt $d q${q}_t2 10 -q $q -t 2; done
}

step_mem() {
  local d=$O/mem q i bg
  [ -x $NEW/asd ] || { say "mem: no 0c9703931 binary"; return 1; }
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF PERF_ASD_ENV="KV_SINK_STATS=1"
  mkdir -p $d; rm -f $d/marks.txt
  $PERF precheck qpstore:8192 > $d/perf_store.txt 2>&1
  grep -q 'started on' $d/progress.log || { say "mem: server did not start"; return 1; }
  say "mem: $(grep -o 'storage-engine=[a-z]*' $d/progress.log | tail -n 1); $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  say "mem: kvlayers fill exit=$?: $(grep -E '^row results|failed' $d/kvl_fill.txt | head -n 2 | paste -sd' ')"
  for i in 1 2; do
    mark $d R1_qp16_${i}_start
    kvl --qps 16 --duration 8 > $d/kvl_qp16_$i.txt 2>&1
    mark $d R1_qp16_${i}_end
    say "mem: run 1 qp=16 ($i) $(grep -E '^stream:' $d/kvl_qp16_$i.txt)"
  done
  for q in 24 32; do
    mark $d R2_qp${q}_start
    kvl --qps $q --duration 8 > $d/kvl_qp$q.txt 2>&1
    mark $d R2_qp${q}_end
    say "mem: run 2 qp=$q $(grep -E '^stream:' $d/kvl_qp$q.txt)"
  done
  mark $d R5_start
  kvl --qps 16 --duration 8 > $d/kvl_qp16_R5.txt 2>&1 & bg=$!
  sleep 2; mark $d R5_prof_start; prof $d/R5; mark $d R5_prof_end; wait $bg
  mark $d R5_end
  say "mem: run 5 qp=16 $(grep -E '^stream:' $d/kvl_qp16_R5.txt)"
  clean_tree || { say "mem: lmcache/ or csrc/ not clean before run 4; stopping"; return 1; }
  python3 "$E3_PATCH" "$T/$PUMP" || return 1
  mark $d R4_qp16_start
  $PERF timeline:16 > $d/perf_R4_qp16.txt 2>&1
  mark $d R4_qp16_end
  git -C $T checkout -- $PUMP
  clean_tree || { say "!! mem: tree not clean after the E3 revert"; return 1; }
  say "mem: run 4 qp=16 $(grep -h '^point' $d/E_timeline_qp16/session_*.txt | cut -c1-120)"
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  say "mem: server stopped, data deleted"
}

while pgrep -f 'perf[.]sh' >/dev/null; do sleep 10; done
for s in $STEPS; do
  case $s in
    build) step_build || { say "build failed; stopping"; exit 1; } ;;
    raw) step_raw ;;
    mem) step_mem ;;
    *) say "unknown step $s" ;;
  esac
done
say "BREAKDOWN3 DONE"
