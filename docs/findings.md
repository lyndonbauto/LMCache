# Findings — things that contradict or complicate the AIE-86 assumptions

Ordered by how much they should change someone's plan.

---

## 1. AMD + EFA + local NVMe does not exist outside GPU/FPGA families

**Contradicts the project premise's reference to AMD hardware.**

125 instance types in us-east-1 have both EFA and local NVMe. 19 are AMD, and
every one of them is an accelerated type: `g5.*`, `g6.*`, `gr6.8xlarge`,
`g6e.*`, `p5.4xlarge`, `p5.48xlarge`, `f2.48xlarge`.

There is no AMD general-purpose, compute-optimised, memory-optimised or
storage-optimised instance with EFA and local flash. The AMD EFA-capable
general-purpose types (`c6a`/`m6a`/`r6a.48xlarge`) are EBS-only **and capped at
50 Gbps**, half the Intel options.

You can have AMD, EFA and local NVMe together only by paying for GPUs you will
not use (`g6e.48xlarge`, $30.13/hr, 2.8x the cost of `i3en.24xlarge`).

**Recommendation:** run the storage tier on Intel `i3en.24xlarge`. Neither
Aerospike's flash engine nor the EFA/SRD path is meaningfully CPU-vendor
sensitive, and the quantities being measured (flash throughput, NIC saturation)
are not either. If AMD validation is a hard requirement for reasons outside this
benchmark, that needs to be raised now, because it changes the instance budget.

---

## 2. Elastic IP quota would have failed the apply — architecture changed in response

**This one would have burned real money before failing.**

`aws service-quotas get-service-quota --service-code ec2 --quota-code L-0263D0A3`
returns **5**, and `describe-addresses` shows **1 already in use** — 4 available.

The obvious design (one public IP per node) needs 6, or 7 with the GPU node. It
would have failed with `AddressLimitExceeded` *after* Terraform had already
created most of a $58/hr cluster, leaving a partially-applied stack that still
bills.

**Resolved in the Terraform, not deferred to a quota request.** The stack now
uses a single NAT gateway (1 EIP, constant regardless of cluster size) in a tiny
dedicated public subnet, with all cluster nodes in one private EFA subnet and
operator access via SSM Session Manager. The plan now creates exactly one EIP.

This is strictly better anyway: no cluster node is reachable from the internet.
Cost is ~$0.045/hr plus $0.045/GB, negligible at this scale.

---

## 3. The vCPU quota is NOT a blocker — the usual assumption is wrong here

The brief anticipated that 480 vCPUs would exceed a default account quota. It
does not, in this account.

| Quota | Code | Limit | Needed | Headroom |
|---|---|---|---|---|
| Running On-Demand Standard (A,C,D,H,I,M,R,T,Z) | `L-1216C47A` | **896** | 552 | 344 |
| Running On-Demand G and VT | `L-DB2E81BA` | **768** | 48 | 720 |
| Running On-Demand P | `L-417A185B` | **768** | 96 | 672 |
| VPCs per Region | `L-F678F1CE` | 15 | 1 | 14 |

Needed = 5 x 96 (`i3en.24xlarge`) + 72 (`c5n.18xlarge`) = **552 standard vCPUs**
against a limit of 896. `describe-instances` shows **zero running instances**,
so the full limit is currently available.

**No quota increase is required. Nothing is blocked on a multi-day lead time.**

One caveat: this is a **shared account** (it already contains an Omnistrate VPC,
an AKO-peering VPC, and 22 key pairs belonging to other people). The 344 vCPU
headroom assumes nobody else launches a large fleet during the benchmark window.
Re-check `describe-instances` immediately before applying.

---

## 4. Raising `write-block-size` alone silently does nothing

`discover_record_cap()` scans the info reply for `max-record-size=` **before**
`write-block-size=`, and takes the first value greater than zero.

So if an operator raises `write-block-size` to 8M — the intuitive knob — and
leaves `max-record-size` at its 1 MiB default, LMCache reads 1 MiB, subtracts the
64 KiB margin, and shards 80 MiB payloads into 86 segments exactly as before. No
error, no warning, no log line. The server looks tuned and the benchmark
quietly produces the untuned numbers.

Both values must be set to 8M. The runbook gates on an `asinfo` check rather
than trusting the config file, because the context and defaults for
`max-record-size` moved between Aerospike 6.x and 7.x/8.x.

The safe sub-case: `max-record-size=0` (meaning "unlimited") is correctly
rejected by the connector's `cap > 0` guard and falls through to
`write-block-size`.

---

## 5. The sequential-segment weakness is bigger than "a known weakness"

`do_single_get()` reads the meta record, then loops the segments **sequentially
on one worker**. Round trips per read = `1 + nseg`, fully dependent.

At the default 1 MiB cap, an 80 MiB chunk is **87 serial round trips**. At an
8 MiB cap it is **12**.

Two consequences that are easy to miss:

- **Tuning the server gets a 7.25x reduction in the serial chain for free**,
  before anyone writes a line of RDMA code. Some of the benefit currently
  attributed to the RDMA project may be obtainable by a config change plus a
  batched client read (`aerospike_batch_read` over the segment keys).
- **The connector's `num_workers` does not help.** Workers parallelise across
  *objects*, not across the segments of one object. So per-object p99 is
  unaffected by concurrency, which means the weakness is invisible in a
  throughput-only benchmark and only shows up in per-object tail latency. Half A
  is designed to separate these.

Half B should carry this in: if `MP_RETRIEVE` latency tracks segment count
rather than bytes, part of the measured "L2 fetch cost" is a client-side bug,
not a transport limitation.

---

## 6. Default client timeouts will fail the top of the sweep

The Aerospike L2 adapter defaults to `read_timeout_ms: 1000`,
`write_timeout_ms: 2000`. An 80 MiB object at the 1 MiB cap is 87 sequential
round trips plus 80 MiB of transfer — it will not complete in 1 second against
flash.

Left at defaults, the large end of the sweep returns timeouts rather than
latencies, and the most interesting data points are the ones that go missing.
The benchmark spec in `docs/half-a-storage-ceiling.md` sets both to 30000.

---

## 7. Single EFA interface per node caps single-node bandwidth

Every candidate reports `EfaInfo.MaximumEfaInterfaces = 1`. There is no
multi-rail EFA on these types, so per-node RDMA bandwidth is bounded by the NIC
line rate: 12.5 GB/s on a 100 Gbps `i3en.24xlarge`.

Worth stating explicitly because the aggregate cluster figure (5 x 12.5 =
62.5 GB/s) is only reachable with the read load spread evenly across all five
nodes. Any hot-key or partition skew in the benchmark shows up as a bandwidth
ceiling that looks like a storage limit but is not.

---

## 8. `us-east-1d` is the only AZ offering every candidate type

EFA cannot cross an AZ and the cluster placement group is AZ-scoped, so this is
a one-shot decision. Of the six AZs, only `us-east-1d` offers all of
`i3en.24xlarge`, `i4i.32xlarge`, `c5n.18xlarge`, `p4d.24xlarge`, `p5.48xlarge`
and `g6e.48xlarge`. Choosing anything else forecloses some Half B GPU options
and would mean rebuilding the stack to change GPU type later.

Separately: a cluster placement group requesting five identical
`i3en.24xlarge` is a realistic `InsufficientInstanceCapacity` risk at apply
time, since AWS must place them physically close together. That is an
apply-time risk, not a plan-time one — the fallback is in the runbook.
