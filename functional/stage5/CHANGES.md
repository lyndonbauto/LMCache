# Stage 5: host changes

## cpu-prep-s4-flt07 (2026-10-01): T-FLT-07 veth/netns probe

Approved by Lyndon Bauto (Slack, 2026-10-01 18:32 UTC, mitigation 1): a veth
pair into a separate network namespace with its own rxe device, so the RDMA
path alone can be taken down for 2 s.

The approved topology was built twice for a feasibility probe and torn down
both times, about 1 minute in total. **Nothing is left behind.** rxe0, `lo`,
rdma_rxe, ufw, sshd, the rdma netns mode (`shared`, unchanged), the GPU, ports
8000/6555, `aerospike-ce` and `/root/lmc-work/LMCache` were not touched.

| Time (UTC) | Change | Undo (run at the time) |
|---|---|---|
| 19:13 | `ip netns add kvs`; `ip link add vkvs0 type veth peer name vkvs1`; `ip link set vkvs1 netns kvs`; IPv6 off on both ends (`net.ipv6.conf.vkvs0.disable_ipv6=1`, same for vkvs1 in kvs); `10.250.0.1/30` on vkvs0, `10.250.0.2/30` on vkvs1; links and kvs `lo` up; `rdma link add rxe1 type rxe netdev vkvs0`; `ip netns exec kvs rdma link add rxe2 type rxe netdev vkvs1` (`flt07_probe.sh`) | `ip netns exec kvs rdma link delete rxe2; rdma link delete rxe1; ip link delete vkvs0; ip netns delete kvs` (19:14) |
| 19:14 | Throwaway containers from `lmcache-rocm:day1` (`--rm`, host network, `/dev/infiniband/uverbs1`, `uverbs2`) for `ibv_devinfo`/`ibv_rc_pingpong` | `--rm`; nothing left |
| 19:16 | The same topology again (`flt07_netns_probe.sh`), plus container `flt07pp-srv` (`--rm`, `--cap-add SYS_ADMIN`, seccomp unconfined, `/run/netns` read-only) running `ibv_rc_pingpong` server inside netns kvs via `nsenter --net=/run/netns/kvs` (TCP 18999 bound in the netns only, never on a host interface) | The script's own teardown, same commands as above, plus `docker rm -f flt07pp-srv` (19:16). Checked afterwards: `rdma link show` lists only rxe0 (ACTIVE), `ip netns list` is empty, no `vkvs*` links, `ibv_devinfo -d rxe0` PORT_ACTIVE |

## Result: mitigation 1 does not work with this rdma_rxe

`ibv_rc_pingpong` rxe1 (host) to rxe2 (netns kvs) timed out (exit 124). The
TCP rendezvous over the veth worked (the server listened on 0.0.0.0:18999
*inside* kvs and the client connected to 10.250.0.2), and 74 packets left
vkvs0. But netns kvs counted 65 `UdpNoPorts` and sent 7 ICMP port
unreachables: nothing listens on UDP 4791 there. Box evidence:
`/root/lmc-work/functional/stage5/flt07_probe/pingpong_netns.txt`.

The cause is in the module's source (`/root/rxe-build/v6.11/rxe_net.c`, the
upstream Linux v6.11 rxe loaded with insmod, Day 1). It opens its RoCE UDP
socket only in the initial netns (`rxe_setup_udp_tunnel(&init_net, ...)`),
and routes every send in the initial netns too (`ip_route_output_key(&init_net, ...)`).
So an rxe device on an interface in another netns can neither receive (no
socket there) nor send to a host address (the route lookup hits the host's
local table, so packets go over `lo` to rxe0, which drops them). Making rxe
netns-aware means loading a newer rdma_rxe. That needs `rmmod` and
recreating rxe0, which this work item forbids.

The other ways to do it also need changes beyond mitigation 1:
- **Both rxe devices in the host netns, with the netns as a wire between two
  veth pairs.** The host routes traffic to its own addresses through `lo`,
  because the `local` table is consulted first (fib rule pref 0). The RDMA
  packets would only cross the veths if that rule moved after a pair of
  `ip rule from 10.250.0.1 to 10.250.1.1` rules. Moving it is a system-wide
  routing-policy change.
- **The same, but deleting the two local routes instead.** ufw's
  `ufw-not-local` chain then drops the packets, because their destination
  is no longer a local address type. Getting past it needs an iptables
  rule, which is a firewall change.

The options are listed in `flt07_rdma_down.sh` (header), `SUMMARY.md` and
`../OPEN-GAPS.md` G-13. Stage 5 keeps the stand-in (kv-sink frozen 2 s)
until a human picks one.
