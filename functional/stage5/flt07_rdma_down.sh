#!/bin/bash
# T-FLT-07: take ONLY the RDMA path down for a few seconds during a fetch.
# Runs on the host. Not run yet: `down` refuses until the dedicated link
# below exists, which needs a host change approved in Slack.
#
# Why not rxe0, lo, netem or iptables: rxe0 sits on lo, and client and
# kv-sink server both use GID 1 = 127.0.0.1, so rdma_rxe takes its loopback
# shortcut (rxe_net.c: saddr == daddr sets RXE_LOOPBACK_MASK and
# rxe_loopback() hands the packet straight to rxe_rcv). RDMA packets never
# reach lo's qdisc, netfilter or counters: during T-RDMA-06, lo carried
# 6.72 GB per run, exactly the TCP stores and plain gets (2 x 3.36 GB), and
# none of the 3.36 GB of RDMA writes. So `tc netem` or an iptables rule on
# UDP 4791 does nothing, and taking lo (or rxe0) down also cuts every TCP
# service on 127.0.0.1 (Aerospike, LMCache, vLLM) and is not allowed.
#
# What works: a second Soft-RoCE device on its own link, with the kv-sink
# server on the far side of that link. A veth pair whose two ends are in the
# same network namespace does not work either: both addresses are local, so
# packets route over lo and land on rxe0. The far end must be in another
# network namespace:
#
#   ip netns add kvsink-net
#   ip link add lmcfa0 type veth peer name lmcfb0
#   ip link set lmcfb0 netns kvsink-net
#   ip addr add 10.250.0.1/30 dev lmcfa0 && ip link set lmcfa0 up
#   ip -n kvsink-net addr add 10.250.0.2/30 dev lmcfb0
#   ip -n kvsink-net link set lmcfb0 up && ip -n kvsink-net link set lo up
#   rdma link add rxefa0 type rxe netdev lmcfa0                      # client side
#   ip netns exec kvsink-net rdma link add rxefb0 type rxe netdev lmcfb0   # server side
#   # a kv-sink container joined to kvsink-net (docker run --network none,
#   # then move it, or `ip netns exec`), seeing only rxefb0's uverbs device
#   # (the server takes the first device; RC GID index 1 = 10.250.0.2,
#   # server issue 6), and LMCache pointed at 10.250.0.2:3100 with
#   # device_name rxefa0, gid_index 1.
#
# Adding rxe devices needs no rdma_rxe reload and leaves rxe0 alone, but it
# is a host network change (new netns, veth, rxe devices): ask first. Until
# then Stage 5 runs T-FLT-07's stand-in: freeze the kv-sink server for 2 s
# right after a lookup (host_actions.sh freeze_kvsink). That stalls the RDMA
# writes and the server's TCP service together, so it shows timeout,
# quarantine, reuse and output equality, but not a link-only failure.
#
# Usage: flt07_rdma_down.sh check | down <seconds>
#   check  print whether the dedicated link exists and what `down` would touch
#   down   ip link set lmcfa0 down, sleep, up (client side of the veth only;
#          refuses if lmcfa0 is missing, carries the default route, or is lo)
set -u
DEV=${FLT07_DEV:-lmcfa0}

guard() {
  [ "$DEV" != lo ] || { echo "refusing: $DEV is lo"; return 1; }
  ip link show "$DEV" > /dev/null 2>&1 || {
    echo "refusing: $DEV does not exist (dedicated RDMA link not set up; see the header)"; return 1; }
  if ip route show default | grep -qw "dev $DEV"; then
    echo "refusing: $DEV carries the default route"; return 1
  fi
  rdma link show 2>/dev/null | grep -qw "netdev $DEV" || {
    echo "refusing: no rxe device on $DEV"; return 1; }
}

case "${1:-}" in
  check)
    if guard; then
      echo "ready: $DEV exists, no default route, rxe on it: $(rdma link show | grep -w "netdev $DEV")"
    fi
    echo "default route: $(ip route show default)"
    echo "rxe devices: $(rdma link show 2>/dev/null | paste -sd';')"
    ;;
  down)
    secs=${2:?seconds}
    guard || exit 1
    t0=$(date -u +%T.%N | cut -c1-12)
    ip link set "$DEV" down
    sleep "$secs"
    ip link set "$DEV" up
    echo "$DEV down at $t0 for $secs s, up at $(date -u +%T.%N | cut -c1-12)"
    ;;
  *)
    echo "usage: $0 check | down <seconds>"; exit 2
    ;;
esac
