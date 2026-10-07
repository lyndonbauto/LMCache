| point | settings | kvlayers GiB/s | stats GiB/s | in flight | placer-wait | wire | busy qps | placer queue | CPUs busy | lw TTFT p50 s | layer 0 ms | layer 31 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01-old-def | `old STATS=1` | 7.53 | 7.51 | 128.0 | 929 | 7174 | 16.0 of 16.0 | 13.9 | 9.4 | 0.177 | 24.0 | 137.7 |
| 02-new-B1 | `new STATS=1 PATH_BUDGET_MB=1` | 6.41 | 6.45 | 32.0 | 199 | 2146 | 15.7 of 16.0 | 2.2 | 7.7 | 0.226 | 11.0 | 185.8 |
| 03-new-B2 | `new STATS=1 PATH_BUDGET_MB=2` | 6.71 | 6.73 | 64.0 | 286 | 4189 | 15.9 of 16.0 | 2.8 | 7.9 | 0.214 | 20.8 | 175.4 |
| 04-new-def | `new STATS=1` | 6.82 | 6.86 | 128.0 | 427 | 8265 | 16.0 of 16.0 | 4.2 | 8.3 | 0.203 | 21.6 | 161.2 |
| 05-new-C32 | `new STATS=1 MAX_IN_FLIGHT=32` | 6.22 | 6.17 | 32.0 | 212 | 2184 | 15.7 of 16.0 | 2.3 | 7.7 | 0.234 | 12.6 | 184.9 |
| 06-old-def2 | `old STATS=1` | 7.48 | 7.50 | 128.0 | 1217 | 6831 | 16.0 of 16.0 | 18.6 | 9.6 | 0.179 | 29.7 | 138.6 |

Steady stats lines (the 2 highest-rate lines in each 10 s window):

```
01-old-def:
  15802 writes 7.72 GiB/s failed 0 | us/write queued 19112 placer-wait 929 read 1 copy 54 post 0 wire 7174 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 13.9, starved 0%
  15740 writes 7.68 GiB/s failed 0 | us/write queued 25890 placer-wait 1108 read 1 copy 56 post 0 wire 6994 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 15.7, starved 0%
02-new-B1:
  13352 writes 6.52 GiB/s failed 0 | us/write queued 12133 placer-wait 199 read 2 copy 49 post 0 wire 2146 reply 1 | in flight 32.0 writes 16.0 MiB, busy qps 15.7 of 16.0, placer queue 2.2, starved 0%
  13362 writes 6.51 GiB/s failed 0 | us/write queued 10075 placer-wait 216 read 2 copy 48 post 0 wire 2135 reply 1 | in flight 32.0 writes 16.0 MiB, busy qps 15.7 of 16.0, placer queue 2.5, starved 0%
03-new-B2:
  14149 writes 6.90 GiB/s failed 0 | us/write queued 19106 placer-wait 286 read 2 copy 49 post 0 wire 4189 reply 0 | in flight 64.0 writes 32.0 MiB, busy qps 15.9 of 16.0, placer queue 2.8, starved 0%
  14053 writes 6.84 GiB/s failed 0 | us/write queued 12280 placer-wait 266 read 2 copy 50 post 0 wire 4245 reply 1 | in flight 64.0 writes 32.0 MiB, busy qps 16.0 of 16.0, placer queue 2.2, starved 0%
04-new-def:
  14504 writes 7.08 GiB/s failed 0 | us/write queued 16887 placer-wait 427 read 2 copy 59 post 0 wire 8265 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 4.2, starved 0%
  14292 writes 6.96 GiB/s failed 0 | us/write queued 35001 placer-wait 456 read 2 copy 59 post 0 wire 8441 reply 0 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 4.5, starved 0%
05-new-C32:
  13045 writes 6.37 GiB/s failed 0 | us/write queued 12544 placer-wait 212 read 2 copy 53 post 0 wire 2184 reply 1 | in flight 32.0 writes 16.0 MiB, busy qps 15.7 of 16.0, placer queue 2.3, starved 0%
  12913 writes 6.30 GiB/s failed 0 | us/write queued 10604 placer-wait 222 read 3 copy 53 post 1 wire 2196 reply 1 | in flight 32.0 writes 16.0 MiB, busy qps 15.7 of 16.0, placer queue 2.4, starved 0%
06-old-def2:
  15895 writes 7.75 GiB/s failed 0 | us/write queued 20039 placer-wait 1217 read 2 copy 58 post 0 wire 6831 reply 0 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 18.6, starved 0%
  15681 writes 7.65 GiB/s failed 0 | us/write queued 30002 placer-wait 1054 read 1 copy 57 post 0 wire 7091 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 15.1, starved 0%
```
