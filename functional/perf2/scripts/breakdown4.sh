#!/bin/bash
# perf2 breakdown, day 2 round 3 (Sriram, 2026-10-06 11:08 PT): why Soft-RoCE
# keeps ~16 cores busy for perftest but ~10 for the sink. asd 0c9703931 at
# defaults (32 placers, cap 128), kvlayers on client e8158149, memory
# namespace, LMCache unchanged, KV_SINK_STATS=1.
#
# Configs, 16 QPs, 512 KiB, each run 20 s; captures start 3 s in and run back
# to back (so they don't overlap):
#   P2     ib_write_bw -q 16 -t 2           (no server)
#   P8     ib_write_bw -q 16 -t 8           (no server)
#   S      kvlayers --qps 16 --duration 20
#   S-hot  S with --layers 16 --chunks 1 --prefix hot8 (8 MiB working set);
#          profile and mpstat only
# Captures: perf record -a -g -F 999 (5 s) with mpstat -P ALL 1 5; workqueue
# tracepoints with -g (2 s); perf sched record (2 s). Reports and perf script
# parsing (breakdown4_report.py) run after the traffic stops.
# Results: /root/lmc-work/functional/perf2/breakdown4/<config>/.
set -u
T=/root/lmc-work/LMCache
NEW=/root/lmc-work/asd-0c9703931
O=/root/lmc-work/functional/perf2/breakdown4
R="python3 $O/../breakdown4_report.py"
PERF="bash $T/functional/perf/perf.sh"
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
RUN_S=20
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown4: $*" | tee -a $O/progress.log; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
now() { date +%s.%N; }

# capture <dir> <full: 1 or 0>
capture() {
  local d=$1 full=$2 m
  mkdir -p $d; rm -f $d/times.txt
  mpstat -P ALL 1 5 > $d/mpstat.txt 2>&1 & m=$!
  echo "prof_start $(now)" >> $d/times.txt
  perf record -a -g -F 999 -o $d/prof.data -- sleep 5 > $d/prof_record.txt 2>&1
  echo "prof_end $(now)" >> $d/times.txt
  wait $m
  [ "$full" = 1 ] || return 0
  echo "wq_start $(now)" >> $d/times.txt
  perf record -a -g -e workqueue:workqueue_queue_work -e workqueue:workqueue_execute_start \
    -e workqueue:workqueue_execute_end -o $d/wq.data -- sleep 2 > $d/wq_record.txt 2>&1
  echo "wq_end $(now)" >> $d/times.txt
  perf sched record -a -o $d/sched.data -- sleep 2 > $d/sched_record.txt 2>&1
}

# post <dir> <full> <gib source>: parse, keep excerpts, drop the perf data.
post() {
  local d=$1 full=$2 src=$3
  perf script -i $d/prof.data -F comm,tid,cpu,ip,sym,dso 2>/dev/null | $R prof > $d/prof.json
  perf script -i $d/prof.data -F comm,tid,cpu,ip,sym,dso 2>/dev/null | head -n 3000 > $d/prof_script_excerpt.txt
  perf report -i $d/prof.data -n --no-children --sort comm,sym -g none --stdio 2>/dev/null | head -n 200 \
    > $d/prof_comm_sym.txt
  perf report -i $d/prof.data -n --no-children --sort cpu -g none --stdio 2>/dev/null > $d/prof_cpu.txt
  if [ "$full" = 1 ]; then
    perf script -i $d/wq.data 2>/dev/null | $R wq > $d/wq.json
    perf script -i $d/wq.data 2>/dev/null | head -n 3000 > $d/wq_script_excerpt.txt
    perf sched latency -i $d/sched.data --sort max 2>/dev/null > $d/sched_latency.txt
    $R sched < $d/sched_latency.txt > $d/sched.json
  fi
  $R gib $d $src > $d/gib.json
  rm -f $d/prof.data $d/wq.data $d/sched.data
  say "$(basename $d): parsed; gib $(cat $d/gib.json)"
}

PT_PORT=18900
# perftest <config> <threads per QP>
perftest() {
  local c=$1 t=$2 d=$O/$1 l
  mkdir -p $d; PT_PORT=$((PT_PORT + 1))
  local common="-d rxe0 -x 1 -m 4096 -s 524288 -D $RUN_S -F --report_gbits -p $PT_PORT -q 16 -t $t"
  docker exec -d lmc-c bash -c "ib_write_bw $common --bind_source_ip 127.0.0.1 > /work${d#/root/lmc-work}/server.txt 2>&1"
  sleep 1
  l=$(ss -ltnp | grep ":$PT_PORT ")
  echo "$c listener: $l" >> $O/listeners.txt
  if ! echo "$l" | grep -q "127.0.0.1:$PT_PORT "; then
    say "!! $c: perftest not on loopback ($l); killing it"
    for p in $(docker exec lmc-c pgrep -x ib_write_bw); do docker exec lmc-c kill -9 "$p"; done
    exit 1
  fi
  docker exec lmc-c bash -c "ib_write_bw $common 127.0.0.1 > /work${d#/root/lmc-work}/client.txt 2>&1" &
  local bg=$!
  sleep 3; capture $d 1; wait $bg
  say "$c: $(grep -E '^ *524288' $d/client.txt | tr -s ' ')"
  post $d 1 $d/client.txt
}

# sink <config> <full> <kvlayers args>
sink() {
  local c=$1 full=$2 d=$O/$1 bg; shift 2
  mkdir -p $d
  echo "${c}_start $(wc -l < $O/S_server/asd-kvsink-bp-perf.log)" >> $O/S_server/marks.txt
  kvl --qps 16 --duration $RUN_S "$@" > $d/kvl.txt 2>&1 & bg=$!
  sleep 3; capture $d $full; wait $bg
  echo "${c}_end $(wc -l < $O/S_server/asd-kvsink-bp-perf.log)" >> $O/S_server/marks.txt
  say "$c: $(grep -E '^stream:' $d/kvl.txt)"
  post $d $full $O/S_server/asd-kvsink-bp-perf.log
}

docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "asd is running; stopping"; exit 1; }
[ -x $NEW/asd ] || { say "no 0c9703931 binary"; exit 1; }
perftest P2 2
perftest P8 8
export PERF_OUT=$O/S_server CONF_TMPL=$MEM_CONF PERF_ASD_ENV="KV_SINK_STATS=1"
mkdir -p $O/S_server; rm -f $O/S_server/marks.txt
$PERF exp_start > $O/S_server/perf_start.txt 2>&1
grep -q 'started on' $O/S_server/progress.log || { say "server did not start"; exit 1; }
say "server: $(grep -o 'storage-engine=[a-z]*' $O/S_server/progress.log | tail -n 1); $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $O/S_server/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
kvl --fill --reps 1 > $O/S_server/kvl_fill.txt 2>&1
kvl --layers 16 --chunks 1 --prefix hot8 --fill --reps 1 > $O/S_server/kvl_fill_hot8.txt 2>&1
say "fills: $(grep -m1 failed $O/S_server/kvl_fill.txt | cut -c1-100); hot8: $(grep -m1 failed $O/S_server/kvl_fill_hot8.txt | cut -c1-100)"
sink S 1
sink S-hot 0 --layers 16 --chunks 1 --prefix hot8
$PERF exp_stop > $O/S_server/perf_stop.txt 2>&1
say "server stopped, data deleted"
$R report $O > $O/report.md
say "BREAKDOWN4 DONE"
