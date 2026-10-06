## 1. Rate and steady stats (clean run)

| config | kvlayers GiB/s (25 s) | stats GiB/s (10 s) | in flight | placer-wait | wire | busy qps | placer queue | CPUs busy |
|---|---|---|---|---|---|---|---|---|
| N128 | 6.67 | 6.60 | 128.0 | 480 | 8513 | 16.0 of 16.0 | 5.2 | 8.3 |
| N512 | 7.32 | 7.33 | 512.0 | 1825 | 30764 | 16.0 of 16.0 | 13.8 | 9.9 |

Steady stats lines (the 3 highest-rate lines in the window):

```
N128:
  14100 writes 6.88 GiB/s failed 0 | us/write queued 17533 placer-wait 480 read 2 copy 61 post 0 wire 8513 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 5.2, starved 0%
  13987 writes 6.83 GiB/s failed 0 | us/write queued 32769 placer-wait 477 read 3 copy 56 post 0 wire 8624 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 15.9 of 16.0, placer queue 4.7, starved 0%
  13753 writes 6.70 GiB/s failed 0 | us/write queued 18696 placer-wait 449 read 2 copy 64 post 0 wire 8790 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 4.0, starved 0%
N512:
  15754 writes 7.68 GiB/s failed 0 | us/write queued 47046 placer-wait 1825 read 1 copy 119 post 1 wire 30764 reply 1 | in flight 512.0 writes 256.0 MiB, busy qps 16.0 of 16.0, placer queue 13.8, starved 0%
  15289 writes 7.46 GiB/s failed 0 | us/write queued 33559 placer-wait 1594 read 4 copy 117 post 0 wire 31762 reply 2 | in flight 512.0 writes 256.0 MiB, busy qps 16.0 of 16.0, placer queue 11.4, starved 0%
  15382 writes 7.39 GiB/s failed 0 | us/write queued 52889 placer-wait 2645 read 3 copy 143 post 1 wire 31424 reply 1 | in flight 512.0 writes 256.0 MiB, busy qps 16.0 of 16.0, placer queue 39.1, starved 0%
```

## 2. rxe_requester exits per GiB (bt run, 10 s)

| exit | N128 | N512 |
|---|---|---|
| sent_packet | 262144 | 262144 |
| window_full | 692 | 344 |
| rx_backed_up | 6697 | 7441 |
| wait_fence | 0 | 0 |
| need_rd_atomic | 0 | 0 |
| nothing_or_other | 1410 | 510 |
| GiB/s (bt window) | 6.46 | 6.98 |
| kvlayers GiB/s (bt run) | 6.64 | 7.10 |

## 3. Placers off CPU (offcpu run, 5 s sched window)

| config | writes/s | off CPU % | sleeps per write | mean sleep ms | wake->run p50 / p99 ms | placer sleep ms per write |
|---|---|---|---|---|---|---|
| N128 | 13761 | 97.5 | 0.61 | 3.413 | 0.050 / 7.465 | 2.093 |
| N512 | 14951 | 96.3 | 0.27 | 6.759 | 0.064 / 19.554 | 1.848 |

Top 3 placer blocking stacks by sleep time:

| config | sleep s/s | ms per write | % of placer sleep | mean ms | stack |
|---|---|---|---|---|---|
| N128 | 24.065 | 1.749 | 83.5 | 3.392 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| N128 | 4.727 | 0.344 | 16.4 | 3.533 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_pop` |
| N128 | 0.012 | 0.001 | 0.0 | 1.651 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| N512 | 22.914 | 1.533 | 83.0 | 6.697 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| N512 | 4.625 | 0.309 | 16.7 | 7.035 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_pop` |
| N512 | 0.085 | 0.006 | 0.3 | 10.633 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |

## 4. lw 8k c=1 at 16 QPs

- config: N512
- layer 0 resident +70.2 ms, layer 31 +143.9 ms after begin_fetch (medians of 5 retrieves); whole fetch 6.95 GiB/s (1 GiB / layer 31 time)
- layer 0 -> 31 rate (breakdown3's measure): 13.20 GiB/s; overstated when layer 0 lands late
- `point E_timeline_qp16_L8192_c1: n=4 errors=0 wall=5.23s ttft_p50~0.187s ext_hit_tokens=32764 out_tokens=[128]`

## 5. Same-session A/B (clean runs, in run order)

| run | kvlayers GiB/s (25 s) | stats GiB/s (10 s) | in flight | placer-wait | wire | busy qps | placer queue | CPUs busy |
|---|---|---|---|---|---|---|---|---|
| 232529-old-N128 | 7.47 | 7.48 | 128.0 | 1177 | 6905 | 16.0 of 16.0 | 14.9 | 9.6 |
| 232602-new-N128 | 6.64 | 6.67 | 128.0 | 454 | 8481 | 15.9 of 16.0 | 4.0 | 8.3 |
| 232636-old-N512 | 8.11 | 8.07 | 512.0 | 18042 | 11972 | 16.0 of 16.0 | 309.5 | 11.2 |
| 232710-new-N512 | 7.25 | 7.28 | 512.0 | 1442 | 31689 | 16.0 of 16.0 | 10.9 | 10.0 |
| 232744-old-N128 | 7.40 | 7.44 | 128.0 | 1234 | 6918 | 16.0 of 16.0 | 17.2 | 9.6 |
| 232818-new-N128 | 6.64 | 6.76 | 128.0 | 448 | 8546 | 16.0 of 16.0 | 4.5 | 8.4 |
