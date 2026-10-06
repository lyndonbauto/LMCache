## A. Soft-RoCE counters (rdma statistic show link rxe0/1), window deltas

Window: P8-A 10.0 s, 108.6 GiB, P8-B 13.8 s, 148.2 GiB, S-A 10.0 s, 72.6 GiB, S-B 13.7 s, 95.9 GiB

| counter | P8-A delta | P8-A per GiB | P8-B delta | P8-B per GiB | S-A delta | S-A per GiB | S-B delta | S-B per GiB |
|---|---|---|---|---|---|---|---|---|
| sent_pkts | 29102523 | 267870.0 | 39428916 | 266041.3 | 19294050 | 265716.1 | 25507308 | 265897.8 |
| rcvd_pkts | 29102523 | 267870.0 | 39428914 | 266041.3 | 19294051 | 265716.1 | 25507308 | 265897.8 |
| completer_retry_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| retry_exceeded_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| out_of_seq_request | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| rcvd_seq_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| duplicate_request | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| ack_deferred | 447730 | 4121.1 | 606598 | 4092.9 | 296832 | 4087.9 | 392430 | 4090.8 |
| rcvd_rnr_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |
| send_rnr_err | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 | 0 | 0.0 |

## B. rxe_requester exits (bpftrace, 10 s)

bpftrace window: P8-B 105.6 GiB (10.56 GiB/s), S-B 69.1 GiB (6.91 GiB/s)

| exit | P8-B count | per GiB | % | S-B count | per GiB | % |
|---|---|---|---|---|---|---|
| sent_packet | 27691850 | 262144 | 97.00 | 18109569 | 262144 | 96.69 |
| window_full | 19749 | 187 | 0.07 | 34569 | 500 | 0.18 |
| rx_backed_up | 837292 | 7926 | 2.93 | 458526 | 6637 | 2.45 |
| wait_fence | 0 | 0 | 0.00 | 0 | 0 | 0.00 |
| need_rd_atomic | 0 | 0 | 0.00 | 0 | 0 | 0.00 |
| nothing_or_other | 523 | 5 | 0.00 | 126426 | 1830 | 0.68 |
| calls | 29040190 | 274908.0 |  | 19026104 | 275411.2 |  |
| retransmit | 0 | 0.0 |  | 0 | 0.0 |  |
| rnr | 0 | 0.0 |  | 0 | 0.0 |  |
