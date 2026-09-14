# Half A — Aerospike storage ceiling + L2 adapter size sweep

**No GPU. ~$58/hr. Run this first.**

Half A answers two questions that do not need vLLM, a GPU, or any RDMA code:

1. What is the sustained read ceiling of a 5-node Aerospike cluster, in DRAM and
   on flash, relative to NIC line rate?
2. How does LMCache's L2 adapter load latency scale with object size, and how
   much of that scaling is the sequential multi-segment read path?

Question 1 gates the entire RDMA project. If flash cannot sustain line rate,
the RDMA bandwidth targets are unreachable regardless of how good the transport
is, because you cannot push bytes out of a NIC faster than you can get them off
the device.

## A1 — Raw cluster ceiling (`asbench`, no LMCache involved)

Deliberately measured without LMCache in the path, so that the L2 adapter
results in A2 can be compared against a known hardware ceiling rather than
against nothing.

### A1a — DRAM-resident

Working set sized to fit comfortably in the post-write cache and page cache
across the cluster. This is the optimistic bound.

```bash
asbench --hosts $LMCACHE_AEROSPIKE_HOSTS --namespace lmcache --set ceiling \
        --keys 20000 --object-spec B1048576 \
        --workload RU,100 --threads 128 --duration 300 \
        --latency --percentiles 50,90,99,99.9
```

20 000 x 1 MiB = ~20 GiB, trivially RAM-resident across 5 x 768 GiB.

### A1b — Flash-resident (the number that matters)

Working set far larger than aggregate RAM, so reads genuinely reach the NVMe
devices. 5 x 768 GiB = 3.75 TiB of RAM, so target >= 10 TiB.

```bash
# Load phase (hours -- this writes ~10 TiB).
asbench --hosts $LMCACHE_AEROSPIKE_HOSTS --namespace lmcache --set ceiling_flash \
        --keys 10000000 --object-spec B1048576 \
        --workload I --threads 256

# Read phase, uniform random over the full keyspace.
asbench --hosts $LMCACHE_AEROSPIKE_HOSTS --namespace lmcache --set ceiling_flash \
        --keys 10000000 --object-spec B1048576 \
        --workload RU,100 --threads 256 --duration 600 \
        --latency --percentiles 50,90,99,99.9
```

**Record for both:** sustained GB/s, IOPS, p50/p99/p99.9 latency, and
**achieved bandwidth as a percentage of 100 Gbps (12.5 GB/s) line rate**, per
node and aggregate.

Confirm the flash run is actually hitting flash — if the read hit rate against
the post-write cache is non-trivial, the working set is too small:

```bash
asinfo -h <server-ip> -v 'namespace/lmcache' | tr ';' '\n' | grep -E 'cache_read_pct|device_read'
```

A single client may not be able to drive 5 nodes to saturation. If A1 reports
well under line rate at 100% CPU on the client, raise `client_count` before
concluding anything about the server.

## A2 — LMCache L2 adapter size sweep

### Command

The CLI is `lmcache bench l2` (verified by reading
`lmcache/cli/commands/bench/l2_adapter_bench/__init__.py`, which registers
`name() -> "l2"`, and `command.py::add_l2_arguments`). Flags used:

| Flag | Meaning |
|---|---|
| `--l2-adapter JSON` | adapter spec; repeatable; falls back to `L2_ADAPTER_JSON` env var |
| `--data-size-kb N` | payload per key in **KB** (default 256) |
| `--num-keys N` | keys per submit (default 32) |
| `--in-flight N` | submits per round, issued sequentially from one producer then awaited (default 1) |
| `--rounds N` | measurement rounds (default 1) |
| `--warmup-rounds N` | warmup rounds (default 1) |
| `--only {lookup,store,load}` | restrict to one operation |
| `--skip-verify` / `--no-skip-verify` | round-trip verification, **off** by default |
| `--l1-align-bytes N` | buffer alignment; use 4096 for O_DIRECT backends |
| `--lookup-max-hit-rate F` | upper bound on lookup hit rate, [0,1] |
| `--flamegraph {on,off}` + `--flamegraph-mode` | self-profiling, renders SVG |

The adapter spec for Aerospike (from
`lmcache/v1/distributed/l2_adapters/aerospike_l2_adapter.py`, registered as type
name `aerospike`):

```json
{
  "type": "aerospike",
  "hosts": "10.240.1.10:3000,10.240.1.11:3000,10.240.1.12:3000,10.240.1.13:3000,10.240.1.14:3000",
  "namespace": "lmcache",
  "set_name": "kv_chunks",
  "num_workers": 8,
  "read_timeout_ms": 30000,
  "write_timeout_ms": 30000,
  "target_segment_bytes": 0,
  "max_record_bytes": 0
}
```

`target_segment_bytes: 0` and `max_record_bytes: 0` are important — they mean
"discover from the server", which is what puts the tuning from
`aerospike-tuning.md` on the measured path. Raise the timeouts well above the
1000/2000 ms defaults: an 80 MiB object at the default 1 MiB cap is 87
sequential round trips and will time out at 1 s.

### The sweep

128 KiB to 80 MiB. `--data-size-kb` takes KB, so 80 MiB = 81920.

```bash
for KB in 128 256 512 960 1024 2048 4096 8128 8192 16384 32768 65536 81920; do
  lmcache bench l2 \
    --l2-adapter "$SPEC" \
    --data-size-kb $KB \
    --num-keys 8 --in-flight 1 \
    --rounds 20 --warmup-rounds 3 \
    --only load
done
```

The 960 / 1024 and 8128 / 8192 pairs are not padding. They straddle the
`cap - 64 KiB` segmentation threshold, so they capture the discontinuity where
one round trip becomes three. Run `--only store` first to populate, then
`--only load` to measure.

**Record per size:** p50 and p99 load latency, derived MB/s, and the predicted
round-trip count from the table in `aerospike-tuning.md`.

### A3 — Quantifying the sequential-segment weakness

This is the expected finding and it is valuable independently of RDMA.

`do_single_get()` (`connector.cpp` ~229-238) reads the meta record, then loops
`for i in 0..nseg-1` reading each segment. Sequentially. On a single worker.
There is no fan-out across the connector's `num_workers` threads for the
segments of one object — those workers serve different *objects* concurrently,
not different segments of the same object. So a large chunk is a deep dependent
chain of round trips, and its latency is `(1 + nseg) x RTT` plus transfer, not
`max(RTT) + transfer`.

**The test.** Run the sweep twice, once with `max-record-size`/`write-block-size`
at 1M and once at 8M, and plot p50 load latency against object size for both.

Predictions, which the data should either confirm or refute:

- Below ~960 KiB the two curves are identical (1 round trip either way).
- Above the threshold, latency grows **linearly in `nseg`**, not in bytes — the
  slope is set by round-trip count, so the 1 MiB curve should be roughly 7x
  steeper at the top of the range.
- Increasing `--in-flight` and `--num-keys` should recover aggregate throughput
  (more objects in parallel) while leaving **per-object p99 unchanged**. That is
  the signature that confirms the bottleneck is a serial chain within one
  object rather than a shortage of concurrency overall.
- If per-object latency at 80 MiB is close to `87 x measured_RTT`, the chain is
  fully exposed and the fix is a parallel or batched segment read
  (`aerospike_batch_read` over all segment keys), independent of any RDMA work.

Take a flamegraph at the top of the range to show where the time goes:

```bash
lmcache bench l2 --l2-adapter "$SPEC" --data-size-kb 81920 \
  --only load --rounds 10 \
  --flamegraph on --flamegraph-mode on-cpu,off-cpu
```

`off-cpu` is the informative one: a serial round-trip chain shows up as off-CPU
wait, not CPU time.

## Deliverables from Half A

1. Sustained read GB/s and IOPS, DRAM vs flash, as a percentage of 12.5 GB/s
   line rate per node.
2. A go/no-go on the RDMA premise: does flash sustain line rate?
3. p50/p99 load latency vs object size, 128 KiB to 80 MiB, at both record caps.
4. Measured round trips per chunk read vs the predicted `1 + nseg`.
5. A recommendation on parallelising `do_single_get()`, with the measured
   latency it would remove.
