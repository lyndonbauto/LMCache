## Run 3: ib_write_bw -s 524288 -t 2

| QPs | GiB/s |
|---|---|
| 16 | 10.83 |
| 24 | 10.52 |
| 32 | 10.06 |
| 64 | 9.47 |

## Runs 1, 2, 5: kvlayers, memory namespace, server defaults

| run | QPs | stream GiB/s | of raw | stats GiB/s | busy qps | in flight | placer queue | placer-wait | copy | wire (us/write) | starved |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 16 | 7.21 | 67% | 7.31 | 16.0 of 16 | 128 | 15.0 | 1191 | 61 | 7278 | 0% |
| 1 | 16 | 7.14 | 66% | 7.21 | 15.9 of 16 | 128 | 16.6 | 1268 | 61 | 7283 | 0% |
| 2 | 24 | 7.31 | 69% | 7.38 | 23.9 of 24 | 128 | 14.6 | 1242 | 64 | 7115 | 0% |
| 2 | 32 | 7.00 | 70% | 7.11 | 31.7 of 32 | 128 | 10.1 | 1020 | 64 | 7755 | 0% |
| 5 | 16 | 6.93 | 64% | 6.99 | 16.0 of 16 | 128 | 16.5 | 1236 | 62 | 7668 | 0% |

## Run 5: CPU during kvlayers --qps 16

- GiB landed in the window: 34.6
- rxe workers: 50039 samples, 10.0 cores busy, 1446 ms/GiB
- mpstat: 9.8 CPUs busy

| thread kind | samples | ms/GiB |
|---|---|---|
| kworker | 50039 | 1446 |
| asd | 6324 | 183 |
| swapper | 296 | 9 |
| kvlayers | 97 | 3 |
| containerd-shim | 8 | 0 |
| perf | 7 | 0 |
| mpstat | 3 | 0 |
| f2b | 3 | 0 |

## Run 4: lw 8k c=1, 16 QPs (LMCache's maximum)

- per-layer timeline: 7.52 GiB/s
