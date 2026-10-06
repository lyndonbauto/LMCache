#!/bin/bash
# perf2 breakdown, day 2 round 4 (Sriram, 2026-10-06 13:19 PT): why each QP's
# send task idles for the sink. asd 0c9703931 at defaults (32 placers, cap
# 128), kvlayers on client e8158149, memory namespace, LMCache unchanged,
# KV_SINK_STATS=1. 16 QPs, 512 KiB.
#
# Configs: P8 (ib_write_bw -q 16 -t 8, no server) and S (kvlayers --qps 16).
# Each config runs twice for 25 s (bpftrace takes ~4 s to attach, so its 10 s
# must still end inside the run); the window starts 3 s in:
#   <cfg>-A  rdma statistic show link rxe0/1 before and after the window
#   <cfg>-B  the same counters around Sriram's bpftrace script (rxe_requester
#            exit reasons, retransmit / RNR timers), so its overhead doesn't
#            touch A
# The rxe module (v6.11 build) has no BTF, so the script reads the two flags by
# offset from the module's debug info (pahole): qp->req.wait_psn at 1136,
# qp->need_req_skb at 1856, plus qp->req.wait_fence and qp->req.need_rd_atomic
# to split Sriram's nothing_or_other. The v6.11 retransmit timer is
# retransmit_timer.
# Results: /root/lmc-work/functional/perf2/breakdown5/<cfg>-<A|B>/.
set -u
T=/root/lmc-work/LMCache
NEW=/root/lmc-work/asd-0c9703931
O=/root/lmc-work/functional/perf2/breakdown5
R="python3 $O/../breakdown5_report.py"
PERF="bash $T/functional/perf/perf.sh"
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
KO=/root/rxe-build/v6.11/rdma_rxe.ko
RUN_S=25
WIN_S=10
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown5: $*" | tee -a $O/progress.log; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
now() { date +%s.%N; }

[ "$(cat /sys/module/rdma_rxe/srcversion)" = "$(modinfo -F srcversion $KO)" ] || {
  say "loaded rdma_rxe is not $KO; offsets would be wrong"; exit 1; }
pahole -C rxe_qp $KO > $O/pahole_rxe_qp.txt 2>&1
pahole -C rxe_req_info $KO > $O/pahole_rxe_req_info.txt 2>&1
off() { awk -v f="$1;" '$NF=="*/" { for (i = 1; i < NF; i++) if ($i == f) print $(NF-2) }' $2 | head -n 1; }
REQ=$(off req $O/pahole_rxe_qp.txt)
WAIT=$(off wait_psn $O/pahole_rxe_req_info.txt)
NEED=$(off need_req_skb $O/pahole_rxe_qp.txt)
FENCE=$(off wait_fence $O/pahole_rxe_req_info.txt)
RDA=$(off need_rd_atomic $O/pahole_rxe_req_info.txt)
[ -n "$REQ" ] && [ -n "$WAIT" ] && [ -n "$NEED" ] && [ -n "$FENCE" ] && [ -n "$RDA" ] || {
  say "offsets not found"; exit 1; }
WAIT_OFF=$((REQ + WAIT)) FENCE_OFF=$((REQ + FENCE)) RDA_OFF=$((REQ + RDA))
say "offsets: req.wait_psn $WAIT_OFF, need_req_skb $NEED, req.wait_fence $FENCE_OFF, req.need_rd_atomic $RDA_OFF"
cat > $O/exits.bt <<EOF
kprobe:rxe_requester { @calls = count(); @qp[tid] = arg0; }
kretprobe:rxe_requester /@qp[tid]/ {
  \$qp = @qp[tid];
  if ((int32)retval == -11) {
    if (*(int32 *)(\$qp + $WAIT_OFF)) { @exit["window_full"] = count(); }
    else if (*(int32 *)(\$qp + $NEED)) { @exit["rx_backed_up"] = count(); }
    else if (*(int32 *)(\$qp + $FENCE_OFF)) { @exit["wait_fence"] = count(); }
    else if (*(int32 *)(\$qp + $RDA_OFF)) { @exit["need_rd_atomic"] = count(); }
    else { @exit["nothing_or_other"] = count(); }
  } else { @exit["sent_packet"] = count(); }
  delete(@qp[tid]);
}
kprobe:retransmit_timer { @timer["retransmit"] = count(); }
kprobe:rnr_nak_timer { @timer["rnr"] = count(); }
interval:s:$WIN_S { exit(); }
EOF

counters() { rdma statistic show link rxe0/1; }

# window <dir> <with bpftrace: 1 or 0>
window() {
  local d=$1 bt=$2
  mkdir -p $d
  echo "start $(now)" > $d/times.txt
  counters > $d/counters_before.txt
  if [ "$bt" = 1 ]; then
    bpftrace -f json $O/exits.bt > $d/bpftrace.json 2> $d/bpftrace.err
  else
    sleep $WIN_S
  fi
  counters > $d/counters_after.txt
  echo "end $(now)" >> $d/times.txt
}

PT_PORT=18950
# perftest <dir> <with bpftrace>
perftest() {
  local d=$1 bt=$2 l bg
  mkdir -p $d; PT_PORT=$((PT_PORT + 1))
  local common="-d rxe0 -x 1 -m 4096 -s 524288 -D $RUN_S -F --report_gbits -p $PT_PORT -q 16 -t 8"
  docker exec -d lmc-c bash -c "ib_write_bw $common --bind_source_ip 127.0.0.1 > /work${d#/root/lmc-work}/server.txt 2>&1"
  sleep 1
  l=$(ss -ltnp | grep ":$PT_PORT ")
  echo "$(basename $d) listener: $l" >> $O/listeners.txt
  if ! echo "$l" | grep -q "127.0.0.1:$PT_PORT "; then
    say "!! perftest not on loopback ($l); killing it"
    for p in $(docker exec lmc-c pgrep -x ib_write_bw); do docker exec lmc-c kill -9 "$p"; done
    exit 1
  fi
  docker exec lmc-c bash -c "ib_write_bw $common 127.0.0.1 > /work${d#/root/lmc-work}/client.txt 2>&1" &
  bg=$!
  sleep 3; window $d $bt; wait $bg
  say "$(basename $d): $(grep -E '^ *524288' $d/client.txt | tr -s ' ')"
}

# sink <dir> <with bpftrace>
sink() {
  local d=$1 bt=$2 bg
  mkdir -p $d
  kvl --qps 16 --duration $RUN_S > $d/kvl.txt 2>&1 & bg=$!
  sleep 3; window $d $bt; wait $bg
  say "$(basename $d): $(grep -E '^stream:' $d/kvl.txt)"
}

docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "asd is running; stopping"; exit 1; }
[ -x $NEW/asd ] || { say "no 0c9703931 binary"; exit 1; }
perftest $O/P8-A 0
perftest $O/P8-B 1
export PERF_OUT=$O/S_server CONF_TMPL=$MEM_CONF PERF_ASD_ENV="KV_SINK_STATS=1"
mkdir -p $O/S_server
$PERF exp_start > $O/S_server/perf_start.txt 2>&1
grep -q 'started on' $O/S_server/progress.log || { say "server did not start"; exit 1; }
kvl --fill --reps 1 > $O/S_server/kvl_fill.txt 2>&1
say "fill: $(grep -m1 failed $O/S_server/kvl_fill.txt | cut -c1-100)"
sink $O/S-A 0
sink $O/S-B 1
$PERF exp_stop > $O/S_server/perf_stop.txt 2>&1
grep "stats:" $O/S_server/asd-kvsink-bp-perf.log > $O/S_server/stats_lines.txt
say "server stopped, data deleted"
$R $O > $O/report.md
say "BREAKDOWN5 DONE"
