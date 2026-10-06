#!/bin/bash
# perf2 breakdown, day 2 (Sriram, 2026-10-05 19:29 PT). Server
# sriram/kv-sink-batch-prio 314564cfb, client 5a24afdb, LMCache unchanged,
# KV_SINK_STATS=1 on every server start.
#
# Steps (STEPS):
#   q1  memory namespace, cap 32:
#       A  perf record -a -g -F 999 (5 s) during ib_write_bw -s 524288 -q 16
#          -t 2, and during kvlayers --qps 16 --duration 8
#       B  wire us per write, hot (--chunks 2 --prefix hot, 32 MiB) against
#          cold (--chunks 64, 1 GiB), kvlayers --qps 16 --duration 8, twice
#       C  ib_write_bw -q 16 -t 2 alone, then alongside kvlayers --qps 1
#          --duration 15, then alone again
#   q2  device namespace, defaults: 64 prompts stored, then one aon point 8k
#       c=16 n=64 (four waves from Aerospike), with
#       D  perf record -a -g (5 s) once the point starts, and the namespace's
#          read counters around the window
#       E  enable-benchmarks-read and -batch-sub on namespace lmcache for the
#          point (the namespace is lmcache, not kvcache), then off
#   Q3 (G) is breakdown.sh's devp16/devp32/devp64 steps.
# Results: /root/lmc-work/functional/perf2/breakdown2/<step>/.
set -u
T=/root/lmc-work/LMCache
O=/root/lmc-work/functional/perf2/breakdown2
NEW=/root/lmc-work/asd-314564cfb
PERF="bash $T/functional/perf/perf.sh"
H=$T/functional/harness
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
DEV_CONF=$T/functional/configs/aerospike-kvsink-bp-perf.conf.in
KVL_DIR=/work/kvlayers-build/client/examples/kv_sink
STEPS=${STEPS:-q1 q2}
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown2: $*" | tee -a $O/progress.log; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
# mark <dir> <name>: the server log's line count, to slice its stats lines.
mark() { echo "$2 $(date -u +%FT%TZ) $(wc -l < $1/asd-kvsink-bp-perf.log)" >> $1/marks.txt; }
asinfo() { python3 $H/as_info.py "$KVSINK_PORT" "$1"; }
ns_reads() { asinfo namespace/lmcache | tr ';' '\n' | grep -E '^(client_read_success|batch_sub_read_success)=' | paste -sd' '; }

# prof <dir> [after-record command]: perf record -a -g -F 999 for 5 s, then
# the command (run as soon as recording stops), then sample-count reports.
prof() {
  mkdir -p $1
  mpstat -P ALL 1 5 > $1/mpstat.txt 2>&1 &
  local m=$!
  perf record -a -g -F 999 -o $1/perf.data -- sleep 5 > $1/perf_record.txt 2>&1
  [ -n "${2:-}" ] && eval "$2"
  wait $m
  perf report -i $1/perf.data -n --no-children --sort comm,dso,symbol -g none --stdio 2>/dev/null \
    > $1/perf_full.txt
  perf report -i $1/perf.data -n --no-children --sort comm,dso -g none --stdio 2>/dev/null \
    > $1/perf_comm_dso.txt
  perf report -i $1/perf.data -n --no-children --sort symbol --symbol-filter=spin_lock \
    -g caller,0.5,callee,function,percent --stdio 2>/dev/null | head -n 400 > $1/perf_locks.txt
  rm -f $1/perf.data
}

# pt <dir> <name> <secs> <ib_write_bw args>: perftest pair in lmc-c, the
# server's socket on 127.0.0.1 (checked; anything else is killed).
PT_PORT=18700
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

step_q1() {
  local d=$O/q1 i bg
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF PERF_ASD_ENV="KV_SINK_STATS=1"
  mkdir -p $d; rm -f $d/marks.txt $d/listeners.txt
  docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "q1: asd is running; stopping"; return 1; }
  say "q1 A: perftest q16 t2, profiled"
  pt $d A_perftest 12 -q 16 -t 2 & bg=$!
  sleep 4; prof $d/A_perftest; wait $bg
  $PERF exp_start > $d/perf_start.txt 2>&1
  grep -q 'started on' $d/progress.log || { say "q1: server did not start"; return 1; }
  say "q1: $(grep -o 'storage-engine=[a-z]*' $d/progress.log | tail -n 1); $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  kvl --chunks 2 --prefix hot --fill --reps 1 > $d/kvl_fill_hot.txt 2>&1
  say "q1: fills exit, cold: $(grep -m1 failed $d/kvl_fill.txt | cut -c1-120); hot: $(grep -m1 failed $d/kvl_fill_hot.txt | cut -c1-120)"
  say "q1 A: kvlayers qps 16, profiled"
  mark $d A_kvl_start
  kvl --qps 16 --duration 8 > $d/kvl_A.txt 2>&1 & bg=$!
  sleep 2; mark $d A_kvl_prof_start; prof $d/A_kvlayers "mark $d A_kvl_prof_end"; wait $bg
  mark $d A_kvl_end
  say "q1 A: kvlayers $(grep -E '^stream:' $d/kvl_A.txt)"
  for i in 1 2; do
    mark $d B_hot${i}_start
    kvl --chunks 2 --prefix hot --qps 16 --duration 8 > $d/kvl_B_hot$i.txt 2>&1
    mark $d B_hot${i}_end
    say "q1 B: hot $i $(grep -E '^stream:' $d/kvl_B_hot$i.txt)"
    mark $d B_cold${i}_start
    kvl --qps 16 --duration 8 > $d/kvl_B_cold$i.txt 2>&1
    mark $d B_cold${i}_end
    say "q1 B: cold $i $(grep -E '^stream:' $d/kvl_B_cold$i.txt)"
  done
  pt $d C_alone1 10 -q 16 -t 2
  mark $d C_with_start
  kvl --qps 1 --duration 15 > $d/kvl_C.txt 2>&1 & bg=$!
  sleep 2; pt $d C_with_kvl 10 -q 16 -t 2; wait $bg
  mark $d C_with_end
  say "q1 C: kvlayers alongside $(grep -E '^stream:' $d/kvl_C.txt)"
  pt $d C_alone2 10 -q 16 -t 2
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  say "q1: server stopped, data deleted"
}

# q2_watch <dir>: benchmarks on after the store, the profile once the point
# starts, benchmarks off when the session ends.
q2_watch() {
  local d=$1 s=$1/L8192_aon/session_L8192_aon.txt
  for _ in $(seq 1800); do grep -q 'store done' $s 2>/dev/null && break; sleep 1; done
  asinfo 'set-config:context=namespace;id=lmcache;enable-benchmarks-read=true' > $d/bench_on.txt
  asinfo 'set-config:context=namespace;id=lmcache;enable-benchmarks-batch-sub=true' >> $d/bench_on.txt
  echo "$(wc -l < $d/asd-kvsink-bp-perf.log)" > $d/bench_logmark.txt
  say "q2 E: benchmarks on ($(paste -sd' ' $d/bench_on.txt))"
  for _ in $(seq 1800); do grep -q 'point L8192_c16' $s 2>/dev/null && break; sleep 0.5; done
  sleep 0.5
  echo "start $(date -u +%T.%N) $(ns_reads)" > $d/D_reads.txt
  prof $d/D_aon 'echo "end $(date -u +%T.%N) $(ns_reads)" >> $d/D_reads.txt'
  say "q2 D: profiled; reads $(paste -sd' ' $d/D_reads.txt)"
}

step_q2() {
  local d=$O/q2 w
  export PERF_OUT=$d CONF_TMPL=$DEV_CONF PERF_ASD_ENV="KV_SINK_STATS=1"
  export FS_PCT=400 STORE_IDS=0-63 CONCS=16 POINT_N=64
  mkdir -p $d
  q2_watch $d & w=$!
  $PERF precheck qpstore:8192 > $d/perf_q2.txt 2>&1
  wait $w
  sleep 12
  asinfo 'set-config:context=namespace;id=lmcache;enable-benchmarks-read=false' > $d/bench_off.txt
  asinfo 'set-config:context=namespace;id=lmcache;enable-benchmarks-batch-sub=false' >> $d/bench_off.txt
  tail -n +$(( $(cat $d/bench_logmark.txt) + 1 )) $d/asd-kvsink-bp-perf.log |
    grep -A 3 -E '\{lmcache\}-(read|batch-sub)' | grep -vE '\(0 total\)' > $d/E_histograms.txt
  say "q2 E: benchmarks off ($(paste -sd' ' $d/bench_off.txt)); $(grep -c 'histogram dump' $d/E_histograms.txt) non-empty histograms"
  say "q2: $(grep -h '^point' $d/L8192_aon/session_L8192_aon.txt | cut -c1-140 | paste -sd' ')"
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  say "q2: server stopped, data deleted"
}

for s in $STEPS; do
  case $s in
    q1) step_q1 ;;
    q2) step_q2 ;;
    *) say "unknown step $s" ;;
  esac
done
say "BREAKDOWN2 DONE"
