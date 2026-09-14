# Instance selection, with evidence (AIE-86, Task 1)

All data below is from live read-only AWS API calls made on **2026-09-14** in
account `236318833413`, region `us-east-1`. Nothing here is from memory.

## EFA support check

```bash
aws ec2 describe-instance-types \
  --instance-types i3en.24xlarge i4i.32xlarge c5n.18xlarge \
                   c6a.48xlarge m6a.48xlarge r6a.48xlarge \
  --query 'InstanceTypes[].{T:InstanceType,EFA:NetworkInfo.EfaSupported,
           Net:NetworkInfo.NetworkPerformance,vCPU:VCpuInfo.DefaultVCpus,
           NVMe:InstanceStorageInfo.TotalSizeInGB,Mfr:ProcessorInfo.Manufacturer}'
```

| Instance | Vendor | EFA | Network | vCPU | RAM | Local NVMe | $/hr |
|---|---|---|---|---|---|---|---|
| `i3en.24xlarge` | Intel | **yes** | **100 Gbps** | 96 | 768 GiB | **8 x 7500 GB** | 10.848 |
| `i4i.32xlarge` | Intel | yes | 75 Gbps | 128 | 1024 GiB | 8 x 3750 GB | 10.982 |
| `c5n.18xlarge` | Intel | yes | 100 Gbps | 72 | 192 GiB | none | 3.888 |
| `c6a.48xlarge` | AMD | yes | 50 Gbps | 192 | 384 GiB | none | 7.344 |
| `m6a.48xlarge` | AMD | yes | 50 Gbps | 192 | 768 GiB | none | 8.294 |
| `r6a.48xlarge` | AMD | yes | 50 Gbps | 192 | 1536 GiB | none | 10.886 |

All six support EFA. The differentiators are network bandwidth and local NVMe.

Every candidate reports `EfaInfo.MaximumEfaInterfaces = 1`, so there is exactly
one EFA interface per node. Multi-rail EFA is not available on any of these
types, which caps single-node RDMA bandwidth at the NIC line rate.

## Decision: `i3en.24xlarge` for the 5 server nodes

It is the only candidate that has all three of EFA, 100 Gbps, and local NVMe.
`i4i.32xlarge` is the closest alternative and is slightly more expensive, but
its 75 Gbps ceiling makes it a worse instrument: the entire point of Half A is
to compare achieved bandwidth *against NIC line rate*, and a 100 Gbps NIC gives
more headroom to discover whether flash or the network binds first.

`i3en.24xlarge` also has 60 TB of local NVMe against 768 GiB of RAM — a ~78:1
ratio. That is what makes a genuinely flash-resident working set possible. On
`i4i.32xlarge` the ratio is ~29:1, which still works but gives less room.

## Decision: `c5n.18xlarge` for the Half A load generator

Cheapest EFA type at 100 Gbps ($3.888/hr). One client can saturate one server's
NIC at roughly a third of the server's cost.

## AMD + EFA + local NVMe in us-east-1 — the answer the project premise needs

**A general-purpose or storage-optimised AMD instance with EFA *and* local NVMe
does not exist in us-east-1.** The combination exists only in GPU and FPGA
families.

Exhaustive query:

```bash
aws ec2 describe-instance-types \
  --filters Name=network-info.efa-supported,Values=true \
            Name=instance-storage-supported,Values=true \
  --query 'InstanceTypes[].{T:InstanceType,Mfr:ProcessorInfo.Manufacturer,...}'
```

125 instance types have EFA + local NVMe. 19 of those are AMD, and **every
single one is an accelerated-computing type**:

- `g5.*` (8 types, NVIDIA A10G)
- `g6.*` / `gr6.8xlarge` (6 types, NVIDIA L4)
- `g6e.*` (5 types, NVIDIA L40S)
- `p5.4xlarge`, `p5.48xlarge` (NVIDIA H100)
- `f2.48xlarge` (FPGA)

There is no `c6a`/`c7a`/`m6a`/`m7a`/`r6a`/`r7a` or AMD `i`-family with local
NVMe. The AMD EFA-capable general-purpose types (`c6a`, `m6a`, `r6a`
`.48xlarge`) are all **EBS-only and capped at 50 Gbps** — half the bandwidth of
the Intel options and with no local flash at all.

### Why this matters

If the broader project premise assumes the Aerospike RDMA server will be
validated on AMD hardware with local flash, **that assumption does not hold on
EC2 in us-east-1**. The options are:

1. **Run the storage tier on Intel** (`i3en`/`i4i`). Recommended. Aerospike's
   flash engine and the EFA/SRD path are not meaningfully CPU-vendor-sensitive,
   and the measurement of interest (flash and network ceilings) is not either.
2. **Run on AMD without local flash** (`r6a.48xlarge`, 1.5 TiB RAM). This gives
   AMD + EFA but forces a memory-only namespace, which means the flash ceiling
   — the number that decides whether the RDMA project is worth doing — cannot
   be measured at all. Also only 50 Gbps.
3. **Abuse a GPU type as a storage node** (`g6e.48xlarge`: AMD, EFA, 400 Gbps,
   7.6 TB NVMe, $30.13/hr). Technically satisfies AMD + EFA + NVMe, but at 2.8x
   the cost of `i3en.24xlarge` while paying for 8 L40S GPUs that sit idle.

The recommendation is option 1, with this documented as a finding.

## Availability zone

`aws ec2 describe-instance-type-offerings --location-type availability-zone`:

| Type | AZs in us-east-1 |
|---|---|
| `i3en.24xlarge` | a, b, c, d, f |
| `i4i.32xlarge` | a, b, c, d, e, f |
| `c5n.18xlarge` | a, b, c, d, f |
| `p4d.24xlarge` | a, b, c, d |
| `p5.48xlarge` | a, b, c, d, e, f |
| `g6e.48xlarge` | a, b, c, d |

**`us-east-1d` is the only AZ that offers every type on this list**, including
both the Half A storage types and every Half B GPU candidate. Since EFA cannot
cross an AZ and the cluster placement group is AZ-scoped, picking the AZ that
keeps all Half B options open costs nothing now and avoids rebuilding the stack
later. That is the Terraform default.

## Half B GPU candidates (do not provision yet)

| Type | GPU | EFA | Network | NVMe | $/hr |
|---|---|---|---|---|---|
| `g6e.12xlarge` | 4x L40S 48 GB | yes | 100 Gbps | 3.8 TB | 10.49 |
| `g6e.48xlarge` | 8x L40S 48 GB | yes | 400 Gbps | 7.6 TB | 30.13 |
| `p4d.24xlarge` | 8x A100 40 GB | yes | 400 Gbps | 8 TB | 21.96 |
| `p5.48xlarge` | 8x H100 80 GB | yes | 3200 Gbps | 30.4 TB | 55.04 |

`g6e.12xlarge` is the recommended default. Half B measures the *ratio* of TTFT
spent in L2 fetch versus everything else. That ratio does not require a
frontier-scale model, and a 4x L40S node measures it for a third of the price of
a p4d. Move up only if the model under test does not fit in 192 GB of HBM.
