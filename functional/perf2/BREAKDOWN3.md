# kv-sink breakdown, day 2 round 2 (Sriram, server `0c9703931`, 2026-10-06)

Sriram's follow-up to `BREAKDOWN2.md` (Slack, 2026-10-06 10:16 PT). Server asd
`0c9703931` (`sriram/kv-sink-batch-prio`: least-loaded queue pair, `busy qps X of Y` in
the stats line, up to 32 QPs, new defaults 32 placers and cap 128), binary
`/root/lmc-work/asd-0c9703931/asd`. `kvlayers` relinked against client `e8158149` (up to 32
RC QPs). LMCache `prototype-stage-1b` unchanged, on client `5a24afdb`. Memory namespace,
server defaults, `KV_SINK_STATS=1`.

- Script: `scripts/breakdown3.sh`; tables: `scripts/breakdown3_report.py`. Outputs:
  `breakdown3/` (`report.md`, `stats_lines.txt`, `kvlayers_runs.txt`,
  `raw_perftest.txt`, `run5_*`).
- Run 4 is at 16 QPs only. LMCache validates `rdma.queue_pairs` to 1-16
  (`_MAX_QUEUE_PAIRS` in `lmcache/v1/distributed/l2_adapters/rdma_registration.py`), and
  raising it is a product-code change. So LMCache's client library was not rebuilt.

## Result

Every QP has writes outstanding (16.0 of 16, 23.9 of 24, 31.7 of 32), so queue-pair
imbalance is not why the sink is below raw. The sink reaches 64-70% of `ib_write_bw` at
16-32 QPs (Graviton: 88-93%). Raw Soft-RoCE doesn't scale past 16 QPs on this box.

At cap 128 the sink's rxe workers cost about what perftest's do per GiB (1,446 vs 1,466
ms/GiB). Only ~10 run at once (perftest: 15.7), with 9.8 of 20 CPUs busy. The rate
follows the busy rxe cores: 10.0 / 1.446 = 6.9 GiB/s, 15.7 / 1.466 = 10.7 GiB/s.
Something limits concurrent rxe work for the sink, and it isn't QP choice or CPU count.

The next step, not run, is to split the rxe samples per task (`perf report --sort tid`)
for the sink and for perftest. That would show whether the requester (asd) or the
responder (client) tasks are the ones that don't spread.

## Run 3: raw `ib_write_bw -s 524288 -t 2` (E1 flags)

| QPs | GiB/s |
|---|---|
| 16 | 10.83 |
| 24 | 10.52 |
| 32 | 10.06 |
| 64 | 9.47 |

## Runs 1, 2, 5: `kvlayers --duration 8`, server defaults

Stats are the median of each run's 3 highest-rate lines (us per write). Read is
1-2 us, and post and reply are 0-1 us everywhere.

| run | QPs | stream GiB/s | of raw | busy qps | in flight | placer queue | placer-wait | copy | wire | starved |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 16 | 7.21 | 67% | 16.0 | 128 | 15.0 | 1191 | 61 | 7278 | 0% |
| 1 | 16 | 7.14 | 66% | 15.9 | 128 | 16.6 | 1268 | 61 | 7283 | 0% |
| 2 | 24 | 7.31 | 69% | 23.9 | 128 | 14.6 | 1242 | 64 | 7115 | 0% |
| 2 | 32 | 7.00 | 70% | 31.7 | 128 | 10.1 | 1020 | 64 | 7755 | 0% |
| 5 | 16 | 6.93 | 64% | 16.0 | 128 | 16.5 | 1236 | 62 | 7668 | 0% |

Run 5 is run 1 again with the profile running. Day 1 on `314564cfb` (`BREAKDOWN.md`):
5.48 GiB/s (cap 32), 6.43 (cap 128), and 7.20 (cap 128 + 16 placers). Today's defaults
match day 1's tuned config, so least-loaded choice adds nothing here. This matches the
Graviton result, where round robin already kept 15.8 of 16 QPs busy.

## Run 4: lw 8k c=1, 16 QPs

7.52 GiB/s from the per-layer timeline (E3 print patch in `pump.py`, applied for the
session and reverted; the tree was checked clean). TTFT p50 ~0.199 s.

## Run 5: CPU during `kvlayers --qps 16`

`perf record -a -g -F 999` for 5 s, plus `mpstat -P ALL 1`. GiB = the 5 one-second stats
lines after the profile's start mark (34.6 GiB).

| | sink, cap 128 (today) | sink, cap 32 (Q1 A) | perftest q16 t2 (Q1 A) |
|---|---|---|---|
| rxe worker samples | 50,039 | 32,101 | 78,640 |
| cores busy | 10.0 | 6.4 | 15.7 |
| ms/GiB | 1,446 | 1,068 | 1,466 |

asd: 6,324 samples (125 ms/GiB). mpstat: 9.8 of 20 CPUs busy.
