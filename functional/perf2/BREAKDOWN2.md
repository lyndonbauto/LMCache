# kv-sink breakdown, day 2 (Sriram's Q1-Q3, 2026-10-06)

Sriram's follow-up to `BREAKDOWN.md` (Slack, 2026-10-05 19:29 PT). New droplet
134.199.201.175, made from the snapshot of the day-1 droplet (same stack). Server asd
`314564cfb` (`/root/lmc-work/asd-314564cfb/asd`), client `5a24afdb`, LMCache
`prototype-stage-1b` unchanged, `KV_SINK_STATS=1` on every server start.

- Scripts: `scripts/breakdown2.sh` (Q1, Q2), `scripts/breakdown.sh` steps
  `devp16 devp32 devp64` (Q3); tables: `scripts/breakdown2_report.py`,
  `scripts/breakdown_report.py`. Outputs: `breakdown2/`.
- Profiles are `perf record -a -g -F 999` for 5 s, so one sample is about 1 ms of CPU.
  "ms/GiB" is CPU ms per GiB moved in that window.
- Soft-RoCE workers are counted as every `kworker` thread: perf truncates their names
  (`kworker/u46:2-r`), and nothing else ran kernel workers in these windows.

## Q1: why ~2.6 ms per write for the sink but ~1.4 ms for perftest?

Memory namespace, cap 32, 16 QPs, 512 KiB writes.

### A. CPU per GiB, `ib_write_bw -q 16 -t 2` vs `kvlayers --qps 16`

| | ib_write_bw | kvlayers (sink) |
|---|---|---|
| GiB moved in 5 s | 53.7 | 30.1 |
| rxe workers: samples (cores busy) | 78,640 (15.7) | 32,101 (6.4) |
| rxe workers: ms/GiB | 1,466 | 1,068 |
| other: ms/GiB | ib_write_bw 94 | asd 149 |

The sink costs Soft-RoCE *less* CPU per GiB than perftest, not more. It is slower because
only about 6 cores' worth of rxe work runs at once, against about 16 for perftest.

Top rxe-worker functions (full 30 in `breakdown2/report_q1_q2.md`):

| function | perftest ms/GiB | sink ms/GiB | sink / perftest | what it is |
|---|---|---|---|---|
| `__raw_spin_lock_irqsave` | 66 | 116 | 1.7x | mostly `skb_dequeue` in `rxe_receiver` |
| `memset_orig` | 112 | 105 | 0.9x | skb allocation (`kmalloc_reserve` in `rxe_init_packet`) |
| `__memcpy` | 44 | 88 | 2.0x | `copy_data` (payload) and `rxe_receiver` |
| `crypto_shash_update` | 331 | 86 | 0.3x | ICRC, mostly the per-packet header part |
| `crc32_pclmul_le_16` | 74 | 82 | 1.1x | ICRC payload |
| `rdma_get_gid_attr` | 106 | 60 | 0.6x | per-packet GID lookup |
| `clear_page_erms` | 17 | 37 | 2.2x | page zeroing under `__kmalloc_node_track_caller` |
| `ib_device_put` | 110 | 30 | 0.3x | per-packet device refcount |

Locks (`breakdown2/q1_A_*_perf_callers.txt`): the hot lock is `skb_dequeue`, the
per-QP receive packet queue in `rxe_receiver`. It is 5.6% of all samples for the sink
against 1.5% for perftest. MR or page lookups (`rxe_pool_get_index`, 21 vs 30 ms/GiB) are
not hot.

perftest spends about 4x more per GiB on per-packet header work (header ICRC, GID
lookups, device refcounts). Both use MTU 4096 (perftest's `Mtu: 4096[B]`; the server log's
`active mtu 4096`), so this is not an MTU difference. It is not explained. It does not
change the conclusion: perftest pays more CPU per byte and still moves twice as much.

### B. Hot (32 MiB) vs cold (1 GiB) working set

`kvlayers --qps 16 --duration 8`, stats lines with at least 28 writes in flight, two runs
each:

| set | GiB/s | wire us/write (median) | copy us |
|---|---|---|---|
| hot, 32 MiB | 5.88 / 6.14 | 2,340 / 2,243 | 47 / 45 |
| cold, 1 GiB | 5.30 / 5.35 | 2,679 / 2,637 | 42 / 40 |

Hot memory saves about 15% of the wire time (2.66 -> 2.29 ms). That is far short of
perftest's 1.4 ms, so cold memory is a minor part of the gap.

### C. Contention from asd's staging copy

| `ib_write_bw -q 16 -t 2` | GiB/s |
|---|---|
| alone (before) | 11.18 |
| alongside `kvlayers --qps 1` (0.93 GiB/s) | 10.37 |
| alone (after) | 11.11 |

perftest loses 7% with the sink running alongside, so there is a little contention, but
not enough to explain a 2x gap.

### Q1 reading

Not cold memory (B: 15%), not contention (C: 7%), not more CPU per byte (A: the sink
uses less). The sink's Soft-RoCE work is less parallel: about 6 cores of rxe workers
against 16, with more time in the receive-queue lock (`skb_dequeue`). rxe runs each QP's
requester and responder as separate tasks. A likely cause, not yet tested: the sink's 32
in-flight writes are not spread evenly over its 16 QPs (perftest keeps exactly 2 per QP),
so fewer QPs, and so fewer rxe workers, are active at once. A per-QP in-flight count in
the server's stats would show it.

## Q2: is aon's win over TCP a transport cost?

Device namespace, server defaults. 64 prompts stored, then one aon point at 8k, c=16,
n=64 (four waves of 16, all from Aerospike: 524,224 hit tokens, 0 errors). The profile
starts when the point starts. GiB come from the namespace's read counters over 6.3 s,
which includes perf's start-up, while samples cover 5 s. So the ms/GiB below is a lower
bound, up to about 25% low.

### D. CPU per GiB during aon

| thread kind | dso | ms/GiB |
|---|---|---|
| asd | kernel (TCP send) | 585 |
| lmcache | kernel (TCP receive) | 378 |
| lmcache | libc (copies) | 226 |
| python (vLLM) | kernel | 110 |
| asd | libc | 92 |
| python (vLLM) | HSA runtime | 85 |
| **total** | | **>= 1,827** (about 16 cores busy) |
| kvlayers over the sink (Q1 A), total | | 1,259 (about 7 cores) |

TCP is not cheaper per GiB than Soft-RoCE on this box. aon costs at least 1.8 s of CPU per
GiB fetched against 1.26 s for the sink. aon wins because the TCP path spreads over about
16 cores: asd's service threads and LMCache's receivers. The sink's Soft-RoCE path uses
about 7 (Q1).

### E. Server read histograms during aon

`enable-benchmarks-read` and `enable-benchmarks-batch-sub` on namespace `lmcache` (the
namespace is `lmcache`, not `kvcache`); both turned off afterwards.

| histogram | count | <= 1 ms | 1-2 ms | 2-4 ms |
|---|---|---|---|---|
| `{lmcache}-read` | 133,254 | 133,208 | 43 | 3 |
| `{lmcache}-read-local` | 133,252 | 133,227 | 23 | 2 |
| `{lmcache}-read-response` | 133,252 | 133,251 | 1 | 0 |
| `{lmcache}-batch-sub-read` | 4,096 | 4,088 | 5 | 2 (+1 at 4-8 ms) |

aon reads 512 KiB records one at a time (single-record reads, not batches). 99.97%
finish within 1 ms on the server, so the server's device reads are not aon's limit.

### Q2 reading

On this box the aon win is a property of where the work runs, not a cheaper transport.
TCP costs more CPU per GiB, but it runs on many threads at once. Soft-RoCE is serialized
per QP in kernel workers. On a hardware NIC the RDMA side drops to near zero CPU and is
not serialized in software, while TCP keeps its ~1.8 s per GiB. That fits Sriram's EFA
result (3.9 vs 0.55 GiB/s).

## Q3: can the device namespace reach the memory namespace's rate?

Device namespace, `KV_SINK_MAX_IN_FLIGHT=128`, 16 QPs. B = `kvlayers --duration 6`; C =
lw 8k c=1 from the per-layer timeline. Steady-state stats lines from B:

| placers | B GiB/s | C (lw) GiB/s | placer-wait us | read us | placer queue | in flight | wire us |
|---|---|---|---|---|---|---|---|
| 8 (day 1, `dev128`) | 5.99 | 5.67 | 9,280 | 370 | 115.2 | 128 | 614 |
| 16 | 7.09 | 7.86 | 6,347 | 653 | 93.6 | 128 | 1,746 |
| 32 | 7.31 | 8.42 | 2,745 | 1,020 | 41.8 | 128 | 4,665 |
| 64 | 7.34 | 7.99 | 1,149 | 1,475 | 16.1 | 128 | 5,747 |
| memory namespace, 16 (day 1) | 7.20 | 7.86 | 993 | 1 | 13.7 | 128 | 7,494 |

Yes. With 16 or more placement threads the device namespace matches the memory namespace
(7.1-7.3 GiB/s for `kvlayers`, 7.9-8.4 GiB/s for lw). Past 16 threads the device read
time grows (650 -> 1,020 -> 1,475 us as more reads hit the disk at once) and the time
moves back to the wire, the same Soft-RoCE limit as in memory. 32 placers is the best lw
point (8.42 GiB/s); 64 gives nothing more. Suggested defaults: `KV_SINK_PLACE_THREADS`
16-32 and cap 128. Async reads are not needed to reach the transport's limit on this box.

## Restore

After each step: asd stopped, data file deleted, read benchmarks off, env knobs only in
that start's environment, and LMCache `lmcache/` and `csrc/` clean. The day-1 `9c16972132`
binary in the server tree is untouched (md5 `0b953fb4...`).
