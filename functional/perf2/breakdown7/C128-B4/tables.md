sched window 5.02 s, 15080 writes/s (stats lines in the window). Off-CPU = sleeps + preemptions; per thread = role total / threads.

| role | threads | off-CPU s/s (total) | per thread | sleeps/s | sleeps per write | mean sleep ms | wake->run p50 / p99 ms | preempts/s | preempted s/s |
|---|---|---|---|---|---|---|---|---|---|
| placer | 32 | 31.01 | 0.969 | 7934 | 0.53 | 3.580 | 0.094 / 8.344 | 2716 | 2.600 |
| poller | 1 | 0.85 | 0.845 | 1066 | 0.07 | 0.723 | 0.005 / 5.295 | 276 | 0.074 |
| service | 15 | 12.35 | 0.823 | 297 | 0.02 | 41.645 | 0.016 / 7.905 | 5 | 0.001 |
| other | 50 | 39.39 | 0.788 | 1232 | 0.08 | 31.953 | 0.059 / 7.994 | 33 | 0.010 |

Wakers (who woke each role, and whether it then ran on the waker's CPU):

| role | waker | wakeups/s | % of role wakeups | same CPU % |
|---|---|---|---|---|
| placer | asd:placer | 4748 | 61.8 | 11.0 |
| placer | asd:poller | 2939 | 38.2 | 13.3 |
| poller | swapper | 531 | 50.2 | 100.0 |
| poller | asd:placer | 330 | 31.2 | 56.8 |
| poller | kworker | 178 | 16.9 | 91.2 |
| poller | asd:service | 7 | 0.7 | 88.6 |
| service | kvlayers | 194 | 70.4 | 41.7 |
| service | asd:service | 25 | 9.2 | 0.0 |
| service | asd:poller | 21 | 7.5 | 2.9 |
| service | asd:other | 14 | 5.2 | 25.0 |
| other | swapper | 458 | 36.9 | 99.9 |
| other | kworker | 458 | 36.9 | 80.5 |
| other | asd:poller | 256 | 20.6 | 14.3 |
| other | asd:placer | 60 | 4.8 | 83.1 |

Top 5 blocking stacks per role by total sleep time (sched.data, frame pointers; placer user frames stop in libc):

| role | sleep s/s | % of role sleep | sleeps/s | mean ms | stack |
|---|---|---|---|---|---|
| placer | 15.249 | 53.7 | 3193 | 4.776 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| placer | 8.993 | 31.7 | 3574 | 2.516 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| placer | 4.165 | 14.7 | 1168 | 3.567 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_pop` |
| poller | 0.461 | 59.8 | 916 | 0.504 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller` |
| poller | 0.259 | 33.6 | 109 | 2.382 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_signal <- cf_queue_push <- verbs_reg` |
| poller | 0.048 | 6.3 | 39 | 1.235 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_push <- verbs_reg` |
| poller | 0.003 | 0.4 | 3 | 0.940 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| service | 12.345 | 99.9 | 250 | 49.377 | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait` |
| service | 0.007 | 0.1 | 44 | 0.155 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| service | 0.000 | 0.0 | 3 | 0.020 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- verbs_reg` |
| other | 19.529 | 49.6 | 231 | 84.652 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| other | 6.383 | 16.2 | 6 | 1002.058 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- drv_run_maintenance_loop <- mem_maint_free_pool` |
| other | 0.994 | 2.5 | 164 | 6.076 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout` |
| other | 0.991 | 2.5 | 20 | 50.300 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- hb_transmitter` |
| other | 0.989 | 2.5 | 575 | 1.721 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_stats` |

