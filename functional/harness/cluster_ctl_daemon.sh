#!/bin/bash
# Lets tests inside a container (lmc-c, which has no docker) drive cluster.sh
# or kvsink_cluster.sh on the host. Runs on the host, in the background:
#
#   nohup setsid functional/harness/cluster_ctl_daemon.sh > ctl.log 2>&1 &
#
# A client writes <id>.req into $CTL (the container sees it as
# /work/aero-cluster/ctl) holding one line: "<script> <command> [arg]", with
# script "cluster" or "kvsink". The daemon runs it, writes the output to
# <id>.out, then the exit code to <id>.rc, which the client polls for.
# Only the commands below are accepted. Stop the daemon by creating $CTL/stop.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
CTL=${AERO_CLUSTER_CTL:-/root/lmc-work/aero-cluster/ctl}
mkdir -p "$CTL"
rm -f "$CTL/stop"
echo "$(date -u +%FT%TZ) ctl daemon pid $$ watching $CTL"
while [ ! -e "$CTL/stop" ]; do
  for req in "$CTL"/*.req; do
    [ -e "$req" ] || continue
    id=$(basename "$req" .req)
    read -r script cmd arg < "$req"
    rm -f "$req"
    case "$script" in
      cluster) path=$HERE/cluster.sh ;;
      kvsink) path=$HERE/kvsink_cluster.sh ;;
      *) echo "bad script $script" > "$CTL/$id.out"; echo 2 > "$CTL/$id.rc"; continue ;;
    esac
    case "$cmd" in
      start|stop|status|kill-node|restart-node|stop-node|wait|warm) ;;
      *) echo "bad command $cmd" > "$CTL/$id.out"; echo 2 > "$CTL/$id.rc"; continue ;;
    esac
    echo "$(date -u +%FT%TZ) $id: $script $cmd ${arg:-}"
    "$path" "$cmd" ${arg:-} > "$CTL/$id.out" 2>&1
    rc=$?
    cat "$CTL/$id.out"
    echo "$rc" > "$CTL/$id.rc.tmp" && mv "$CTL/$id.rc.tmp" "$CTL/$id.rc"
  done
  sleep 0.2
done
echo "$(date -u +%FT%TZ) ctl daemon stopping"
