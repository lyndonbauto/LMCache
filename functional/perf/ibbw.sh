#!/bin/bash
# E1 (LW-INVESTIGATION section 7): raw Soft-RoCE ceiling on rxe0 with perftest.
# Runs in lmc-c (host network; the host's MLNX OFED libibverbs has no rxe
# provider, and a separate network namespace cannot use rxe0's GIDs, which
# belong to the host's lo). The server's out-of-band socket is bound with
# --bind_source_ip 127.0.0.1 and checked with ss before each run; a server
# found listening off loopback is killed and the sweep stops.
set -u
# IBBW_SET=sink_depths: only ib_write_bw at the kv-sink server's depths.
OUT=${IBBW_OUT:-/root/lmc-work/functional/perf/ibbw}
# lmc-c mounts /root/lmc-work at /work.
OUTC=/work${OUT#/root/lmc-work}
C=lmc-c
mkdir -p $OUT
rm -f $OUT/*.txt
docker exec $C bash -c 'command -v ib_write_bw >/dev/null || (apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq perftest >/dev/null 2>&1); ib_write_bw --version | head -1; ibv_devinfo -d rxe0 | grep -E "state|active_mtu"' > $OUT/setup.txt 2>&1
cat $OUT/setup.txt
port=18600
COMMON="-d rxe0 -x 1 -m 4096 -s 524288 -D 10 -F --report_gbits"
run() {  # run <name> <tool> <args...>
  local name=$1 tool=$2; shift 2
  port=$((port + 1))
  docker exec -d $C bash -c "$tool $COMMON -p $port --bind_source_ip 127.0.0.1 $* > $OUTC/${name}_server.txt 2>&1"
  sleep 1
  local l; l=$(ss -ltnp | grep ":$port ")
  echo "$name listener: $l" >> $OUT/listeners.txt
  if ! echo "$l" | grep -q "127.0.0.1:$port "; then
    echo "!! $name: server not on loopback ($l); killing it"
    for pid in $(docker exec $C pgrep -x "$tool"); do docker exec $C kill -9 "$pid"; done
    exit 1
  fi
  if [ "$name" = write_q1_t8 ]; then
    ( sleep 4; top -b -n 3 -d 2 -w 200 > $OUT/top_${name}.txt 2>&1 ) &
    ( head -n 1 /proc/stat > $OUT/stat0_$name.txt; sleep 9; head -n 1 /proc/stat > $OUT/stat1_$name.txt ) &
  fi
  docker exec $C bash -c "$tool $COMMON -p $port --cpu_util $* 127.0.0.1 > $OUTC/${name}_client.txt 2>&1; echo rc=\$? >> $OUTC/${name}_client.txt"
  wait
  sleep 2
  echo "$name: $(grep -E '^ *524288' $OUT/${name}_client.txt | tr -s ' ') $(grep rc= $OUT/${name}_client.txt)"
}
if [ "${IBBW_SET:-}" = sink_depths ]; then
  # The kv-sink server's depths (perf2 breakdown run A).
  run write_q1_t8 ib_write_bw -q 1 -t 8
  run write_q8_t4 ib_write_bw -q 8 -t 4
  run write_q16_t2 ib_write_bw -q 16 -t 2
  run write_q16_t8 ib_write_bw -q 16 -t 8
else
  run write_q1_t8 ib_write_bw -q 1 -t 8
  run write_q4_t8 ib_write_bw -q 4 -t 8
  run write_q8_t8 ib_write_bw -q 8 -t 8
  run write_q1_t128 ib_write_bw -q 1 -t 128
  run write_q4_t128 ib_write_bw -q 4 -t 128
  run write_q8_t128 ib_write_bw -q 8 -t 128
  run read_q1 ib_read_bw -q 1
  run read_q4 ib_read_bw -q 4
fi
cat $OUT/listeners.txt
