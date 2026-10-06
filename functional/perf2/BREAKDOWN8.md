# kv-sink breakdown, day 2 round 7 (Sriram, post lock removed, 2026-10-06)

Sriram's follow-up to `BREAKDOWN7.md` (Slack, 2026-10-06 16:08 PT). Server asd `55d6ae8d8`
posts RC writes without the per-QP post lock that round 6 found placers asleep on (SRD
keeps it). `kvlayers` on client `e8158149`, memory namespace, 16 QPs, 512 KiB, 32 placers
(default), `KV_SINK_STATS=1`, LMCache unchanged.

- N128: defaults (cap 128, budget 4 MiB per QP). N512: `KV_SINK_MAX_IN_FLIGHT=512
  KV_SINK_PATH_BUDGET_MB=16`.
- Each config is one server cycle with one fill, then three `kvlayers --qps 16
  --duration 25` runs, each measured 3 s in:
  - clean: mpstat, 10 s
  - bt: round 4's bpftrace exits, same offsets, 10 s
  - offcpu: round 6's capture (`sched_switch` dwarf 1 s for the role map, then `perf
    sched record -a -g` 5 s)
- lw 8k c=1 at 16 QPs with the faster config (N512), as `breakdown3.sh`'s run 4. The E3
  print patch was applied for the session and reverted, and the tree was checked clean.
- Same-session A/B (step `ab`): clean runs alternating `3b1d52fb3` and `55d6ae8d8`. This
  was added because both configs came out slower than round 5.
- Runs stay at 25 s. Sriram traced the failed rows at the end of 30 s streams to strict
  per-sink priority under nonstop resubmission: high layers' batches wait until the
  stream ends and pass their 30 s deadline. 1,152 = 18 starved layers x 64 rows.

- Script: `scripts/breakdown8.sh` (steps `build runs lw`, then `ab`); tables:
  `scripts/breakdown8_report.py`.
- Outputs: `breakdown8/report.md`; per config `clean/`, `bt/`, `offcpu/` (round 6's
  `tables.md`, `dwarf_stacks.md` and excerpts) and `stats_lines.txt`; `lw/` (point and
  session files); `ab/` (one directory per A/B run).

## Result

The lock is gone and placers no longer block on it, but on this box `55d6ae8d8` is ~11%
slower, and layerwise's first layer lands later:

- Same-session A/B: N128 6.64 / 6.64 GiB/s vs 7.47 / 7.40 for `3b1d52fb3`, and N512
  7.25 vs 8.11. The runs alternated, so this is the code change, not drift.
- Placers: post-lock sleep went from 1.0 / 1.7 ms per write (round 6) to 0.001 / 0.006
  ms. Placers are still off CPU 96-97% of the time, but now they wait for work: 83% of
  their sleep is `pthread_cond_wait <- cf_queue_pop` (queue empty), the rest the queue
  mutex. The placer queue fell from 15 / 310 to 4 / 11, and placer-wait from 1.2 / 18 ms
  to 0.45 / 1.4 ms.
- So the limit moved to the wire. Per-write `wire` time rose from 6.9 to 8.5 ms (N128)
  and 12.0 to 31.7 ms (N512). Busy QPs stayed at 16.0 of 16, and CPUs busy fell
  (9.6 -> 8.3, 11.2 -> 10.0).
- Nothing-to-send fell 1,878 -> 1,410 (N128) and 622 -> 510 (N512) per GiB; window
  full is 692 / 344. The QPs have work more often but move fewer bytes.
- lw 8k c=1 (N512): TTFT p50 0.187 s (round 3, `0c9703931` defaults: 0.199 s). The whole
  fetch is slightly faster (layer 31 at +144 ms vs +154 ms, 6.95 vs 6.48 GiB/s). But
  layer 0 now lands at +70 ms instead of +26 ms. Concurrent posting interleaves all
  layers on the wire, so the low layers no longer go first. breakdown3's layer 0 -> 31
  rate (13.2 GiB/s) is meaningless here, because it divides by that shorter span.

Reading (not measured): with the lock, posts to a QP were serialized, and the doorbell
`write()` of one placer often let the rxe worker drain a batch. Without it, several
placers ring the same QP at once. That gives more doorbells and more rxe task
scheduling per byte, and posts interleave across layers, so per-QP delivery gets slower
and priority order is lost. Next measurements, if useful: rxe send-task runs per GiB
and per-run packet counts (`do_task` calls / GiB, as round 3), and post concurrency per
QP. A middle ground to test: keep posts serialized per QP but ring the doorbell outside
any sleeping lock (a spinlock, or one placer per QP).

## Tables

1. Same-session A/B, clean runs (run order; GiB/s from `kvlayers` over 25 s and the
   stats lines in the 10 s window; fields from the highest-rate stats line).

```
run              kvlayers  stats  in flight  placer-wait us  wire us  busy qps  placer queue  CPUs
old-N128 (1)     7.47      7.48   128        1177            6905     16.0/16   14.9          9.6
new-N128 (1)     6.64      6.67   128         454            8481     15.9/16    4.0          8.3
old-N512         8.11      8.07   512       18042           11972     16.0/16  309.5          11.2
new-N512         7.25      7.28   512        1442           31689     16.0/16   10.9          10.0
old-N128 (2)     7.40      7.44   128        1234            6918     16.0/16   17.2          9.6
new-N128 (2)     6.64      6.76   128         448            8546     16.0/16    4.5          8.4
```

2. `55d6ae8d8` runs (`runs` step). Clean: GiB/s and CPUs; bt: exits per GiB (GiB =
   `sent_packet` / 262,144); round 5 (`3b1d52fb3`) for comparison.

```
                    N128    N512    round 5 C128-B4  round 5 C512-B16
kvlayers GiB/s      6.67    7.32    7.52             8.14
CPUs busy           8.3     9.9     9.5              11.0
window_full         692     344     500              280
rx_backed_up        6697    7441    6629             7517
nothing_or_other    1410    510     1878             622
GiB/s (bt window)   6.46    6.98    7.23             7.86
```

3. Placers off CPU (offcpu run, 5 s sched window; round 6 for comparison).

```
                          N128     N512     round 6 C128-B4  round 6 C512-B16
writes/s                  13761    14951    15080            16178
off CPU %                 97.5     96.3     96.9             96.2
sleeps per write          0.61     0.27     0.53             0.16
placer sleep ms / write   2.09     1.85     1.88             1.72
  in the post lock        0.001    0.006    1.01             1.70
  in cf_queue_pop (idle)  2.09     1.84     0.87             0.02
wake->run p50 / p99 ms    0.050 / 7.5  0.064 / 19.6  0.094 / 8.3  0.184 / 17.6
```

Top 3 placer blocking stacks (both configs): `pthread_cond_wait <- cf_queue_pop` (83%),
`cf_queue_pop` queue mutex (16-17%), futex `syscall` (0.0-0.3%).

4. lw 8k c=1 at 16 QPs (E3 timeline, medians of 5 retrieves, times after
   `begin_fetch`).

```
                       55d6ae8d8 N512   round 3 (0c9703931 defaults)
layer 0 resident       +70.2 ms         +26.3 ms
layer 31 resident      +143.9 ms        +154.3 ms
whole fetch GiB/s      6.95             6.48
TTFT p50               0.187 s          0.199 s
```
