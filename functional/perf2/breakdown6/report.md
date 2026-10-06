## 1. Rate and steady stats (clean run)

| config | kvlayers GiB/s (25 s) | stats GiB/s (10 s) | in flight | placer-wait us | wire us | busy qps | placer queue | CPUs busy |
|---|---|---|---|---|---|---|---|---|
| C128-B4 | 7.52 | 7.48 | 128.0 | 1119 | 6913 | 16.0 of 16.0 | 13.8 | 9.5 |
| C256-B4 | 7.56 | 7.54 | 128.0 | 996 | 7090 | 16.0 of 16.0 | 13.9 | 9.5 |
| C256-B8 | 8.01 | 8.02 | 256.0 | 4754 | 10345 | 16.0 of 16.0 | 79.8 | 10.5 |
| C512-B16 | 8.14 | 8.15 | 512.0 | 17284 | 12291 | 16.0 of 16.0 | 301.5 | 11.0 |

Steady stats lines (the 3 highest-rate lines in the window):

```
C128-B4:
  15868 writes 7.75 GiB/s failed 0 | us/write queued 24086 placer-wait 1119 read 1 copy 54 post 0 wire 6913 reply 0 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 13.8, starved 0%
  15591 writes 7.58 GiB/s failed 0 | us/write queued 52067 placer-wait 976 read 1 copy 54 post 0 wire 7230 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 11.9, starved 0%
  15510 writes 7.55 GiB/s failed 0 | us/write queued 39886 placer-wait 1117 read 1 copy 53 post 0 wire 7099 reply 0 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 13.9, starved 0%
C256-B4:
  15823 writes 7.67 GiB/s failed 0 | us/write queued 24970 placer-wait 996 read 2 copy 55 post 0 wire 7090 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 13.9, starved 0%
  15714 writes 7.67 GiB/s failed 0 | us/write queued 20754 placer-wait 1077 read 1 copy 53 post 0 wire 7012 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 14.9, starved 0%
  15709 writes 7.65 GiB/s failed 0 | us/write queued 21191 placer-wait 1100 read 2 copy 57 post 0 wire 7034 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 14.8, starved 0%
C256-B8:
  17026 writes 8.28 GiB/s failed 0 | us/write queued 26607 placer-wait 4754 read 2 copy 64 post 0 wire 10345 reply 0 | in flight 256.0 writes 128.0 MiB, busy qps 16.0 of 16.0, placer queue 79.8, starved 0%
  16813 writes 8.15 GiB/s failed 0 | us/write queued 22181 placer-wait 4398 read 2 copy 59 post 0 wire 11058 reply 1 | in flight 256.0 writes 128.0 MiB, busy qps 16.0 of 16.0, placer queue 65.1, starved 0%
  16604 writes 8.10 GiB/s failed 0 | us/write queued 31687 placer-wait 4912 read 1 copy 65 post 0 wire 10435 reply 1 | in flight 256.0 writes 128.0 MiB, busy qps 16.0 of 16.0, placer queue 79.4, starved 0%
C512-B16:
  17215 writes 8.34 GiB/s failed 0 | us/write queued 21592 placer-wait 17284 read 2 copy 70 post 0 wire 12291 reply 0 | in flight 512.0 writes 256.0 MiB, busy qps 16.0 of 16.0, placer queue 301.5, starved 0%
  16919 writes 8.26 GiB/s failed 0 | us/write queued 31432 placer-wait 16857 read 2 copy 69 post 0 wire 13824 reply 1 | in flight 512.0 writes 256.0 MiB, busy qps 16.0 of 16.0, placer queue 288.1, starved 0%
  16984 writes 8.25 GiB/s failed 0 | us/write queued 27003 placer-wait 15883 read 1 copy 76 post 0 wire 14731 reply 1 | in flight 512.0 writes 256.0 MiB, busy qps 16.0 of 16.0, placer queue 252.6, starved 0%
```

## 2. rxe_requester exits (bt run, 10 s)

| exit | C128-B4 per GiB | % | C256-B4 per GiB | % | C256-B8 per GiB | % | C512-B16 per GiB | % |
|---|---|---|---|---|---|---|---|---|
| sent_packet | 262144 | 96.68 | 262144 | 96.68 | 262144 | 96.84 | 262144 | 96.89 |
| window_full | 500 | 0.18 | 514 | 0.19 | 326 | 0.12 | 280 | 0.10 |
| rx_backed_up | 6629 | 2.44 | 6612 | 2.44 | 7328 | 2.71 | 7517 | 2.78 |
| wait_fence | 0 | 0.00 | 0 | 0.00 | 0 | 0.00 | 0 | 0.00 |
| need_rd_atomic | 0 | 0.00 | 0 | 0.00 | 0 | 0.00 | 0 | 0.00 |
| nothing_or_other | 1878 | 0.69 | 1886 | 0.70 | 904 | 0.33 | 622 | 0.23 |
| GiB/s (bt window) | 7.23 |  | 7.09 |  | 7.62 |  | 7.86 |  |
