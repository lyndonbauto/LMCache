#!/bin/bash
# A 3-node kv-sink server cluster: three asd processes from the kv-sink build
# (/root/lmc-work/aerospike-server-kvsink) in the aero-kvsink container, for
# T-RDMA-05 and T-FLT-04. Runs on the host. Same commands as cluster.sh:
#
#   kvsink_cluster.sh start [rf] | stop | status | kill-node N |
#                     restart-node N | wait [size] | warm
#
# Node N: service 34N0, fabric 34N1, heartbeat 34N2, admin 34N3 (N-1 in the
# tens digit: n1 = 3400-3403, n2 = 3410-3413, n3 = 3420-3423), 127.0.0.1.
# The namespace is memory-only: a killed or restarted node comes back empty.
# "warm" stores and reads one record per node with kv-sink-fetch so each
# node registers its memory stripes now (server issue 5); start and
# restart-node run it automatically.
set -u
CTR=aero-kvsink
ASD=/root/lmc-work/aerospike-server-kvsink/target/Linux-x86_64/bin/asd
BASE=${KVSINK_CLUSTER_BASE:-/root/lmc-work/aero-cluster/kvsink}
HERE=$(cd "$(dirname "$0")" && pwd)
TEMPLATE=$HERE/../configs/cluster/aerospike-kvsink-node.conf.template
INFO="python $HERE/as_info.py"
WAIT_S=${KVSINK_CLUSTER_WAIT_S:-180}
NODES="1 2 3"

svc_port() { echo $((3400 + ($1 - 1) * 10)); }

node_pid() {
  docker exec "$CTR" pgrep -f "kvsink-n$1.conf"
}

stat() {
  docker exec "$CTR" $INFO "$(svc_port "$1")" statistics 2>/dev/null \
    | tr ';' '\n' | sed -n "s/^$2=//p"
}

running_nodes() {
  local n out=""
  for n in $NODES; do node_pid "$n" >/dev/null && out="$out $n"; done
  echo $out
}

render() {
  local rf=$1 n other seeds svc
  for n in $NODES; do
    mkdir -p "$BASE/n$n/work/smd" "$BASE/n$n/work/usr/udf/lua"
    seeds=""
    for other in $NODES; do
      [ "$other" = "$n" ] && continue
      seeds="$seeds        mesh-seed-address-port 127.0.0.1 $(( $(svc_port "$other") + 2 ))\\n"
    done
    svc=$(svc_port "$n")
    sed -e "s/@NODE@/n$n/g" -e "s/@N@/$n/g" -e "s|@WORK@|$BASE/n$n/work|g" \
        -e "s/@SVC@/$svc/g" -e "s/@FAB@/$((svc + 1))/" -e "s/@HB@/$((svc + 2))/" \
        -e "s/@ADMIN@/$((svc + 3))/" -e "s/@RF@/$rf/" -e "s|@SEEDS@|${seeds%\\n}|" \
        "$TEMPLATE" > "$BASE/kvsink-n$n.conf"
  done
  echo "$rf" > "$BASE/rf"
}

start_node() {
  local n=$1
  node_pid "$n" >/dev/null && return 0
  docker exec -d "$CTR" bash -c "ulimit -l unlimited; ulimit -n 20000; exec $ASD --config-file $BASE/kvsink-n$n.conf --foreground >> $BASE/n$n/asd.log 2>&1"
}

stop_node() {
  local n=$1 pid t
  pid=$(node_pid "$n") || return 0
  docker exec "$CTR" kill -TERM "$pid"
  for t in $(seq 1 30); do node_pid "$n" >/dev/null || { echo "stopped n$n"; return 0; }; sleep 1; done
  docker exec "$CTR" kill -9 "$pid"; echo "killed -9 n$n"
}

wait_cluster() {
  local want=${1:-} nodes n size mig ok t stable=0
  nodes=$(running_nodes)
  [ -z "$want" ] && want=$(echo $nodes | wc -w)
  for t in $(seq 1 "$WAIT_S"); do
    ok=1
    for n in $nodes; do
      size=$(stat "$n" cluster_size); mig=$(stat "$n" migrate_partitions_remaining)
      if [ "$size" != "$want" ] || [ "$mig" != 0 ]; then ok=0; break; fi
    done
    if [ $ok = 1 ]; then stable=$((stable + 1)); else stable=0; fi
    if [ $stable -ge 3 ]; then
      echo "kv-sink cluster size $want on nodes [$nodes], migrations done (${t}s)"
      return 0
    fi
    sleep 1
  done
  echo "timed out after ${WAIT_S}s waiting for size $want"; status_all; return 1
}

warm() {
  docker exec "$CTR" python "$HERE/kvsink_warm.py" $(for n in $(running_nodes); do svc_port "$n"; done)
}

status_all() {
  local n
  echo "rf=$(cat "$BASE/rf" 2>/dev/null || echo '?')"
  for n in $NODES; do
    if pid=$(node_pid "$n"); then
      echo "n$n pid=$pid service=127.0.0.1:$(svc_port "$n") cluster_size=$(stat "$n" cluster_size)" \
        "migrate_partitions_remaining=$(stat "$n" migrate_partitions_remaining)" \
        "build=$(docker exec "$CTR" $INFO "$(svc_port "$n")" build 2>/dev/null)"
    else
      echo "n$n stopped"
    fi
  done
}

node_arg() { case "${1:-}" in 1|2|3) ;; *) echo "node must be 1, 2 or 3"; exit 2 ;; esac; }

case "${1:-}" in
  start)
    rf=${2:-$(cat "$BASE/rf" 2>/dev/null || echo 1)}
    if [ "$(cat "$BASE/rf" 2>/dev/null)" != "$rf" ]; then for n in $NODES; do stop_node "$n"; done; fi
    render "$rf"
    for n in $NODES; do start_node "$n"; done
    wait_cluster 3 && warm
    ;;
  stop) for n in $NODES; do stop_node "$n"; done ;;
  status) status_all ;;
  kill-node)
    node_arg "${2:-}"
    pid=$(node_pid "$2") || { echo "n$2 not running"; exit 1; }
    docker exec "$CTR" kill -9 "$pid" && echo "killed n$2 (SIGKILL, pid $pid)"
    sleep 1; wait_cluster
    ;;
  stop-node) node_arg "${2:-}"; stop_node "$2"; wait_cluster ;;
  restart-node)
    node_arg "${2:-}"; stop_node "$2" >/dev/null; start_node "$2"; echo "started n$2"
    wait_cluster && warm
    ;;
  wait) wait_cluster "${2:-}" ;;
  warm) warm ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
