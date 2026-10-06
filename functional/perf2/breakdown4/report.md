## 1. rxe work by task type (5 s, -F 999): ms/GiB, cores

GiB in the window: P2 54.3, P8 53.9, S 35.6, S-hot 31.3

| task type | P2 ms/GiB | P2 cores | P8 ms/GiB | P8 cores | S ms/GiB | S cores | S-hot ms/GiB | S-hot cores |
|---|---|---|---|---|---|---|---|---|
| receive (rxe_rcv) | 277 | 3.01 | 282 | 3.03 | 187 | 1.33 | 275 | 1.72 |
| responder | 320 | 3.48 | 332 | 3.57 | 416 | 2.96 | 554 | 3.46 |
| completer | 24 | 0.26 | 24 | 0.26 | 28 | 0.20 | 43 | 0.27 |
| requester | 840 | 9.12 | 841 | 9.06 | 751 | 5.35 | 1061 | 6.64 |
| rxe other | 7 | 0.08 | 7 | 0.08 | 9 | 0.07 | 18 | 0.11 |
| all rxe | 1468 | 15.94 | 1486 | 16.01 | 1390 | 9.90 | 1951 | 12.21 |

### rxe work by task type and thread kind (cores)

- P2: requester in kworker-rxe 7.91; responder in kworker-rxe 3.00; receive (rxe_rcv) in kworker-rxe 2.61; requester in kworker 1.21; responder in kworker 0.48; receive (rxe_rcv) in kworker 0.40; completer in kworker-rxe 0.23; rxe other in kworker-rxe 0.06; completer in kworker 0.03; rxe other in kworker 0.01
- P8: requester in kworker-rxe 7.78; responder in kworker-rxe 3.06; receive (rxe_rcv) in kworker-rxe 2.60; requester in kworker 1.26; responder in kworker 0.50; receive (rxe_rcv) in kworker 0.42; completer in kworker-rxe 0.23; rxe other in kworker-rxe 0.06; completer in kworker 0.03; requester in kthreadd 0.03
- S: requester in kworker-rxe 4.67; responder in kworker-rxe 2.59; receive (rxe_rcv) in kworker-rxe 1.16; requester in kworker 0.66; responder in kworker 0.36; completer in kworker-rxe 0.17; receive (rxe_rcv) in kworker 0.17; rxe other in kworker-rxe 0.05; completer in kworker 0.02; requester in kthreadd 0.02
- S-hot: requester in kworker-rxe 5.87; responder in kworker-rxe 3.06; receive (rxe_rcv) in kworker-rxe 1.52; requester in kworker 0.77; responder in kworker 0.40; completer in kworker-rxe 0.24; receive (rxe_rcv) in kworker 0.21; rxe other in kworker-rxe 0.07; rxe other in asd 0.03; completer in kworker 0.03

## 3. rxe work by CPU (cores busy with rxe work)

| config | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | max / mean |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P2 | 0.89 | 0.01 | 0.97 | 0.70 | 0.80 | 0.85 | 0.68 | 0.81 | 0.88 | 0.86 | 0.83 | 0.87 | 0.86 | 0.72 | 0.90 | 0.92 | 0.95 | 0.68 | 0.84 | 0.92 | 1.2 |
| P8 | 0.81 | 0.90 | 0.80 | 0.80 | 0.96 | 0.72 | 0.17 | 0.64 | 0.72 | 0.85 | 0.82 | 0.85 | 0.87 | 0.97 | 0.77 | 0.86 | 0.87 | 0.89 | 0.86 | 0.89 | 1.2 |
| S | 0.52 | 0.49 | 0.51 | 0.51 | 0.45 | 0.49 | 0.52 | 0.51 | 0.49 | 0.48 | 0.53 | 0.51 | 0.49 | 0.51 | 0.48 | 0.48 | 0.47 | 0.46 | 0.51 | 0.50 | 1.1 |
| S-hot | 0.61 | 0.65 | 0.46 | 0.62 | 0.60 | 0.59 | 0.63 | 0.61 | 0.63 | 0.60 | 0.65 | 0.62 | 0.63 | 0.51 | 0.64 | 0.61 | 0.63 | 0.62 | 0.65 | 0.63 | 1.1 |

## 2. rxe_wq do_work runs (2 s)

| config | task | runs | runs/GiB | queue->start p50 / p99 us | run p50 / p99 us | running at once | work items |
|---|---|---|---|---|---|---|---|
| P2 | all | 179681 | 8273 | 173 / 317 | 143 / 315 | 15.34 | 32 |
| P2 | send task | 89623 | 4127 | 35 / 122 | 277 / 321 | 11.99 | 16 |
| P2 | receive task | 90058 | 4147 | 274 / 327 | 73 / 137 | 3.35 | 16 |
| P8 | all | 169817 | 7880 | 160 / 333 | 144 / 316 | 14.81 | 32 |
| P8 | send task | 84706 | 3931 | 37 / 227 | 277 / 321 | 11.42 | 16 |
| P8 | receive task | 85111 | 3949 | 275 / 366 | 77 / 142 | 3.39 | 16 |
| S | all | 127268 | 8681 | 155 / 1319 | 107 / 404 | 8.59 | 32 |
| S | send task | 64174 | 4377 | 34 / 1977 | 198 / 538 | 5.96 | 16 |
| S | receive task | 63094 | 4304 | 199 / 986 | 87 / 167 | 2.63 | 16 |

## 4. perf sched latency (2 s), by thread kind (top 6 by runtime)

| config | kind | runtime ms | switches | avg delay ms | max delay ms |
|---|---|---|---|---|---|
| P2 | kworker-rxe | 28367 | 102162 | 0.351 | 8.079 |
| P2 | kworker | 3850 | 13325 | 0.353 | 7.289 |
| P2 | ib_write_bw | 2008 | 10 | 0.044 | 0.274 |
| P2 | perf | 41 | 3 | 0.447 | 1.313 |
| P2 | containerd | 2 | 56 | 0.307 | 3.008 |
| P2 | migration | 2 | 41 | 0.048 | 0.248 |
| P8 | kworker-rxe | 27138 | 98131 | 0.344 | 11.387 |
| P8 | kworker | 3522 | 14068 | 0.336 | 6.719 |
| P8 | ib_write_bw | 1984 | 30 | 0.967 | 6.263 |
| P8 | kthreadd | 296 | 1245 | 0.348 | 3.890 |
| P8 | perf | 38 | 3 | 1.354 | 3.827 |
| P8 | containerd | 2 | 52 | 0.181 | 2.568 |
| S | kworker-rxe | 15103 | 46768 | 0.369 | 35.562 |
| S | asd | 2204 | 28016 | 0.833 | 66.203 |
| S | kworker | 1959 | 5750 | 0.371 | 9.971 |
| S | kvlayers | 32 | 678 | 0.585 | 43.550 |
| S | perf | 29 | 3 | 0.232 | 0.272 |
| S | containerd | 1 | 72 | 0.416 | 8.851 |

## 5. mpstat (5 s average, summed over CPUs)

| config | busy CPUs | sys | soft |
|---|---|---|---|
| P2 | 16.9 | 15.9 | 0.0 |
| P8 | 17.0 | 16.0 | 0.0 |
| S | 9.8 | 8.8 | 0.0 |
| S-hot | 9.6 | 8.0 | 0.2 |
