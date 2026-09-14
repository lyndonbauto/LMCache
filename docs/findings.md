# M0 baseline: Aerospike as an LMCache L2 backend

Measured 2026-09-14 on 5x `i3en.24xlarge` (Aerospike Enterprise 8.1.2.5, RF=1,
8 instance-store NVMe each) plus 1x `c5n.18xlarge` load generator, all in one
`us-east-1d` cluster placement group with EFA enabled.

All results below are **NVIDIA-free**: Half A does not involve a GPU. Half B
(TTFT attribution) is **not** in this document -- see "Blocked" at the end.

## Headline

1. **The record-size cliff is real and reproducible**, in both arms, at
   whatever the cap happens to be. Crossing it costs ~60-100% read latency for
   a fraction of a percent more data.
2. **Raising `max-record-size` is not a clean win.** It moves the cliff, and
   for objects larger than the new cap it makes things *worse*. The two arms
   cross over at ~8 MiB.
3. **The storage tier is not the bottleneck; the client NIC is.** Flash-resident
   and DRAM-resident reads are indistinguishable, both pinned at 100 GbE line
   rate. This is the central argument for the RDMA work.

## 1. The two-arm sweep

Arm A = stock defaults (`flush-size 1M`, no `max-record-size`) -> live cap
1 MiB. Arm B = `max-record-size 8M` + `flush-size 8M` -> live cap 8 MiB. Each
arm was gated on an `asinfo` read of the **live** cap before any data was
collected, and the devices were wiped between arms.

Per-object load latency, 4 keys x 10 rounds, 8 connector workers:

| Object | Arm A rt | A p50 | A p99 | Arm B rt | B p50 | B p99 |
|-------:|---------:|------:|------:|---------:|------:|------:|
| 128 KB   |  1 |   1.06 |   7.59 |  1 |   1.31 |  10.03 |
| 256 KB   |  1 |   1.18 |   4.96 |  1 |   1.12 |  10.54 |
| 512 KB   |  1 |   1.74 |   3.58 |  1 |   2.02 |   7.52 |
| 960 KB   |  1 |   2.60 |   4.24 |  1 |   2.83 |  13.58 |
| 1024 KB  |  3 | **5.35** |  13.54 |  1 |   3.74 |  11.34 |
| 2048 KB  |  4 |   6.28 |  16.24 |  1 |   5.24 |  14.14 |
| 4096 KB  |  6 |  11.17 |  14.35 |  1 |   9.91 |  17.54 |
| 8128 KB  | 10 |  21.17 |  29.75 |  1 |  20.90 |  29.06 |
| 8192 KB  | 10 |  18.56 |  23.47 |  3 | **33.44** |  45.06 |
| 16384 KB | 19 |  35.34 |  42.89 |  4 |  51.34 |  58.80 |
| 32768 KB | 36 |  72.69 |  83.90 |  6 |  84.36 |  92.33 |
| 65536 KB | 70 | 140.78 | 146.95 | 10 | 153.68 | 172.07 |
| 81920 KB | 87 | 173.07 | 190.09 | 12 | 189.94 | 218.22 |

`rt` = round trips per read = 1 metadata GET + N segment GETs (1 when the
object fits in a single record).

### The cliff

The discontinuity sits exactly at the cap, in both arms:

- **Arm A**, 960 KB -> 1024 KB: **2.60 ms -> 5.35 ms**. +6.7% data, **+106% latency**.
- **Arm B**, 8128 KB -> 8192 KB: **20.90 ms -> 33.44 ms**. +0.8% data, **+60% latency**.

Arm B is the cleaner demonstration: 64 KB more payload, 12.5 ms more latency.
The cause is structural, not bandwidth -- one round trip becomes three, and the
connector issues the metadata GET, waits, then fetches segments.

### The crossover, which was not expected

Below ~8 MiB, Arm B wins (fewer round trips). **Above ~8 MiB, Arm A wins**, and
the margin grows with size: at 80 MiB, the "untuned" 1 MiB cap is
**173 ms vs 190 ms**, ~9% *faster* despite issuing 87 round trips instead of 12.

Aggregate read bandwidth tells the same story: Arm A reaches 1880 MB/s at
80 MiB, Arm B only 1659 MB/s.

The reason is parallelism granularity. An 80 MiB object under a 1 MiB cap is 86
segments that fan out across 5 nodes and 40 devices; under an 8 MiB cap it is
11 segments hitting far fewer devices, and each 8 MiB record read pulls a full
8 MiB flush block. Larger records reduce round-trip *count* but coarsen the
unit of concurrency.

**Implication:** "just raise `max-record-size`" is the right advice only if
objects are smaller than the new cap. It is actively harmful for large
segments. The real fix is to stop paying the sequential metadata->segment
round trip at all, which is what the RDMA server-push design does.

## 2. Storage ceiling

`asbench`, 960 KiB objects, 60 read threads, 256 outstanding:

| Residency | Reads/s | Throughput | Notes |
|---|---:|---:|---|
| Flash (`storage-engine device`, 8x NVMe) | 12,450 | **12.2 GB/s** | 56 GB dataset, direct I/O |
| DRAM (`storage-engine memory`) | 12,460 | **12.2 GB/s** | 18 GB dataset |

**These are the same number.** Both are pinned at the `c5n.18xlarge`'s 100 Gbps
NIC (12.5 GB/s theoretical, 12.2 GB/s observed = 98% of line rate).

Removing the flash tier entirely changes read throughput by 0.08%. The NVMe
tier was never the constraint -- the 8-device-per-node configuration delivers
~16 GB/s of raw read capacity per node, comfortably above what a single 100 GbE
client can consume. Keeping 8 devices was the right call; at 4 devices
(~8 GB/s) we would have been measuring the device count instead of the fabric.

The practical consequence: **on this hardware, an L2 KV-cache read is a network
problem, not a storage problem.** Any optimisation that does not reduce bytes
on the wire or round trips on the wire is optimising the wrong layer.

## 3. Aerospike configuration findings relevant to LMCache

### `write-block-size` no longer exists

Aerospike 8.x rejects `write-block-size` at startup:

```
CRITICAL: 'write-block-size' is obsolete - please use 'flush-size' and
'post-write-cache' and perhaps 'max-record-size'
```

`asinfo -v namespace/<ns>` on 8.1.2.5 does **not** emit a `write-block-size`
field at all. `discover_record_cap()` reads `max-record-size=` first and falls
back to `write-block-size=`; **on Aerospike 7.2+ that fallback is dead code**.
If `max-record-size` is unset the connector silently uses its hardcoded 1 MiB
default. It happens to be correct here -- 8.1 reports `max-record-size=1048576`
when it is left at the default -- but the fallback path should be treated as
unreachable on modern servers.

### The 2 TiB per-device trim is not a Community limitation

I previously attributed this to Community Edition. That was wrong, and worth
correcting since it drove the switch to Enterprise:

```
WARNING (drv_ssd): usable device size must be <= 2199023255552,
trimming original size 7499994365952
```

This still appears on **Enterprise**. It is an architectural limit of the SSD
driver's wblock addressing (262,144 wblocks x 8 MiB = exactly 2 TiB), present in
both editions. Larger devices must be partitioned into <=2 TiB slices offered as
separate `device` entries.

Switching to Enterprise was still necessary -- the actual startup blocker was
`CRITICAL (storage): Community Edition limit exceeded`, a separate cap on total
namespace size that 8 x 2 TiB per node exceeds. With RF=1 the cluster now has
16 TiB usable per node, 80 TiB total, which is ample.

### `lmcache bench l2` requires `openai`

`engine_bench` imports `openai` at CLI **parser-registration** time, so a
missing `openai` package breaks the entirely unrelated `lmcache bench l2`
subcommand with `ModuleNotFoundError`. Worth making that import lazy.

## 4. How instance-store devices were positively identified

Numbering is genuinely unstable. Across the five nodes the EBS root volume was
observed as `nvme0n1`, `nvme3n1` and `nvme4n1` -- and it moved between the
first and second build of the *same* cluster. Any index-based rule would
eventually have handed the root volume to Aerospike's raw-device engine.

Selection uses `lsblk -J -o NAME,MODEL,SIZE,TYPE,MOUNTPOINT` and requires **all**
of:

- `MODEL` is exactly `Amazon EC2 NVMe Instance Storage`
  (EBS reports `Amazon Elastic Block Store`)
- `TYPE` is `disk`
- neither the disk nor any child partition has a mountpoint
- it is not the disk backing `/`, resolved via `findmnt -no SOURCE /`

The count is then asserted to be exactly 8, and a mismatch **aborts** rather
than proceeding. A wrong count means the identification logic is wrong, and the
cost of guessing is a destroyed node. Every node logged its decision, e.g.:

```
root filesystem source : /dev/nvme4n1p1 -> disk nvme4n1
rejected               : /dev/nvme4n1 (model='Amazon Elastic Block Store')
selected               : /dev/nvme0n1 ... /dev/nvme8n1   (8 devices)
```

Wipe is `blkdiscard -f` (TRIM, near-instant) followed by
`dd if=/dev/zero bs=1M count=8 oflag=direct` to clear the Aerospike header,
then **verified** by reading the first 8 MiB back and asserting it is all
zeros. Verification matters: the first failed start was caused by assuming a
discard had taken effect when it had not.

## 5. Blocked

**Half B (TTFT attribution) did not run: no GPU capacity.** `g6e.12xlarge` is
*offered* in `us-east-1d` but returned `InsufficientInstanceCapacity`
continuously for 24 minutes, including after the node was removed from the
cluster placement group. No instance was ever created. Options, in order of
how much they distort the measurement:

1. Wait / retry `g6e.12xlarge` in `us-east-1d`.
2. Place the GPU in another AZ. Costs cross-AZ latency (~0.5-1 ms), which
   inflates the measured L2-fetch share of TTFT -- conservative in the right
   direction, but no longer EFA-eligible.
3. Use a different GPU family (`g5.12xlarge`, `g4dn.12xlarge`). Changes the
   decode-side numbers and so is not comparable to the approved plan.

AMD GPUs are not offered on EC2 in `us-east-1`, so Half B will be NVIDIA
regardless, and should be labelled as such.

## 6. Incident: an unrelated apply destroyed the cluster

Adding the GPU node with `terraform apply` **destroyed and recreated all five
storage nodes and the client**, losing the ephemeral NVMe dataset and about an
hour of setup.

Cause: the AMI comes from `data.aws_ssm_parameter` pointing at
`/aws/service/ami-amazon-linux-latest/al2023-...`, which resolves the *latest*
image at every plan. AWS republished that AMI during the session, the ID
changed, and a changed `ami` **forces replacement**.

Fixed by `lifecycle { ignore_changes = [ami, user_data] }` on all three
instance resources, so existing nodes keep the image they booted with while new
nodes still get the current one. This is worth flagging generally: any
Terraform stack that resolves a "latest" AMI has a live grenade in it, and the
blast radius is the entire fleet.
