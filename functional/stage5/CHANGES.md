# Stage 5: host changes

## gpu-stage5 (2026-10-02 03:17-05:35Z): GPU run on the new kv-sink stack

No host change: no package, kernel module, ufw, sshd, routing or container
configuration was changed, and no tc/netem/iptables rule was added. Every
service listened on 127.0.0.1. The public-listener check ran after every
LMCache and vLLM start and passed each time (`udp:4791` from `rdma_rxe` and
`tcp:22` allowed, as before).

| Change | Where | Approved by | State left |
|---|---|---|---|
| kv-sink server (`asd`) restarted (emptied) before every pipelined group, and SIGSTOPped for 2 s once (T-FLT-07 stand-in) | `aero-kvsink-bp`, 127.0.0.1:3700-3703; logs in each section's `kvsink_*/` | Work brief | Stopped |
| `lmcache.kv_chunks` truncated before every plain group | `aerospike-ce`, 127.0.0.1:3000 | Work brief (Aerospike containers on 127.0.0.1) | Up, set left with the last group's records |
| LMCache servers SIGKILLed (T-FLT-05) and SIGSTOPped 8 s (section 7); vLLM on 8000 | `lmc-c`; 127.0.0.1:6555 (ZMQ), 8080 (HTTP), 8000 | Work brief | None running; USED_VRAM 285 MB |
| Read-only Python stack captures of wedged / waiting vLLM engines (`nsenter -m -p` + `pystacks.py`) | host, against `lmc-c` processes | Work brief (documented trap) | n/a |
| Results | `/root/lmc-work/functional/stage5/` (`run1.txt`..`run4.txt` drivers; run 2's first sec7 pipe kept as `sec7/run2_sec7_pipe/`) | n/a | Kept |
| Harness | `fcbc9e1f`, `4070f7e3`, `f59ce1aa`, `da65e4ca` (`functional/stage5/` only) | Work brief (harness changes) | Pushed, box pulled |

The CE 3-node cluster (`aero-n1..n3`) was not started: no Stage 5 row
needs it. `lmc-b`, `lmc-d`, `lmc-newstack` and `aero-kvsink` (old server)
were not touched.

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

### Result: mitigation 1 does not work with this rdma_rxe

`ibv_rc_pingpong` rxe1 (host) to rxe2 (netns kvs) timed out (exit 124). The
TCP rendezvous over the veth worked, and 74 packets left vkvs0. But netns
kvs counted 65 `UdpNoPorts` and sent 7 ICMP port unreachables: nothing
listens on UDP 4791 there. Box evidence:
`/root/lmc-work/functional/stage5/flt07_probe/pingpong_netns.txt`.

The v6.11 rxe opens its RoCE UDP socket only in the initial netns
(`rxe_setup_udp_tunnel(&init_net, ...)`) and routes every send there too
(`ip_route_output_key(&init_net, ...)`), so an rxe device in another netns
can neither receive nor send. The alternatives (both rxe devices in the host
netns with a routing-policy change, or a newer netns-aware rdma_rxe) need
new approvals; see `SUMMARY.md`.
