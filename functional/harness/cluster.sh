#!/bin/bash
# The E4 cluster: three Aerospike CE 8.2 nodes on this host (functional test
# plan section 3), one container each (aero-n1..aero-n3, host networking,
# image aerospike/aerospike-server:latest, the image aerospike-ce runs).
# Runs on the host.
#
#   cluster.sh start [rf]      create or start all three nodes with replication
#                              factor rf (default: the last one used, else 1);
#                              a different rf restarts every node with it
#   cluster.sh stop            stop all three (containers and data are kept)
#   cluster.sh status          per node: running, cluster size, migrations,
#                              master/replica objects in namespace lmcache
#   cluster.sh kill-node N     SIGKILL node N's asd, then wait for the other
#                              two to agree on a 2-node cluster
#   cluster.sh restart-node N  start node N again (or restart it if running)
#   cluster.sh wait [size]     wait for cluster size (default: running nodes)
#                              on every running node and no migrations left
#   cluster.sh wipe            remove every node's data file and container
#                              (cluster stopped)
#
# Node N: service 33N0, fabric 33N1, heartbeat 33N2, info 33N3 (N-1 in the
# tens digit: n1 = 3300-3303, n2 = 3310-3313, n3 = 3320-3323), all 127.0.0.1.
# Data: /root/lmc-work/aero-cluster/nN/lmcache.dat (sparse, 16 GiB).
# A private cluster beside this one (dry runs): AERO_CLUSTER_PORT_BASE (default
# 3300), AERO_CLUSTER_PREFIX (container names, default aero-n) and
# AERO_CLUSTER_BASE (data and configs, default /root/lmc-work/aero-cluster).
set -u
IMAGE=aerospike/aerospike-server:latest
BASE=${AERO_CLUSTER_BASE:-/root/lmc-work/aero-cluster}
HERE=$(cd "$(dirname "$0")" && pwd)
TEMPLATE=${AERO_CLUSTER_TEMPLATE:-$HERE/../configs/cluster/aerospike-node.conf.template}
WAIT_S=${AERO_CLUSTER_WAIT_S:-180}
NODES="1 2 3"

PORT_BASE=${AERO_CLUSTER_PORT_BASE:-3300}
PREFIX=${AERO_CLUSTER_PREFIX:-aero-n}
svc_port() { echo $((PORT_BASE + ($1 - 1) * 10)); }
ctr() { echo "$PREFIX$1"; }

running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$(ctr "$1")" 2>/dev/null)" = true ]
}

running_nodes() {
  local n out=""
  for n in $NODES; do running "$n" && out="$out $n"; done
  echo $out
}

# stat N FIELD: one field of node N's "statistics" info reply, empty on error.
stat() {
  docker exec "$(ctr "$1")" asinfo -h 127.0.0.1 -p "$(svc_port "$1")" \
    -v statistics 2>/dev/null | tr ';' '\n' | sed -n "s/^$2=//p"
}

ns_stat() {
  docker exec "$(ctr "$1")" asinfo -h 127.0.0.1 -p "$(svc_port "$1")" \
    -v namespace/lmcache 2>/dev/null | tr ';' '\n' | sed -n "s/^$2=//p"
}

render() {
  local rf=$1 n seeds other
  mkdir -p "$BASE/conf"
  for n in $NODES; do
    mkdir -p "$BASE/n$n"
    seeds=""
    for other in $NODES; do
      [ "$other" = "$n" ] && continue
      seeds="$seeds        mesh-seed-address-port 127.0.0.1 $(( $(svc_port "$other") + 2 ))\\n"
    done
    local svc; svc=$(svc_port "$n")
    sed -e "s/@NODE@/n$n/g" -e "s/@N@/$n/g" -e "s/@NODE_ID@/a$n/" \
        -e "s/@SVC@/$svc/g" -e "s/@FAB@/$((svc + 1))/" \
        -e "s/@HB@/$((svc + 2))/" -e "s/@INFO@/$((svc + 3))/" \
        -e "s/@RF@/$rf/" -e "s|@SEEDS@|${seeds%\\n}|" \
        "$TEMPLATE" > "$BASE/conf/n$n.conf"
  done
  echo "$rf" > "$BASE/rf"
}

start_node() {
  local n=$1 name; name=$(ctr "$n")
  if running "$n"; then return 0; fi
  if docker inspect "$name" >/dev/null 2>&1; then
    docker start "$name" >/dev/null
  else
    docker run -d --name "$name" --network host --ulimit nofile=20000:20000 \
      -v "$BASE/conf:/etc/aerospike-e4:ro" -v "$BASE/n$n:/opt/aerospike/data" \
      --entrypoint /usr/bin/asd "$IMAGE" \
      --foreground --config-file "/etc/aerospike-e4/n$n.conf" >/dev/null
  fi
}

wait_cluster() {
  local want=${1:-} nodes n size mig ok t stable=0
  nodes=$(running_nodes)
  [ -z "$want" ] && want=$(echo $nodes | wc -w)
  for t in $(seq 1 "$WAIT_S"); do
    ok=1
    for n in $nodes; do
      size=$(stat "$n" cluster_size)
      mig=$(stat "$n" migrate_partitions_remaining)
      if [ "$size" != "$want" ] || [ "$mig" != 0 ]; then ok=0; break; fi
    done
    # Migrations start a moment after the cluster re-forms, so one good poll
    # can come before they do: require three in a row.
    if [ $ok = 1 ]; then stable=$((stable + 1)); else stable=0; fi
    if [ $stable -ge 3 ]; then
      echo "cluster size $want on nodes [$nodes], migrations done (${t}s)"
      return 0
    fi
    sleep 1
  done
  echo "timed out after ${WAIT_S}s waiting for size $want / no migrations"
  status_all
  return 1
}

stop_all() {
  local n
  for n in $NODES; do
    if running "$n"; then docker stop -t 30 "$(ctr "$n")" >/dev/null && echo "stopped n$n"; fi
  done
}

status_all() {
  local n
  echo "rf=$(cat "$BASE/rf" 2>/dev/null || echo '?')"
  for n in $NODES; do
    if running "$n"; then
      echo "n$n $(ctr "$n") running service=127.0.0.1:$(svc_port "$n")" \
        "cluster_size=$(stat "$n" cluster_size)" \
        "migrate_partitions_remaining=$(stat "$n" migrate_partitions_remaining)" \
        "master_objects=$(ns_stat "$n" master_objects)" \
        "prole_objects=$(ns_stat "$n" prole_objects)"
    else
      echo "n$n $(ctr "$n") stopped"
    fi
  done
}

node_arg() {
  case "${1:-}" in 1|2|3) ;; *) echo "node must be 1, 2 or 3"; exit 2 ;; esac
}

case "${1:-}" in
  start)
    rf=${2:-$(cat "$BASE/rf" 2>/dev/null || echo 1)}
    case "$rf" in 1|2|3) ;; *) echo "rf must be 1, 2 or 3"; exit 2 ;; esac
    if [ "$(cat "$BASE/rf" 2>/dev/null)" != "$rf" ] && [ -n "$(running_nodes)" ]; then
      echo "replication factor changes to $rf: restarting every node"
      stop_all
    fi
    render "$rf"
    for n in $NODES; do start_node "$n"; done
    wait_cluster 3
    ;;
  stop)
    stop_all
    ;;
  status)
    status_all
    ;;
  kill-node)
    node_arg "${2:-}"
    docker kill -s KILL "$(ctr "$2")" >/dev/null && echo "killed n$2 (SIGKILL)"
    wait_cluster
    ;;
  restart-node)
    node_arg "${2:-}"
    if running "$2"; then docker restart -t 30 "$(ctr "$2")" >/dev/null; else start_node "$2"; fi
    echo "started n$2"
    wait_cluster
    ;;
  wait)
    wait_cluster "${2:-}"
    ;;
  wipe)
    if [ -n "$(running_nodes)" ]; then echo "stop the cluster first"; exit 1; fi
    # The containers hold each node's SMD (truncations, roster), so remove
    # them too; the next start creates them fresh.
    for n in $NODES; do
      rm -f "$BASE/n$n/lmcache.dat"
      docker rm "$(ctr "$n")" >/dev/null 2>&1 || true
    done
    echo "wiped data files under $BASE/n{1,2,3} and removed the containers"
    ;;
  *)
    sed -n "2,22p" "$0"; exit 2
    ;;
esac
