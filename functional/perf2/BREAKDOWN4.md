# kv-sink breakdown, day 2 round 3 (Sriram, where Soft-RoCE work waits, 2026-10-06)

Sriram's follow-up to `BREAKDOWN3.md` (Slack, 2026-10-06 11:08 PT): why Soft-RoCE keeps
~16 cores busy for perftest but ~10 for the sink. Same builds as round 2: asd `0c9703931`
at defaults (32 placers, cap 128), `kvlayers` on client `e8158149`, memory namespace,
`KV_SINK_STATS=1`, LMCache unchanged. The rxe module is built from Linux v6.11.

Configs (16 QPs, 512 KiB writes, each ~20 s so the captures run mid-run back to back):

- P2: `ib_write_bw -q 16 -t 2`; P8: `ib_write_bw -q 16 -t 8` (E1 flags, loopback).
- S: `kvlayers --qps 16 --duration 20` (1 GiB working set, 32 layers x 64 chunks).
- S-hot: S with `--layers 16 --chunks 1 --prefix hot8` (8 MiB working set).

Captures per config: `perf record -a -g -F 999` 5 s with `mpstat -P ALL 1 5`, then the
`workqueue_queue_work` / `execute_start` / `execute_end` tracepoints with `-g` for 2 s,
then `perf sched record` 2 s (S-hot: the profile, mpstat and stats lines only).

- Script: `scripts/breakdown4.sh`; parsers and tables: `scripts/breakdown4_report.py`.
- Outputs: `breakdown4/report.md` (all tables), and per config `prof_script_excerpt.txt`
  and `wq_script_excerpt.txt` (first 3,000 lines of `perf script`), `prof_comm_sym.txt`
  (`perf report --no-children --sort comm,sym`), `prof_cpu.txt` (`--sort cpu`),
  `sched_latency.txt`, `mpstat.txt`, `prof.json`; `S_server/stats_lines.txt`.
- The per-run workqueue lists (`wq.json`, 11-15 MB each) stay on the box in
  `/root/lmc-work/functional/perf2/breakdown4/`.
- A first pass (`breakdown4_run1/` on the box) had a parser bug in items 1 and 3 (it
  looked for the `rdma_rxe` module, but perf resolves module symbols as
  `[kernel.kallsyms]`). Its items 2, 4 and 5 agree with this run (running at once 15.2 /
  15.0 / 8.5; S send-task queue->start p99 1,873 us), so it serves as a repeat.

## Result

The sink does not pay more rxe CPU per GiB (1,390 ms vs perftest's 1,468). It gets
fewer rxe cores because each QP's send task is idle more: running 37% of the time for S
vs 71-75% for perftest, although S keeps 128 writes in flight (8 per QP, P8's depth) on
16.0 of 16 busy QPs. No hop is saturated, no CPU is hot, and median scheduling delay is
the same. So the rxe work isn't waiting for a CPU. It waits for packets and ACKs on each
QP's critical path.

What the data rules out:

- _A saturated task type:_ the busiest per-QP task is perftest's send task at 75%. For S
  it's 37% (send) and 16% (receive). None is near 1 core per QP.
- _Requester work in the posting threads:_ the requester runs in rxe kworkers for both
  (asd: 0.00-0.03 cores of rxe work). On v6.11 a user QP's post queues the send task on
  `rxe_wq` rather than running it inline.
- _Scheduling:_ the average delay for kworkers running rxe work is 0.35-0.37 ms in every
  config. The send-task queue->start p50 is 34-37 us everywhere, and rxe work is spread
  evenly over the 20 CPUs (max / mean 1.1-1.2).
- _Per-QP queue depth:_ P8 (8 per QP) is no faster than P2 (2 per QP), and S has P8's
  depth.

What differs for S:

- _Tail waits:_ the send task's queue->start p99 is 1,977 us for S vs 122 / 227 us for
  P2 / P8; the receive task's is 986 vs 327 / 366 us. The kworker sched max is 35 ms vs
  8-11 ms.
- _Responder cost and lock:_ the responder (receive task) costs 416 ms/GiB vs 320-332.
  `_raw_spin_lock_irqsave` is ~13% of the sink's rxe samples, and the excerpt's chains put
  it on the per-QP responder packet queue: `skb_dequeue` in `rxe_receiver` against
  `skb_queue_tail` in `rxe_resp_queue_pkt` (from `rxe_rcv` in the sender's loopback
  transmit). This lock doesn't show in perftest's top symbols. It is the same lock Q1
  found (`BREAKDOWN2.md`).
- _ICRC:_ perftest spends ~7x more samples in `crypto_shash_update` (18.7k vs 2.5k)
  while moving ~1.5x the bytes. Header-ICRC work per GiB differs a lot between the two;
  not explained.

S-hot doesn't answer the cold-memory question cleanly. With 16 writes per rep, only ~11.5
are in flight (busy qps 9.7 of 16, starved 100%), so S-hot runs at a lower depth than S.
It reaches 6.33 GiB/s (S 7.26), with 12.2 rxe cores and 1,951 ms/GiB.

Possible next steps (not run): count why `rxe_requester` exits each run (window full at
`RXE_MAX_UNACKED_PSNS` vs nothing to send) with a kprobe, for S and P8. Give S-hot 128
distinct in-flight writes in a hot region (for example 128 chunks of one layer) so it
matches S's depth.

## Tables

Item 1, rxe work by task type (5 s, `-F 999`, 1 sample ~ 1 ms). GiB in the window =
rate x 5 s (perftest's reported rate; S from the server's stats lines in the window).

```
task type          P2 ms/GiB cores   P8 ms/GiB cores   S ms/GiB cores   S-hot ms/GiB cores
receive (rxe_rcv)       277  3.01         282  3.03       187  1.33          275  1.72
responder               320  3.48         332  3.57       416  2.96          554  3.46
completer                24  0.26          24  0.26        28  0.20           43  0.27
requester               840  9.12         841  9.06       751  5.35         1061  6.64
all rxe                1468 15.94        1486 16.01      1390  9.90         1951 12.21
GiB in window          54.3              53.9            35.6               31.3
```

By thread: rxe kworkers (`kworker/uN:M-r...`) carry ~87% in every config. Kworkers whose
truncated names don't show `-r` / `+r` carry almost all the rest; `ib_write_bw` 0; asd 0.00 (S) / 0.03 (S-hot) cores.

Item 2, `rxe_wq` `do_work` runs (2 s). A work item's task comes from its `queue_work`
call chains; 16 send + 16 receive items per config. Queue->start counts from when the
item is queued to its next start, so it includes waiting for the item's own current run.

```
config task          runs/GiB  queue->start p50/p99 us  run p50/p99 us  running at once
P2     send task       4127        35 /  122             277 / 321         11.99
P2     receive task    4147       274 /  327              73 / 137          3.35
P8     send task       3931        37 /  227             277 / 321         11.42
P8     receive task    3949       275 /  366              77 / 142          3.39
S      send task       4377        34 / 1977             198 / 538          5.96
S      receive task    4304       199 /  986              87 / 167          2.63
```

Item 3, rxe cores per CPU: P2 0.68-0.97 (CPU 1: 0.01), P8 0.64-0.97 (CPU 6: 0.17), S
0.45-0.53, S-hot 0.46-0.65. Max / mean 1.2, 1.2, 1.1, 1.1. Per-CPU table in
`breakdown4/report.md`.

Item 4, `perf sched latency` (2 s), by thread kind:

```
config kind         runtime ms  switches  avg delay ms  max delay ms
P2     kworker-rxe     28367     102162      0.351          8.08
P2     ib_write_bw      2008         10      0.044          0.27
P8     kworker-rxe     27138      98131      0.344         11.39
P8     ib_write_bw      1984         30      0.967          6.26
S      kworker-rxe     15103      46768      0.369         35.56
S      asd              2204      28016      0.833         66.20
S      kvlayers           32        678      0.585         43.55
```

Item 5, mpstat (5 s average, summed over 20 CPUs): busy 16.9 / 17.0 / 9.8 / 9.6 CPUs for
P2 / P8 / S / S-hot, almost all `%sys` (15.9 / 16.0 / 8.8 / 8.0), `%soft` 0-0.2.

Stats lines during S (steady state): 6.91-7.49 GiB/s, in flight 128.0 writes, busy qps
15.9-16.0 of 16, wire 6,859-7,880 us per write, placer queue 12-20, starved 0%. The
client reported 7.26 GiB/s over 20 s; P2 / P8 reported 93.28 / 93.48 Gb/s (10.86 / 10.88
GiB/s).
