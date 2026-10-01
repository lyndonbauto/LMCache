#!/bin/bash
# T-FLT-07 feasibility probe for "mitigation 1" (approved 2026-10-01): netns
# kvs, veth vkvs0 (host, 10.250.0.1, rxe1) / vkvs1 (kvs, 10.250.0.2, rxe2),
# ibv_rc_pingpong rxe1 -> rxe2 with the server inside kvs, then a full
# teardown. Runs on the host; never touches lo or rxe0. Result on this box
# (v6.11 rdma_rxe): the client times out and kvs counts UdpNoPorts, because
# rxe's UDP 4791 socket exists only in the initial netns (stage5/CHANGES.md).
set -u
ip netns add kvs
ip link add vkvs0 type veth peer name vkvs1
ip link set vkvs1 netns kvs
sysctl -qw net.ipv6.conf.vkvs0.disable_ipv6=1
ip addr add 10.250.0.1/30 dev vkvs0; ip link set vkvs0 up
ip netns exec kvs sysctl -qw net.ipv6.conf.vkvs1.disable_ipv6=1
ip -n kvs addr add 10.250.0.2/30 dev vkvs1; ip -n kvs link set vkvs1 up; ip -n kvs link set lo up
rdma link add rxe1 type rxe netdev vkvs0
ip netns exec kvs rdma link add rxe2 type rxe netdev vkvs1
sleep 2
IMG=lmcache-rocm:day1
DEV="--device /dev/infiniband/uverbs1 --device /dev/infiniband/uverbs2 --cap-add IPC_LOCK --ulimit memlock=-1"
# server: in netns kvs (TCP 18999 only on the netns addresses), device rxe2
docker run -d --name flt07pp-srv --rm --network host $DEV --cap-add SYS_ADMIN --security-opt seccomp=unconfined -v /run/netns:/run/netns:ro $IMG \
  nsenter --net=/run/netns/kvs timeout 20 ibv_rc_pingpong -d rxe2 -g 1 -p 18999 -n 100 >/dev/null
sleep 2
ip netns exec kvs ss -ltn | grep 18999
ip -s link show vkvs0 | sed -n '3,6p'
docker run --rm --network host $DEV $IMG timeout 15 ibv_rc_pingpong -d rxe1 -g 1 -p 18999 -n 100 10.250.0.2 2>&1 | tail -4
echo "client exit=${PIPESTATUS[0]}"
docker logs flt07pp-srv 2>&1 | tail -4
ip -s link show vkvs0 | sed -n '3,6p'
ip netns exec kvs nstat -az UdpNoPorts IcmpOutDestUnreachs 2>/dev/null | tail -2
nstat -az UdpNoPorts 2>/dev/null | tail -1
docker rm -f flt07pp-srv >/dev/null 2>&1
ip netns exec kvs rdma link delete rxe2; rdma link delete rxe1; ip link delete vkvs0; ip netns delete kvs
echo "== after teardown"; rdma link show; ip netns list; ip -br link | grep vkvs || echo "no vkvs"
