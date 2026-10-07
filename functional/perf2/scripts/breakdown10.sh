#!/bin/bash
# perf2 breakdown, day 3 (Sriram, 2026-10-06 20:19 PT): what keeps half the
# box's CPUs idle? Server asd 3f3940e42 (same tree as 3b1d52fb3: locked
# posting, defaults cap 128 / 32 placers / 4 MiB per QP), kvlayers on client
# e8158149, memory namespace, 16 QPs, 512 KiB, KV_SINK_STATS=1, LMCache
# unchanged. No code changes; the server tree and binary are restored.
#
# Steps (STEPS):
#   build   asd 3f3940e42 in place over 9c16972132 (as breakdown8.sh), binary
#           kept in $NEW, tree and binary restored; kvlayers relinked against
#           e8158149 in its own client copy (as breakdown3.sh)
#   facts   setup and machine facts (docker inspect, lscpu, cmdline, rxe
#           module, workqueue cpumask, cpufreq / cpuidle, RAM) -> facts/
#   raw     ib_write_bw -s 524288 -q 16 -t 2 (E1 flags), 10 s -> raw/
#   runs    two server cycles, each: one fill, then kvlayers --qps 16
#           --duration 20 with mpstat -P ALL 1 10 (3 s in), top -H (two
#           frames 2 s apart, 6 s in; the second is kept) and /proc/interrupts
#           before and after the stream:
#             a  vLLM + LMCache MP server up and idle (a perf_session.sh aon
#                session held by a sleep step)
#             b  vLLM and the LMCache MP server stopped (how the breakdown
#                kvlayers runs ran)
# Results: /root/lmc-work/functional/perf2/breakdown10/.
set -u
T=/root/lmc-work/LMCache
SRC=/root/lmc-work/aerospike-server-kvsink-bp
ASD=$SRC/target/Linux-x86_64/bin/asd
ASD_ORIG_MD5=0b953fb421486d003e28ccc90dde2d7b
NEW=/root/lmc-work/asd-3f3940e42
SRC_TAR=$NEW/src-3f3940e42.tar
KVO=/root/lmc-work/kvlayers-build/client
KVN=/root/lmc-work/kvlayers-e8158149/client
CLI_TAR=/root/lmc-work/kvlayers-e8158149/src-e8158149.tar
O=/root/lmc-work/functional/perf2/breakdown10
R="python3 $O/../breakdown10_report.py"
PERF="bash $T/functional/perf/perf.sh"
MEM_CONF=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in
KVL_DIR=/work/kvlayers-e8158149/client/examples/kv_sink
STEPS=${STEPS:-build facts raw runs}
RUNS=${RUNS:-a b}
RUN_S=20
WIN_S=10
HOLD_S=90
export DATA_DIR=/mnt/scratch/perf-aero PERF_ASD=$NEW/asd
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
mkdir -p $O
say() { echo "$(date -u +%FT%TZ) breakdown10: $*" | tee -a $O/progress.log; }
touch_dependents() { grep -rl --include='*.c' 'kv_sink.h' $SRC/as/src | xargs touch; }
make_asd() { docker exec aero-kvsink-bp bash -c "cd $SRC && nice -n 19 make -j16" >> $O/build.txt 2>&1; }
kvl() { docker exec -w $KVL_DIR lmc-c timeout 300 ./kvlayers --host 127.0.0.1 --port $KVSINK_PORT \
  --transport rc --ns lmcache --set kvl --layers 32 --chunks 64 --blocksz 524288 "$@"; }
now() { date +%s.%N; }

step_build() {
  local files rc
  if [ -x $NEW/asd ]; then
    say "3f3940e42 binary already built"
  else
    files=$(tar tf $SRC_TAR | grep -v '/$')
    mkdir -p $NEW/orig
    [ "$(md5sum < $ASD | cut -c1-32)" = $ASD_ORIG_MD5 ] || { say "!! tree binary is not 9c16972132"; return 1; }
    for f in $files; do mkdir -p $NEW/orig/$(dirname $f); cp -a $SRC/$f $NEW/orig/$f || return 1; done
    cp -a $ASD $NEW/asd.9c16972132 || return 1
    tar xf $SRC_TAR -C $SRC && (cd $SRC && touch $files) && touch_dependents
    say "building 3f3940e42 in place"
    make_asd && cp -a $ASD $NEW/asd.new && mv $NEW/asd.new $NEW/asd
    rc=$?
    for f in $files; do cp -a $NEW/orig/$f $SRC/$f; done
    touch_dependents
    make_asd
    cp -a $NEW/asd.9c16972132 $ASD
    if [ "$(md5sum < $ASD | cut -c1-32)" != $ASD_ORIG_MD5 ]; then say "!! 9c16972132 binary not restored"; return 1; fi
    for f in $files; do cmp -s $SRC/$f $NEW/orig/$f || { say "!! $f not restored"; return 1; }; done
    say "tree and 9c16972132 binary restored ($ASD_ORIG_MD5)"
    [ $rc -eq 0 ] || { say "3f3940e42 build failed; see build.txt"; rm -f $NEW/asd; return 1; }
    strings $NEW/asd | grep -q 'MiB per path' || { say "!! new binary has no path budget"; rm -f $NEW/asd; return 1; }
    say "3f3940e42 binary $(md5sum < $NEW/asd | cut -c1-32)"
  fi
  if [ -x $KVN/examples/kv_sink/kvlayers ]; then say "e8158149 kvlayers already built"; return 0; fi
  rm -rf $KVN && mkdir -p $(dirname $KVN) && cp -a $KVO $KVN || return 1
  tar xf $CLI_TAR -C $KVN && (cd $KVN && touch $(tar tf $CLI_TAR | grep -v '/$')) || return 1
  cmp -s $KVN/src/main/aerospike/as_sink_verbs.c $KVO/src/main/aerospike/as_sink_verbs.c &&
    { say "!! e8158149 source not applied"; return 1; }
  docker exec lmc-c bash -c "cd ${KVN/\/root\/lmc-work/\/work} && nice -n 19 make -j16 EVENT_LIB=libuv && cd examples/kv_sink && rm -f kvlayers && make kvlayers" \
    >> $O/build_client.txt 2>&1 || { say "e8158149 kvlayers build failed; see build_client.txt"; return 1; }
  say "e8158149 kvlayers $(md5sum < $KVN/examples/kv_sink/kvlayers | cut -c1-32); old kvlayers $(md5sum < $KVO/examples/kv_sink/kvlayers | cut -c1-32)"
}

step_facts() {
  local d=$O/facts c
  mkdir -p $d
  for c in aero-kvsink-bp lmc-c; do
    docker inspect -f "$c: NanoCpus={{.HostConfig.NanoCpus}} CpuQuota={{.HostConfig.CpuQuota}} CpuPeriod={{.HostConfig.CpuPeriod}} CpuShares={{.HostConfig.CpuShares}} CpusetCpus='{{.HostConfig.CpusetCpus}}' CpusetMems='{{.HostConfig.CpusetMems}}' NetworkMode={{.HostConfig.NetworkMode}} IpcMode={{.HostConfig.IpcMode}} PidMode='{{.HostConfig.PidMode}}' Privileged={{.HostConfig.Privileged}} Memory={{.HostConfig.Memory}}" $c
  done > $d/docker.txt 2>&1
  {
    echo "## uname -r"; uname -r
    echo "## /proc/cmdline"; cat /proc/cmdline
    echo "## lscpu"; lscpu
    echo "## rdma_rxe (loaded)"; echo "srcversion $(cat /sys/module/rdma_rxe/srcversion)"
    modinfo -F srcversion /root/rxe-build/v6.11-20261007/rdma_rxe.ko | sed 's/^/file srcversion /'
    echo "file /root/rxe-build/v6.11-20261007/rdma_rxe.ko (upstream v6.11 rxe built for MLNX OFED 24.10, functional/HOST-CHANGES.md)"
    echo "## workqueue cpumask"; cat /sys/devices/virtual/workqueue/cpumask
    for w in /sys/devices/virtual/workqueue/*/cpumask; do echo "$w $(cat $w)"; done
    echo "## isolcpus / nohz_full"; cat /sys/devices/system/cpu/isolated /sys/devices/system/cpu/nohz_full 2>&1
    echo "## cpupower"; command -v cpupower >/dev/null && { cpupower frequency-info; cpupower idle-info; } || echo "cpupower not installed"
    echo "## cpufreq (cpu0)"
    for f in scaling_driver scaling_governor cpuinfo_min_freq cpuinfo_max_freq scaling_cur_freq; do
      echo "$f $(cat /sys/devices/system/cpu/cpu0/cpufreq/$f 2>&1)"
    done
    echo "## cpuidle driver"; cat /sys/devices/system/cpu/cpuidle/current_driver /sys/devices/system/cpu/cpuidle/current_governor* 2>&1
    echo "## cpuidle states (cpu0): name latency_us residency_us usage time_us disable"
    for s in /sys/devices/system/cpu/cpu0/cpuidle/state*; do
      echo "$(basename $s) $(cat $s/name) $(cat $s/latency) $(cat $s/residency) $(cat $s/usage) $(cat $s/time) $(cat $s/disable)"
    done
    echo "## free -g"; free -g
    echo "## numactl"; numactl -H 2>&1 | head -5
    echo "## irqbalance"; systemctl is-active irqbalance 2>&1
    echo "## steal since boot (/proc/stat cpu line)"; head -1 /proc/stat
  } > $d/machine.txt 2>&1
  say "facts: $(wc -l < $d/machine.txt) lines; $(head -1 $d/docker.txt | cut -c1-150)"
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
  docker exec lmc-c bash -c "ib_write_bw $common $* 127.0.0.1 > /work${d#/root/lmc-work}/${name}_client.txt 2>&1" &
  sleep 3
  mpstat -P ALL 1 5 > $d/${name}_mpstat.txt 2>&1
  wait
  say "$name: $(grep -E '^ *524288' $d/${name}_client.txt | tr -s ' ')"
}

step_raw() {
  local d=$O/raw
  mkdir -p $d; rm -f $d/listeners.txt
  docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "raw: asd is running; skipped"; return 1; }
  pt $d q16_t2 10 -q 16 -t 2
}

# vllm_procs: vLLM and LMCache processes in lmc-c (comm and command head).
vllm_procs() { docker exec lmc-c ps -eo pid,comm,args --no-headers | grep -E 'vllm|lmcache|VLLM' | grep -v grep | cut -c1-160; }

# run <a|b>: one server cycle; a holds vLLM + LMCache up during the stream.
run() {
  local name=$1 d=$O/run_$1 bg sess=""
  mkdir -p $d/clean
  export PERF_OUT=$d CONF_TMPL=$MEM_CONF PERF_ASD_ENV="KV_SINK_STATS=1"
  $PERF exp_start > $d/perf_start.txt 2>&1
  grep -q 'started on' $d/progress.log || { say "run $name: server did not start"; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1; }
  say "run $name: $(grep -E 'kv-sink: (at most|[0-9]+ placement)' $d/asd-kvsink-bp-perf.log | sed 's/.*kv-sink: //' | paste -sd',')"
  kvl --fill --reps 1 > $d/kvl_fill.txt 2>&1
  grep -q 'failed rows 0' $d/kvl_fill.txt || say "run $name: WARNING fill failed rows"
  if [ $name = a ]; then
    local W=/work${d#/root/lmc-work} aon_l2
    aon_l2="--no-l1-use-lazy --l2-adapter {\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$KVSINK_PORT\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"}"
    mkdir -p $d/sess
    timeout 1200 docker exec -e LMC_EXTRA="$aon_l2" -e L1_GB=100 -e STOP_GRACE=60 -e LW_WAIT_TIMEOUT= \
      -e STOP_ON_ENGINE_STOP=0 -e L2_PORT="$KVSINK_PORT" -e PERF_MODEL= \
      lmc-c bash /work/LMCache/functional/perf/perf_session.sh $W/sess sess aon "sleep secs=$HOLD_S" \
      > $d/sess/session_sess.txt 2>&1 & sess=$!
    for _ in $(seq 300); do [ -f $d/sess/warm_sess_1.json ] && break; kill -0 $sess 2>/dev/null || break; sleep 2; done
    [ -f $d/sess/warm_sess_1.json ] || { say "run $name: vLLM session did not come up"; kill $sess 2>/dev/null; $PERF exp_stop > $d/perf_stop.txt 2>&1; return 1; }
    sleep 5
    ss -ltnpH > $d/listeners.txt
    awk '{print $4}' $d/listeners.txt | grep -vE '^(127\.|\[::1\]|0\.0\.0\.0:22$|\[::\]:22$)' > $d/offloopback.txt
    [ -s $d/offloopback.txt ] && say "!! run $name: listener off loopback: $(paste -sd' ' $d/offloopback.txt)"
  fi
  vllm_procs > $d/procs.txt; say "run $name: $(wc -l < $d/procs.txt) vLLM/LMCache processes in lmc-c"
  cat /proc/interrupts > $d/clean/irq_before.txt
  echo "stream_start $(now)" > $d/clean/times_stream.txt
  kvl --qps 16 --duration $RUN_S > $d/clean/kvl.txt 2>&1 & bg=$!
  sleep 3
  echo "start $(now)" > $d/clean/times.txt
  ( sleep 3; top -b -n 2 -d 2 -H -w 400 > $d/clean/top_raw.txt 2>&1 ) &
  mpstat -P ALL 1 $WIN_S > $d/clean/mpstat.txt 2>&1
  echo "end $(now)" >> $d/clean/times.txt
  wait $bg
  cat /proc/interrupts > $d/clean/irq_after.txt
  echo "stream_end $(now)" >> $d/clean/times_stream.txt
  wait
  say "run $name: $(grep -E '^stream:' $d/clean/kvl.txt)"
  if [ -n "$sess" ]; then
    wait $sess
    say "run $name: session $(tail -1 $d/sess/session_sess.txt | cut -c1-100)"
  fi
  $PERF exp_stop > $d/perf_stop.txt 2>&1
  grep "stats:" $d/asd-kvsink-bp-perf.log > $d/stats_lines.txt
}

step_runs() {
  local r
  [ -x $NEW/asd ] || { say "runs: no 3f3940e42 binary"; return 1; }
  for r in $RUNS; do run $r || return 1; done
}

docker exec aero-kvsink-bp pgrep -x asd >/dev/null && { say "asd is running; stopping"; exit 1; }
while pgrep -f 'perf[.]sh' >/dev/null; do sleep 10; done
for s in $STEPS; do
  case $s in
    build) step_build || { say "build failed; stopping"; exit 1; } ;;
    facts) step_facts ;;
    raw) step_raw ;;
    runs) step_runs || say "runs stopped early" ;;
    *) say "unknown step $s" ;;
  esac
done
docker exec aero-kvsink-bp pgrep -x asd >/dev/null && say "!! asd still running" || say "server stopped, data deleted"
$R $O > $O/report.md 2>&1
say "BREAKDOWN10 DONE"
