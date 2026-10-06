# kv-sink breakdown, day 2 round 6 (Sriram, where asd's threads block, 2026-10-06)

Sriram's follow-up to `BREAKDOWN6.md` (Slack, 2026-10-06 15:29 PT). Each placer takes
~1.9 ms per write, but its timed work (read + copy + post) is only ~75 us, and the box is
half idle. This round is an off-CPU measurement: where do asd's threads block, and for
how long? Server asd `3b1d52fb3`, `kvlayers` on client `e8158149`, memory namespace,
16 QPs, 512 KiB, 32 placers (default), `KV_SINK_STATS=1`, LMCache unchanged.

Two configs (C = `KV_SINK_MAX_IN_FLIGHT`, B = `KV_SINK_PATH_BUDGET_MB`): C128-B4 (the
defaults) and C512-B16. Each is one server cycle with one `--fill --reps 1`, then
`kvlayers --qps 16 --duration 30`. The captures start 3 s into the run and run back to
back:

1. `perf record -e sched:sched_switch -g -p <asd>` for 5 s (frame pointers), then the
   same with `--call-graph dwarf,8192` for 1 s. Frame-pointer stacks stop inside libc
   for placers (`pthread_cond_wait`, the futex `syscall`), so the dwarf capture supplies
   the user frames and the thread -> role map.
2. `perf sched record -a -g` for 5 s, then `perf sched timehist -w -g --state -p <asd>`
   and `perf sched latency`. System-wide, because `perf record -p` misses the switch-in
   that ends each sleep.

Roles come from each thread's stacks: `placer` (`run_placer` or `place`), `poller`
(`run_poller`), `service` (`thr_tsvc` / `epoll_wait`), `other`. A sleep is a switch-out
in state S/D. Its length is the `wait time` on the thread's next timehist line, and its
stack is the `perf script` record of the same `sched.data` at the same (tid, time).

- Script: `scripts/breakdown7.sh`; tables: `scripts/breakdown7_report.py`.
- Outputs: `breakdown7/report.md`, and per config `tables.md` (role, waker and
  blocking-stack tables), `dwarf_stacks.md`, `tid_roles.txt`, `stats_lines.txt`, and the
  first 2,000 lines of each raw text: `offcpu_fp_excerpt.txt`,
  `offcpu_dwarf_excerpt.txt`, `sched_script_excerpt.txt`, `timehist_excerpt.txt`, plus
  `sched_latency.txt`. Raw `perf.data` was deleted after parsing.

## Result

Placers spend their missing time asleep on a per-QP post lock, not working:

- Placers are off CPU 97% of the time in both configs (0.97 / 0.96 s per second per
  thread).
- C512-B16: 99% of placer sleep time is one stack, `cf_mutex_lock <- verbs_post <-
  place`. That is 1.70 ms per write (27.5 s/s over 16,178 writes/s), which is almost
  all of the ~1.95 ms placer time per write (32 / 16,178). Mean sleep 10.9 ms, 0.16
  sleeps per write. 98% of placer wakeups come from another placer, i.e. the mutex
  handoff.
- C128-B4: the same lock costs 1.01 ms per write (54% of placer sleep). Another
  0.87 ms per write is idle in `cf_queue_pop`, waiting for work (32%
  `pthread_cond_wait` + 15% the queue's own mutex). Wakers: 62% another placer (lock
  handoff), 38% the poller (`cf_queue_push`).
- `verbs_post` holds `post_lock[q]` (one per QP, 16 QPs for 32 placers) across
  `ibv_post_send`. On Soft-RoCE that call is a `write()` to the uverbs device. 24% (C128)
  and 39% (C512) of placer switch-outs are preemptions on return from that `write`
  (`R` state, `syscall_exit_to_user_mode <- write`). In the excerpts, the task that
  takes the CPU is almost always a kworker, presumably the rxe work the post just
  queued. So the lock holder often loses its CPU while holding the lock, and the other
  placers on that QP sleep until it runs again. Preemption is 0.17-0.18 ms per write,
  but it stretches every lock hold behind it.
- Wakeup -> run delay for placers: p50 0.09 / 0.18 ms, p99 8.3 / 17.6 ms. A woken placer
  runs on the waker's CPU only 6-11% of the time.
- The stats line hides this: `t_posted` is stamped before the lock is taken, so `post`
  reads 0 us and the lock wait plus `ibv_post_send` count as `wire` (7.4 ms at C128,
  13.5 ms at C512).
- Others: the poller sleeps mostly in its own `usleep` (60% / 97% of its sleep), with
  0.07 / 0.12 sleeps per write. Service threads sleep in `epoll_wait`, at 0.02 per write.
  Neither blocks the write path.

Suggested next steps (server-side, not done here):

- Take the doorbell out of the critical section, or give each placer its own QP so
  there is no lock to hand off.
- Or batch several work requests per `ibv_post_send` under one lock hold.
- Stamp `t_posted` after the lock so the stats line separates lock wait from wire time.

Caveat: every 30 s stream ended with failed rows: C128 1,152 (the same count in all
five C128 runs, including a control with no perf at all), C512 192. They all land in the
final stats second, after the capture windows (3-20 s). Every earlier stats line shows
`failed 0`. Earlier rounds' 25 s runs had none, so this looks like an end-of-stream
effect at 30 s, not perf. Not investigated.

## Tables

1. Per role, 5 s sched window (writes/s = stats lines in the window). Off-CPU =
   sleeps + preemptions.

```
config    role     threads  off-CPU s/s  per thread  sleeps/s  per write  mean ms  wake->run p50/p99 ms  preempts/s
C128-B4   placer   32       31.01        0.969       7934      0.53        3.58    0.094 / 8.34          2716
C128-B4   poller   1         0.85        0.845       1066      0.07        0.72    0.005 / 5.30           276
C128-B4   service  15       12.35        0.823        297      0.02       41.6     0.016 / 7.91             5
C128-B4   other    50       39.39        0.788       1232      0.08       32.0     0.059 / 7.99            33
C512-B16  placer   32       30.78        0.962       2591      0.16       10.75    0.184 / 17.60         1790
C512-B16  poller   1         0.71        0.713       1902      0.12        0.36    0.003 / 7.03            69
C512-B16  service  21       18.91        0.900        308      0.02       61.3     0.017 / 14.72            5
C512-B16  other    50       39.43        0.789       1157      0.07       34.1     0.093 / 15.88           34
```

writes/s: C128-B4 15,080, C512-B16 16,178. `kvlayers`: 7.38 / 7.98 GiB/s over 30 s.

2. Top placer blocking stacks by sleep time (sched.data frame pointers; the futex
   `syscall` frame is `cf_mutex_lock` in `verbs_post`, as the dwarf stacks show).

```
config    stack (user frames)                       sleep s/s  % of placer sleep  sleeps/s  mean ms
C128-B4   syscall (cf_mutex_lock <- verbs_post)       15.25      53.7               3193      4.78
C128-B4   pthread_cond_wait <- cf_queue_pop            8.99      31.7               3574      2.52
C128-B4   cf_queue_pop (queue mutex)                   4.17      14.7               1168      3.57
C512-B16  syscall (cf_mutex_lock <- verbs_post)       27.50      98.8               2531     10.87
C512-B16  pthread_cond_wait <- cf_queue_pop            0.34       1.2                 55      6.20
```

3. Who wakes placers, and whether the placer then ran on the waker's CPU.

```
config    waker        wakeups/s  % of placer wakeups  same CPU %
C128-B4   asd:placer   4748       61.8                 11.0
C128-B4   asd:poller   2939       38.2                 13.3
C512-B16  asd:placer   2530       98.1                  6.4
C512-B16  asd:poller     48        1.9                  3.7
```

4. Placer switch-outs by stack, dwarf capture (1 s, counts, all states).

```
config    state  stack (user frames)                                        % of placer
C128-B4   S      cf_mutex_lock <- verbs_post <- place                        32.3
C128-B4   S      pthread_cond_wait <- cf_queue_pop <- run_placer             30.4
C128-B4   R      write (preempted on syscall exit)                           23.8
C128-B4   S      pthread_mutex_lock <- cf_queue_lock <- cf_queue_pop         10.4
C512-B16  S      cf_mutex_lock <- verbs_post <- place                        56.5
C512-B16  R      write (preempted on syscall exit)                           39.2
C512-B16  R      memcpy <- verbs_post <- place (interrupt)                    2.2
```

Full tables for all roles, including the poller, service and other stacks and wakers,
are in `breakdown7/report.md`.
