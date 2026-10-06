## A. Soft-RoCE counters (rdma statistic show link rxe0/1), window deltas

Window: P8-A 10.0 s, 107.7 GiB, P8-B 13.9 s, 153.4 GiB, S-A 10.0 s, 72.8 GiB, S-B 13.7 s, 94.7 GiB

| counter | P8-A delta | P8-A per GiB | P8-B delta | P8-B per GiB | S-A delta | S-A per GiB | S-B delta | S-B per GiB |
|---|---|---|---|---|---|---|---|---|
| sent_pkts | 28576871 | 265441.7 | 40434726 | 263581.3 | 19352850 | 265722.4 | 25175905 | 265749.8 |
| rcvd_pkts | 28576872 | 265441.7 | 40434726 | 263581.3 | 19352850 | 265722.4 | 25175903 | 265749.8 |
| completer_retry_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| retry_exceeded_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| out_of_seq_request | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| rcvd_seq_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| duplicate_request | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| ack_deferred | 439641 | 4083.7 | 622071 | 4055.1 | 297733 | 4088.0 | 387327 | 4088.5 |
| rcvd_rnr_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| send_rnr_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |

## B. rxe_requester exits (bpftrace, 10 s)

| exit | P8-B count | per GiB | % | S-B count | per GiB | % |
|---|---|---|---|---|---|---|
bpftrace window: P8-B 108.7 GiB (10.87 GiB/s), S-B 68.0 GiB (6.80 GiB/s)

| exit | P8-B count | per GiB | % | S-B count | per GiB | % |
|---|---|---|---|---|---|---|
| sent_packet | 28502177 | 262144 | 97.00 | 17837034 | 262144 | 96.69 |
| window_full | 20335 | 187 | 0.07 | 33849 | 497 | 0.18 |
| rx_backed_up | 862081 | 7929 | 2.93 | 450652 | 6623 | 2.44 |
| nothing_or_other | 469 | 4 | 0.00 | 127005 | 1867 | 0.69 |
| calls | 29775453 | 273854.7 |  | 18709688 | 274969.1 |  |
| retransmit | 0 | 0.0 |  | 0 | 0.0 |  |
| rnr | 0 | 0.0 |  | 0 | 0.0 |  |
