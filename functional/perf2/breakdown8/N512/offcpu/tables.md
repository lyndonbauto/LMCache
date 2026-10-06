sched window 5.03 s, 14951 writes/s (stats lines in the window). Off-CPU = sleeps + preemptions; per thread = role total / threads.

| role | threads | off-CPU s/s (total) | per thread | sleeps/s | sleeps per write | mean sleep ms | wake->run p50 / p99 ms | preempts/s | preempted s/s |
|---|---|---|---|---|---|---|---|---|---|
| placer | 32 | 30.81 | 0.963 | 4087 | 0.27 | 6.759 | 0.064 / 19.554 | 1266 | 3.187 |
| poller | 1 | 0.95 | 0.951 | 304 | 0.02 | 2.850 | 0.013 / 15.215 | 215 | 0.085 |
| service | 20 | 18.99 | 0.950 | 348 | 0.02 | 54.540 | 0.015 / 19.932 | 9 | 0.001 |
| other | 49 | 39.36 | 0.803 | 1185 | 0.08 | 33.217 | 0.018 / 19.720 | 38 | 0.010 |

Wakers (who woke each role, and whether it then ran on the waker's CPU):

| role | waker | wakeups/s | % of role wakeups | same CPU % |
|---|---|---|---|---|
| placer | asd:poller | 2566 | 65.0 | 12.7 |
| placer | asd:placer | 1380 | 35.0 | 18.0 |
| poller | asd:placer | 195 | 67.6 | 32.4 |
| poller | swapper | 60 | 20.8 | 100.0 |
| poller | kworker | 23 | 7.9 | 93.9 |
| poller | asd:service | 8 | 2.6 | 39.5 |
| service | kvlayers | 174 | 55.7 | 18.2 |
| service | asd:poller | 52 | 16.8 | 1.1 |
| service | asd:service | 52 | 16.6 | 0.4 |
| service | asd:other | 14 | 4.5 | 8.5 |
| other | swapper | 510 | 42.7 | 99.9 |
| other | kworker | 379 | 31.7 | 84.8 |
| other | asd:poller | 240 | 20.1 | 12.0 |
| other | asd:placer | 55 | 4.6 | 74.3 |

Top 5 blocking stacks per role by total sleep time (sched.data, frame pointers; placer user frames stop in libc):

| role | sleep s/s | % of role sleep | sleeps/s | mean ms | stack |
|---|---|---|---|---|---|
| placer | 22.914 | 83.0 | 3422 | 6.697 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| placer | 4.625 | 16.7 | 657 | 7.035 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_pop` |
| placer | 0.085 | 0.3 | 8 | 10.633 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| poller | 0.592 | 68.3 | 93 | 6.332 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_signal <- cf_queue_push <- verbs_reg` |
| poller | 0.162 | 18.7 | 143 | 1.128 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller` |
| poller | 0.106 | 12.3 | 61 | 1.735 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_push <- verbs_reg` |
| poller | 0.007 | 0.8 | 6 | 1.107 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| service | 18.964 | 99.9 | 242 | 78.296 | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait` |
| service | 0.021 | 0.1 | 96 | 0.216 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| service | 0.006 | 0.0 | 10 | 0.588 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- verbs_reg` |
| other | 19.494 | 49.5 | 231 | 84.364 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| other | 6.370 | 16.2 | 6 | 1000.976 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- drv_run_maintenance_loop <- mem_maint_free_pool` |
| other | 0.992 | 2.5 | 553 | 1.794 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_stats` |
| other | 0.992 | 2.5 | 152 | 6.544 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout` |
| other | 0.990 | 2.5 | 19 | 50.782 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- hb_adjacency_tender <- joinable_shim_fn` |

