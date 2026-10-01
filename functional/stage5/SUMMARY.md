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
| T-FLT-07 | `stage5.sh flt07` | not run. Stand-in: kv-sink frozen 2 s. The real link-down needs approval for a host change (below) |
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
