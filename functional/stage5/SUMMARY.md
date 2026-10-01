# Stage 5 (E3 failure injection): summary

Status: the harness is prepared and its parsing was dry-run on the CPU
(2026-10-01, cpu-prep-s3s5). Nothing has run against vLLM yet. The runbook,
fault methods and pass criteria are in
[../stage3/HARNESS.md](../stage3/HARNESS.md).

| Test | Driver section | Status |
|---|---|---|
| T-FLT-01 | `stage5.sh flt01` | not run |
| T-FLT-05 (recompute and fail) | `stage5.sh flt05` | not run |
| T-FLT-06 | `stage5.sh flt06` | not run |
| T-FLT-07 | `stage5.sh flt07` | not run. Stand-in: kv-sink frozen 2 s. The approved veth/netns link (mitigation 1) was probed and does not work with this rdma_rxe (below) |
| Plan section 7 (stall past the worker's wait; the engine stops) | `stage5.sh sec7`, `count7` | not run |

The dry run (`stage5.sh dry`, box: `/root/lmc-work/functional/stage5/dry5.txt`)
checked the following:
- the `fault_inject` config (`gap_tail_ratios [0.5]` over the RDMA adapter)
  parses, and an LMCache server starts with it (`stage3/dryrun_server.sh`);
- the P-multi turn ids and the flt01 prompt list are as intended;
- `flt07_rdma_down.sh check` refuses, because the dedicated link does not
  exist. It leaves `lo`, rxe0 and eth0 alone.

## Needs a human decision: T-FLT-07

The RDMA path cannot be taken down on its own on this box. rxe0 is bound to
`lo` with GID 127.0.0.1, and its packets bypass `lo` qdiscs and netfilter
(stage3 SUMMARY, finding). Taking `lo` or rxe0 down would break every other
service, and the brief forbids reloading rdma_rxe.

The proposal (`flt07_rdma_down.sh` header) is a veth pair `lmcfa0`/`lmcfb0`,
with `lmcfb0` in a new netns, rxe devices `rxefa0`/`rxefb0` on top of them,
and the kv-sink server's RDMA side moved to that link. `flt07_rdma_down.sh
down 2` then drops only `lmcfa0`. This is a host change, so it needs a Slack
check-in first. Until then, `flt07` runs the stand-in, which exercises the
same LMCache timeout, quarantine and reuse path, but through a server stall
rather than a link loss.

**Update 2026-10-01 (cpu-prep-s4-flt07).** Lyndon Bauto approved this
proposal (mitigation 1) at 18:32 UTC. A probe built it (netns `kvs`, veth
`vkvs0`/`vkvs1`, rxe1 on the host end, rxe2 inside the netns) and tore it
down again; nothing is left. It does not work: rxe1 to rxe2
`ibv_rc_pingpong` timed out, and the netns counted 65 `UdpNoPorts`. The
v6.11 rdma_rxe loaded on this box opens its UDP 4791 socket and does its
route lookups only in the initial netns, so an rxe device inside another
netns can neither receive nor send. Details and undo log: `CHANGES.md`.
Options:

1. **Hairpin in the host netns.** Put both rxe devices in the host netns on
   two veth pairs, with netns `kvs` only forwarding between them. Move the
   host's `local` fib rule from pref 0 to after two `ip rule from 10.250.0.1
   to 10.250.1.1` rules (and the reverse), and set `accept_local` on the two
   host veths. ufw stays unchanged: incoming is allowed by default, and the
   destination is still a local address. `down 2` would down the netns-side
   veth (`ip -n kvs link set ... down`), leaving host routes alone. This is
   a system-wide routing-policy change, so it needs a new approval.
2. **Newer rdma_rxe.** Build and load one with per-netns sockets. Loading it
   means `rmmod`, which deletes rxe0; recreate it between GPU work items.
   The build against the OFED core may need porting.
3. **Default: keep the stand-in.** Run T-FLT-07 with the kv-sink frozen for
   2 s, mark it partial, and do the real link-down on a fabric (E5a).

Options 1 and 2 also need LMCache to see the new uverbs device. `lmc-c` maps
only `/dev/infiniband/uverbs0`, so Stage 5 would run in a new container
from `lmcache-rocm:day1` with lmc-c's flags plus `uverbs1`. The kv-sink
server under test would run in a container that sees only the server-side
device (the server takes the first verbs device, issue 6), with its TCP
service on 127.0.0.1.
