#!/bin/bash
# Start, stop or check the Aerospike kv-sink server in the aero-kvsink container.
# Runs on the host. Usage: kvsink_server.sh start|stop|status
#   KVSINK_CONF  server config (default: functional/configs/aerospike-kvsink.conf in the clone)
#   KVSINK_LOG   server log    (default: /root/lmc-work/functional/stage3/logs/asd-kvsink.log)
set -u
CTR=aero-kvsink
ASD=/root/lmc-work/aerospike-server-kvsink/target/Linux-x86_64/bin/asd
CONF=${KVSINK_CONF:-/root/lmc-work/LMCache-cpu/functional/configs/aerospike-kvsink.conf}
LOG=${KVSINK_LOG:-/root/lmc-work/functional/stage3/logs/asd-kvsink.log}
DATA=/root/lmc-work/aerospike-kvsink-data/work

asd_pid() {
  docker exec "$CTR" pgrep -x asd
}

case "${1:-}" in
  start)
    if pid=$(asd_pid); then echo "already running: pid $pid"; exit 0; fi
    mkdir -p "$DATA/smd" "$DATA/usr/udf/lua" "$(dirname "$LOG")"
    # proto-fd-max 15000 needs more than docker exec's default 1024 open files.
    docker exec -d "$CTR" bash -c "ulimit -l unlimited; ulimit -n 20000; exec $ASD --config-file $CONF --foreground >> $LOG 2>&1"
    for _ in $(seq 1 60); do
      if docker exec "$CTR" python -c "import socket; socket.create_connection(('127.0.0.1', 3100), 1)" 2>/dev/null; then
        echo "started: pid $(asd_pid), service 127.0.0.1:3100, log $LOG"
        exit 0
      fi
      sleep 1
    done
    echo "did not come up in 60 s; tail of $LOG:"; tail -n 20 "$LOG"; exit 1
    ;;
  stop)
    pid=$(asd_pid) || { echo "not running"; exit 0; }
    docker exec "$CTR" kill -TERM "$pid"
    for _ in $(seq 1 30); do
      asd_pid >/dev/null || { echo "stopped (pid $pid)"; exit 0; }
      sleep 1
    done
    docker exec "$CTR" kill -9 "$pid"; echo "killed -9 (pid $pid)"
    ;;
  status)
    if pid=$(asd_pid); then echo "running: pid $pid"; else echo "not running"; fi
    ;;
  *)
    echo "usage: $0 start|stop|status"; exit 2
    ;;
esac
