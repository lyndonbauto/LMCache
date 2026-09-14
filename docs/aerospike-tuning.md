# Aerospike namespace tuning for LMCache (AIE-86, Task 3)

The rendered config is `terraform/templates/aerospike.conf.tftpl`. This document
is the reasoning.

> **SUPERSEDED IN PART — read `findings.md` §1 before applying "Chosen values" below.**
>
> This document was written before the sweep ran and recommends `max-record-size`
> / `write-block-size` at 8M/8M on the theory that fewer round trips is strictly
> better. **The measurement disproved that.** The two arms cross over at ~8 MiB:
> above the cap, the 1 MiB default is *faster* (80 MiB: 173 ms / 1880 MB/s at
> 1 MiB vs 190 ms / 1659 MB/s at 8 MiB), because a larger record coarsens the
> unit of concurrency and reduces device fanout.
>
> The correct rule is **match the cap to the chunk-size distribution you actually
> serve** — raising it helps objects below the new cap and hurts objects above it.
> Do not set it to the maximum by reflex. Everything else in this document
> (the sharding logic, the both-values trap, RF, mesh, storage engine) still holds.

## How LMCache actually decides how to shard

Read these two functions before changing any number here:

- `discover_record_cap()` — `csrc/storage_backends/aerospike/connector.cpp:401-435`
- `plan()` — `csrc/storage_backends/aerospike/connector.cpp:387-399`
- construction — same file, lines 105-115
- `do_single_get()` — same file, lines ~229-238

The chain is:

1. On connect, if the adapter's `max_record_bytes` config is 0 (the default),
   the connector issues the Aerospike info command `namespace/<ns>` and scans
   the reply for `max-record-size=` **first**, then `write-block-size=`. It
   takes the first value that parses to something greater than zero. If neither
   is found it falls back to a hardcoded `kDefaultRecordCapBytes` = **1 MiB**.

2. It subtracts a hardcoded `kSafetyMarginBytes` = **64 KiB** to leave room for
   key, bin and record overhead:
   `max_record_bytes_ = discovered_cap - 64 KiB`.

3. `target_segment_bytes_` defaults to `max_record_bytes_`, and
   `single_record_threshold_bytes_` is set equal to it.

4. `plan(payload)` returns one record if `payload <= threshold`, otherwise
   `nseg = ceil(payload / target_segment_bytes_)` segments.

5. `do_single_get()` reads the meta record, and if `nseg > 1` loops
   `for i in 0..nseg-1` reading each segment **sequentially on one worker**.
   Total round trips = `1 + nseg`. These are dependent — each is issued only
   after the previous one returns.

So the server-side record cap does not just affect storage layout. It directly
sets the length of a serial round-trip chain on every large read.

## Chosen values

```
namespace lmcache {
    max-record-size 8M           # namespace level
    storage-engine device {
        write-block-size 8M
    }
}
```

8 MiB is the maximum Aerospike accepts for both. Effective segment size becomes
`8 MiB - 64 KiB = 8,323,072 bytes`.

### What that buys, exactly

Round trips to read one chunk, computed from the code above:

| Chunk size | 1 MiB cap (default) | 8 MiB cap (chosen) |
|---|---|---|
| 128 KiB | 1 seg, **1 RT** | 1 seg, **1 RT** |
| 256 KiB | 1 seg, 1 RT | 1 seg, 1 RT |
| 512 KiB | 1 seg, 1 RT | 1 seg, 1 RT |
| 1 MiB | 2 seg, **3 RT** | 1 seg, **1 RT** |
| 2 MiB | 3 seg, 4 RT | 1 seg, 1 RT |
| 4 MiB | 5 seg, 6 RT | 1 seg, 1 RT |
| 8 MiB | 9 seg, 10 RT | 2 seg, 3 RT |
| 16 MiB | 18 seg, 19 RT | 3 seg, 4 RT |
| 32 MiB | 35 seg, 36 RT | 5 seg, 6 RT |
| 64 MiB | 69 seg, 70 RT | 9 seg, 10 RT |
| **80 MiB** | **86 seg, 87 RT** | **11 seg, 12 RT** |

At the top of the sweep range the serial chain shortens by **7.25x**. If a
single segment read costs ~200 µs against flash, that is ~17 ms versus ~2.4 ms
of pure latency for the same 80 MiB — before any bandwidth consideration at all.

Note the cliff at 1 MiB in the default configuration: a chunk one byte over
960 KiB goes from one round trip to three. The L2 sweep should include points
just either side of the threshold to capture it.

### The trap: both values must be set

`discover_record_cap()` checks `max-record-size=` **before**
`write-block-size=`. If someone raises `write-block-size` to 8M and leaves
`max-record-size` at its 1 MiB default, LMCache reads 1 MiB, caps there, and
shards into 960 KiB segments anyway. The server is tuned; the client never finds
out. Nothing errors, the benchmark just quietly produces the untuned numbers.

Verify on the running cluster rather than trusting the config file:

```bash
asinfo -h <server-ip> -v 'namespace/lmcache' | tr ';' '\n' \
  | grep -E 'max-record-size|write-block-size'
```

Both must read `8388608`. If `max-record-size=0`, the connector's `cap > 0`
guard rejects it and correctly falls through to `write-block-size` — that case
is safe, but it is safer still to set an explicit value.

### Cost of an 8 MiB write block

Larger write blocks mean more defragmentation write amplification (a block is
rewritten wholesale when it is defragged) and more DRAM held in per-device write
buffers. For a workload whose records are multi-megabyte KV-cache segments this
is the right trade: the alternative is paying the cost on the read path, as
round trips, on every single retrieve. Small-record workloads should not copy
this setting.

### Aerospike version caveat

The exact defaults and the context `max-record-size` lives in have moved between
Aerospike 6.x and 7.x/8.x. Do not assume — run the `asinfo` check above after
the cluster comes up and confirm the effective values before recording any
benchmark numbers. The runbook makes this a gating step.

## Storage engine: device on local NVMe

`storage-engine device` with the raw `i3en.24xlarge` instance-store volumes, no
filesystem.

**Tradeoff.** A memory namespace would produce larger, prettier numbers and
would be simpler to set up. It would also be the wrong measurement. The question
Half A exists to answer is whether the flash tier can sustain NIC line rate,
because if it cannot, the RDMA bandwidth targets for the whole project are moot
— you cannot RDMA data out faster than you can read it off the device. A
memory-only namespace answers a question nobody asked.

Both cases get measured, but the flash-resident number is the finding.

Raw devices rather than files on a filesystem: Aerospike's flash engine does its
own block allocation, and a filesystem underneath adds a second allocator plus a
page cache layer that the benchmark is specifically trying to see past.

The device list is discovered at boot by matching the NVMe model string
`Amazon EC2 NVMe Instance Storage`, not by device index. Nitro does not
guarantee stable `/dev/nvmeXn1` ordering, and a hardcoded list will eventually
point Aerospike's raw-device engine at the EBS root volume.

### Keeping the flash-resident test honest

Two settings exist purely to stop DRAM from silently serving the "flash" reads:

- `post-write-cache 64` — small. This is a DRAM read cache in front of the
  device; a large one lets recently written records be served from memory.
- `read-page-cache false` — stops the OS page cache doing the same thing.

The DRAM-resident case is measured separately by using a working set that fits
comfortably in RAM, not by turning these caches back on.

## Replication factor: 1

Three reasons, in order of importance:

1. **RF=2 would corrupt the measurement.** The headline Half A output is
   sustained *read* bandwidth against NIC line rate. RF=2 puts replica write
   traffic on the same fabric, so the result would be a replication-limited
   number reported as a storage ceiling.
2. **Durability buys nothing here.** A KV cache entry is regenerable. A lost
   entry costs a prefill recompute, not data loss. This is true in production
   too, which is why RF=1 is a defensible production choice for this workload
   and not just a benchmark shortcut.
3. **Capacity.** RF=2 halves usable space, which matters when the flash test
   wants a working set far larger than 5 x 768 GiB of RAM.

Cost: losing a node loses 1/5 of the cache. On a benchmark cluster that is a
re-run.

**Follow-up worth doing:** one confirmation run at RF=2 to get a
production-representative write-path number. Keep it separate from the ceiling
measurement.

## Cluster formation: mesh, not multicast

`mode mesh` with all five peer addresses listed explicitly.

Multicast is not delivered inside a VPC. A multicast heartbeat config does not
fail loudly — it produces five single-node clusters that each report themselves
as healthy. The runbook's `asadm -e info` check exists specifically to catch
this, by asserting the cluster size is 5 on every node.

Peer IPs are pinned in Terraform via `cidrhost()` (10.240.1.10 – .14) rather
than left to DHCP. That removes a bootstrap ordering dependency: every node gets
a byte-identical config listing every peer including itself (Aerospike ignores
the self entry), so the cluster forms regardless of which node boots first.
