# One-shot runbook — Aerospike as an LMCache L2 backend

**Read this first. It is the ordered entry point for re-running the experiment.**

Session 1 (2026-09-14) completed Half A and lost Half B. It cost ~$110 and about
two hours, of which roughly half was avoidable. Everything avoidable is captured
here as a gate or a pinned value. Follow the order and the run should be clean.

| Doc | What it is for |
|---|---|
| **This file** | Ordered execution and the traps. Start here. |
| `README.md` | Teardown, prerequisites, verification commands. |
| `docs/findings.md` | The results, and corrections to earlier assumptions. |
| `docs/aerospike-tuning.md` | Namespace config reasoning (partly superseded — see its banner). |
| `docs/half-a-storage-ceiling.md` / `docs/half-b-ttft.md` | Per-half method. |
| `docs/instance-selection.md` / `docs/cost-estimate.md` | Why these instances, what they cost. |
| `docs/soft-roce.md` | Zero-cost local RDMA testing, no AWS needed. |

Jira: epic **AIE-85**, baseline **AIE-86**, RDMA gate **AIE-90**. The Jira comments
hold the decision trail (why the gate criteria changed); this repo holds the method.

---

## 0. Pre-flight gates — ALL of these before `terraform apply`

### 0.1 Prove GPU capacity FIRST. This is the lesson of session 1.

Half B was lost because we built the 5-node cluster, ran Half A, and *then*
discovered `g6e.12xlarge` had no capacity — `InsufficientInstanceCapacity`
continuously for 24 minutes, including outside the placement group. The cluster
then had to be destroyed with the TTFT breakdown unmeasured.

**There is no "is there capacity" API.** `describe-instance-type-offerings` tells
you a type is *offered*, not that it is *available*. The only real test is a launch:

```bash
# Launch one GPU instance in the target AZ, confirm it reaches running, terminate it.
# If this fails, DO NOT build the cluster -- you will pay $58/hr for a run you
# cannot finish. Either wait, or accept Half A only as a deliberate decision.
```

If GPU capacity is unavailable and you still want Half A, that is fine — but make
it an explicit choice up front, not a discovery two hours in.

### 0.2 Quotas

| Quota | Code | Session 1 value | Need |
|---|---|---|---|
| Standard On-Demand vCPU | `L-1216C47A` | 896 | 552 + GPU |
| **Elastic IPs** | `L-0263D0A3` | **5, 1 in use** | 1 (by design) |
| G/VT On-Demand vCPU | `L-DB2E81BA` | 768 | GPU node |

vCPU was never the constraint. **Elastic IPs were**, and would have failed the
apply *after* most of a $58/hr cluster existed. The design therefore uses one NAT
gateway plus SSM Session Manager, so no node is internet-reachable and only one
EIP is consumed regardless of cluster size. Do not "simplify" this back to a
public IP per node.

### 0.3 Feature key

Aerospike **Enterprise** is required. Session 1 used a key valid to 2027-01-15
with `asdb-cluster-nodes-limit 0`. Confirm not expired, then place at
`/etc/aerospike/features.conf`, mode `0600`, owner `aerospike:aerospike`,
referenced from the `service` stanza as `feature-key-file`.

**The key is gitignored. Never commit it.**

### 0.4 Guards before apply, not after

Budget alarm and the EventBridge + Lambda auto-teardown must exist *before* any
instance does. A guard added after the cluster is running was missing when it
mattered. $58/hr left over a weekend is ~$2,800 and blows through a $2,500 ceiling.

Auto-teardown must survive a closed laptop — EventBridge/Lambda, not a local `at`
job. `bin/extend-teardown.sh` extends the window; know that command before you
need it.

---

## 1. Pinned values — changing these will cost you hours

### Aerospike package

**Aerospike Enterprise `8.1.2.5`, `amzn2023` build.** Three attempts were needed:

- **el9 Enterprise on Amazon Linux 2023** — ABI-incompatible. Wants OpenLDAP 2.6,
  then core-dumps in OpenSSL init.
- **`8.0.0.7` amzn2023** — crashes against AL2023's current OpenSSL 3.5.7.
- **`8.1.2.5` amzn2023** — works.

No credentials are needed for the download; the feature key gates functionality,
not access to artifacts.

### AMI must be pinned

The AMI came from `data.aws_ssm_parameter` on `al2023-ami-kernel-default-x86_64`,
which resolves **latest**. AWS republished the image mid-session, a changed `ami`
forced replacement, and an unrelated `apply` **destroyed and rebuilt all six
nodes**, costing about an hour.

Fixed with `ignore_changes = [ami, user_data]`. Any stack resolving a "latest"
AMI has this grenade with whole-fleet blast radius.

### Instances and placement

5 × `i3en.24xlarge` + 1 × `c5n.18xlarge` + 1 × `g6e.12xlarge`, **same AZ, same
subnet, one cluster placement group**. EFA does not work across AZs.

Keep **all 8 NVMe devices per node**. At ~2 GB/s each, 8 devices (~16 GB/s) sit
above the 12.5 GB/s NIC, so the fabric is the bottleneck and you measure the
network rather than the device count. Four devices (~8 GB/s) would invalidate the
storage-ceiling result. The 60 TB/node capacity is incidental — you are buying
bandwidth, not space.

`i3en.12xlarge` also supports EFA at roughly half price, but at 50 Gbps. Cheaper,
and a weaker test of a fabric-bound hypothesis.

### Security group

Self-referencing rules on **both** ingress *and* egress, all protocols, all ports,
the group's own ID as source/destination. A missing **egress** self-reference lets
the connection appear to establish and then fails RDMA writes in a way that is
very hard to diagnose from the receiving side. There is a comment in the Terraform
saying so — do not let anyone clean it up.

---

## 2. Device selection — get this wrong and you destroy nodes

**Never select devices by `nvmeXn1` number.** In session 1 the EBS root volume
appeared as `nvme0n1` on some nodes, `nvme3n1` and `nvme4n1` on others, and it
*moved between two builds of the same cluster*. Writing to it destroys the node;
putting it in `aerospike.conf` destroys it on next start.

Require all of:

- `lsblk` MODEL exactly `Amazon EC2 NVMe Instance Storage` (EBS reports
  `Amazon Elastic Block Store`)
- TYPE `disk`
- no mountpoint on the disk or any child
- not the disk backing `/` per `findmnt`

Then **assert the count is exactly 8 and abort on mismatch** — a mismatch means
the identification logic is wrong, and guessing costs a node.

Wipe with `blkdiscard -f` (a TRIM, near-instant) then zero an 8 MiB header, then
**read back and assert all-zero**. Do not use `blkdiscard -z`: zeroing 7.5 TB ×
8 × 5 is not viable. Do not assume the wipe took — verifying is what caught it
the first time.

---

## 3. Ordered execution

1. Pre-flight gates (§0) — **including the GPU launch test**.
2. `terraform init && validate && plan`. Session 1 plan was `32 to add` (34 with GPU).
3. Guards apply first, then the fleet.
4. Verify, per `README.md` §Verification — do not trust any number before these pass:
   - bootstrap finished
   - EFA present *and functional*: `fi_info` shows the efa provider,
     `ibv_devinfo` reports `PORT_ACTIVE`, and **`fi_pingpong` node-to-node**, not
     just loopback. Node-to-node is what actually exercises the self-referencing
     security-group rules (~19–22 µs/xfer observed; ~15 µs loopback).
   - `cluster_size=5` with an identical cluster key on every node
   - **the record cap LMCache will actually discover**, via `asinfo` — see §4
5. Run Half A: storage ceiling (flash and DRAM) then the size sweep, both arms.
6. Run Half B: TTFT breakdown, MP mode.
7. Tear down and verify no orphans.

---

## 4. Traps that silently produce meaningless numbers

- **`max-record-size` is parsed before `write-block-size`** in
  `discover_record_cap()`, which returns on first hit. Raising only the intuitive
  knob leaves LMCache capped at the 1 MiB default with **no error and no log
  line**. Always gate the run on an `asinfo` check of the *live* value, never on
  the config file. `bin/verify-record-cap.sh`.
- **`write-block-size` no longer exists** in `asinfo` output on Aerospike 7.2+, so
  the fallback branch in `discover_record_cap()` is dead code on any modern server.
  *(Upstream LMCache bug — worth fixing.)*
- **The default 1 s L2 read timeout cannot complete 80 MiB.** The top of the sweep
  returns timeouts instead of latencies — precisely the region the sweep exists to
  characterise. Raise it explicitly and record the value used.
- **`lmcache bench l2` fails to load unless `openai` is installed**, because
  `engine_bench` imports it at parser-registration time and breaks the whole
  `bench` CLI group. *(Upstream LMCache bug — worth fixing.)*
- **RF=1.** RF=2 puts replica writes on the fabric under measurement and turns a
  storage-ceiling test into a replication-limited one.
- **`num_workers` parallelises across objects, not across segments of one object.**
  The sequential-segment chain is therefore invisible in throughput-only numbers.
  Report per-object p50/p99 **and round-trips-per-read**.
- **Aerospike holds the record lock across the DMA** in the RDMA PoC. If tail
  latency looks odd under mixed store/load traffic, suspect that before LMCache.

---

## 5. What session 1 already answered — do not spend money re-measuring

- **Storage ceiling: 12,450 reads/s flash vs 12,460 DRAM, both 12.2 GB/s = 98% of
  100 GbE line rate.** Removing the flash tier entirely changes throughput by
  **0.08%**. An L2 read here is a network problem, not a storage problem.
- **Therefore RDMA's throughput headroom is ~2%.** The remaining RDMA case is
  **CPU offload and per-object latency**, not bandwidth. AIE-90's success criteria
  were rewritten accordingly — a throughput-only result shows nothing and gets
  misread as "RDMA failed".
- **The sharding cliff is real at whatever the cap is**: +6.7% data → +106%
  latency at the 1 MiB boundary; +0.8% data → +60% latency at the 8 MiB boundary.
- **The arms cross over at ~8 MiB** (see the banner in `docs/aerospike-tuning.md`).
- **87 sequential round trips beat 12.** Issuing them *concurrently* is the
  cheapest win available and needs no RDMA, no protocol change, and none of the
  LMCache internals work in AIE-91–96.

**Still unmeasured, and it bounds the whole project: what share of TTFT is L2
fetch at all.** That is Half B. Until it runs, the maximum possible benefit of
the RDMA effort is unknown.

Also unmeasured: control-path cost — discovering which of 100+ chunks are cached
across the cluster.

---

## 6. The LMCache side

The RDMA receive path lives on branch **`feat/aerospike-rdma-l1-prototype`** in
the LMCache fork (`github.com/lyndonbauto/LMCache`), 10 commits based on `dev` at
`68b7e5f5`. Design notes at
`docs/design/v1/distributed/l2_adapters/aerospike_rdma.md` in that branch.

Verified: builds and links against real `libibverbs` (RC and EFA paths), default
no-RDMA build unaffected, 40 tests. **Unproven: no real RDMA write has ever
executed** — needs a Soft-RoCE device (`docs/soft-roce.md`) or EFA hardware.

Hard invariant enforced at startup: **the RDMA fetch timeout must be strictly less
than `write_ttl_seconds`** (default 600 s). The L1 `write_lock` is a `TTLLock`; if
a fetch outlives it the lock expires silently and the buffer becomes readable and
evictable *while a remote node may still be writing into it*.

**Blocker for pipelining, and it is on the Aerospike side:** the `kv-sink-fetch`
protocol makes the server fence on its own send CQ before replying, so a fetch is
atomically all-or-nothing by construction and there is no per-layer signal to
consume. LMCache already has request multiplexing and non-blocking polling, so a
pollable per-region progress counter on the Aerospike side would be sufficient —
a streaming channel is not required.

Also: `aerospike_info_foreach` **must not free** its response string, the opposite
of `aerospike_info_any`/`_node`/`_host`. Registration needs per-node fanout, so it
must use `_foreach`; pattern-matching on `discover_record_cap()` double-frees on
every node.
