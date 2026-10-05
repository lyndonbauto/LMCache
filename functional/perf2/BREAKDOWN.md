# kv-sink path breakdown (Sriram's request, 2026-10-05)

Why does layerwise (lw) reach only part of raw `ib_write_bw` on the MI300X droplet
(Soft-RoCE `rxe0` on `lo`)? No LMCache code was changed.

- Server: asd `sriram/kv-sink-batch-prio` `314564cfb` (built in place, copied to
  `/root/lmc-work/asd-314564cfb/asd`, md5 `c934cad4f806ed88812b877162af6a67`; the
  `9c16972132` tree and binary were restored, md5 `0b953fb4...`). Client `5a24afdb`.
  LMCache `prototype-stage-1b` `b947831d`.
- Every run has `KV_SINK_STATS=1`. Script: `scripts/breakdown.sh`; table:
  `scripts/breakdown_report.py breakdown mem32 ...` (`breakdown/report.md`).
- A: `ib_write_bw -s 524288`. B: `kvlayers --layers 32 --chunks 64 --blocksz 524288
  --qps N --duration 6` after one `--fill --reps 1`. C: LMCache lw 8k c=1; the fetch rate
  is 31 x 32 MiB over the layer 0 -> 31 time of the median retrieve (throwaway `pump.py`
  print patch, reverted after each C). E: `mpstat`, `top -H`, `perf record -a -g` during B
  and C at 8 QPs.
- Stage columns are the median of the 3 highest-rate stats lines in each run.

## Raw ceiling (A)

| QPs x depth | Gb/s | GiB/s |
|---|---|---|
| q1 t8 | 19.28 | 2.24 |
| q8 t4 | 76.20 | 8.87 |
| q16 t2 | 96.24 | 11.2 |
| q16 t8 | 95.52 | 11.1 |

## Runs

us/write per stage; "in flight" is writes; raw is `ib_write_bw` at the same QP count
(q1 t8, q8 t4, q16 t2).

| Run | Fetch GiB/s | % raw | queued | placer-wait | read | copy | wire | in flight | placer queue | starved |
|---|---|---|---|---|---|---|---|---|---|---|
| mem, cap 32, B 1 QP | 1.64 | 73% | 33509 | 40 | 1 | 34 | 2270 | 8.0 | 0.2 | 0% |
| mem, cap 32, C 1 QP | 1.45 | 65% | 248364 | 38 | 2 | 53 | 2607 | 5.0 | 0.1 | 38% |
| mem, cap 32, B 8 QPs | 5.55 | 63% | 16603 | 233 | 1 | 37 | 2524 | 32.0 | 2.2 | 0% |
| mem, cap 32, C 8 QPs | 5.07 | 57% | 68229 | 315 | 1 | 60 | 2722 | 5.4 | 0.5 | 83% |
| mem, cap 32, B 16 QPs | 5.48 | 49% | 15727 | 200 | 1 | 38 | 2584 | 32.0 | 1.9 | 0% |
| mem, cap 32, C 16 QPs | 4.95 | 44% | 70892 | 279 | 1 | 60 | 2943 | 5.5 | 0.4 | 83% |
| mem, cap 128, B 16 QPs | 6.43 | 57% | 23378 | 1091 | 1 | 53 | 8378 | 128.0 | 13.9 | 0% |
| mem, cap 128, C 16 QPs | 7.00 | 63% | 46917 | 1236 | 1 | 66 | 8607 | 16.4 | 2.1 | 88% |
| mem, cap 128 + 16 placers, B 16 QPs | 7.20 | 64% | 18063 | 993 | 1 | 50 | 7494 | 128.0 | 13.7 | 0% |
| mem, cap 128 + 16 placers, C 16 QPs | 7.86 | 70% | 38064 | 1191 | 1 | 64 | 7371 | 15.3 | 2.9 | 89% |
| device, cap 32, B 16 QPs | 5.88 | 53% | 10285 | 1618 | 378 | 38 | 599 | 32.0 | 19.5 | 0% |
| device, cap 32, C 16 QPs | 5.52 | 49% | 66571 | 1795 | 409 | 40 | 636 | 5.8 | 3.6 | 82% |
| device, cap 128, B 16 QPs | 5.99 | 53% | 14532 | 9280 | 370 | 37 | 614 | 128.0 | 115.2 | 0% |
| device, cap 128, C 16 QPs | 5.67 | 51% | 56840 | 10335 | 385 | 38 | 683 | 23.2 | 20.9 | 82% |

`post` and `reply` are 0-1 us in every run. A repeat of the 8-QP memory cycle
(`mem32e`) gave B 5.48 and C 5.01 GiB/s.

C's stats lines are one-second averages, and one 1 GiB retrieve takes about 0.2 s of each
second (the rest is vLLM decode). So C's in-flight, placer-queue and starved columns
measure that duty cycle, not the server while it fetches. C's per-write stage times are
still valid.

## Findings

1. **LMCache costs about 10%.** At equal settings lw reaches 0.89-0.93 of `kvlayers`
   (5.07 vs 5.55, 4.95 vs 5.48, 5.52 vs 5.88). With cap 128 lw is at or above `kvlayers`
   (7.00 vs 6.43); the lw rate excludes layer 0, and the `kvlayers` stream rate includes
   each batch's tail. Most of the gap to raw is below LMCache.
2. **Memory namespace: the wire stage is the limit, and depth does not raise it.** At
   cap 32 the cap is full (32.0 in flight, placer queue about 2, 0% starved) and a write
   spends 2.5-2.6 ms on the wire. That is 32 x 0.5 MiB / 2.58 ms = 6.1 GiB/s by Little's
   law. Raw `ib_write_bw` with the same 32 writes outstanding (q16 t2) moves 11.2 GiB/s,
   or 1.4 ms per write. Cap 128 makes the wire time grow with the depth (8.4 ms), so the
   rate rises only to 6.4 GiB/s (7.2 with 16 placers). The sink's writes over Soft-RoCE
   saturate around 6.5-7.5 GiB/s; record read (1 us) and copy (35-65 us) are not the
   limit.
3. **Device namespace: the 8 placement threads are the limit.** The device read is about
   380 us per 0.5 MiB write and most admitted writes wait for a placer (19.5 of 32 queued;
   115 of 128 at cap 128). Only about 7 writes are on the wire (0.6 ms each), so cap 128
   does not help (5.88 -> 5.99). More placers (not in this run) are the next lever here.
4. **CPU (E, `kvlayers` at 8 QPs, `breakdown/perf_groups.txt`):** 71% of the 20 CPUs idle
   and no CPU under 50% idle, so the box is not CPU-bound. Of the busy samples, 79% are
   Soft-RoCE kernel workers (`rxe_wq`): skb `memset` 8.8%, `crc32` ICRC about 15%
   (`crypto_shash_update` 7.5% + `crc32_pclmul_le_16` 7.3%), spin locks about 10%,
   `memcpy` 7.0%, `rdma_get_gid_attr` 4.9%. asd is 11%, mostly the staging `memcpy`
   (libc, 6.8%). The cost is per packet in the kernel, serialized per QP, not in asd.
5. **Why rxe moves the sink's writes at half of perftest's rate is not isolated.**
   perftest pays the same per-packet costs. Two differences that fit the data:
   perftest re-sends one hot buffer per QP into one hot buffer, while the sink sends from
   32-128 staging slots into the multi-GiB LMCache region (cache-cold on both sides of the
   rxe copy). And all the sink's QPs target one memory region (lock contention in rxe's
   lookups). Neither applies to a hardware NIC, where this transport cost disappears.

## E during C: not representative

Both C samples (8 QPs) caught the window just after the LMCache MP server started: a
fresh `lmcache` process at 0.6 s of CPU with its RSS growing (650 -> 845 MB), 23% of
samples in `clear_page_erms` (first-touch page zeroing) and 93% idle CPUs. The retrieves
themselves are 0.2 s windows between decodes, so a 5 s system profile would be mostly
decode even if it were placed later.

## Box state after the run

asd stopped and the data file deleted; the `9c16972132` binary is back (md5
`0b953fb421486d003e28ccc90dde2d7b`); LMCache `lmcache/` and `csrc/` clean on the box.
The `314564cfb` binary stays in `/root/lmc-work/asd-314564cfb/` (with the source tar,
not committed). Raw logs are archived off the box.
