sched window 5.01 s, 13761 writes/s (stats lines in the window). Off-CPU = sleeps + preemptions; per thread = role total / threads.

| role | threads | off-CPU s/s (total) | per thread | sleeps/s | sleeps per write | mean sleep ms | wake->run p50 / p99 ms | preempts/s | preempted s/s |
|---|---|---|---|---|---|---|---|---|---|
| placer | 32 | 31.19 | 0.975 | 8440 | 0.61 | 3.413 | 0.050 / 7.465 | 1649 | 2.388 |
| poller | 1 | 0.94 | 0.942 | 556 | 0.04 | 1.569 | 0.014 / 4.935 | 338 | 0.069 |
| service | 12 | 9.56 | 0.796 | 276 | 0.02 | 34.635 | 0.017 / 7.506 | 5 | 0.002 |
| other | 49 | 39.43 | 0.805 | 1250 | 0.09 | 31.542 | 0.015 / 7.606 | 24 | 0.008 |

Wakers (who woke each role, and whether it then ran on the waker's CPU):

| role | waker | wakeups/s | % of role wakeups | same CPU % |
|---|---|---|---|---|
| placer | asd:poller | 5217 | 63.9 | 12.0 |
| placer | asd:placer | 2951 | 36.1 | 16.8 |
| poller | asd:placer | 363 | 67.7 | 26.3 |
| poller | swapper | 106 | 19.7 | 100.0 |
| poller | kworker | 59 | 11.1 | 88.3 |
| poller | asd:service | 4 | 0.7 | 61.1 |
| service | kvlayers | 187 | 72.4 | 23.9 |
| service | asd:poller | 26 | 10.0 | 1.5 |
| service | asd:service | 17 | 6.6 | 1.2 |
| service | swapper | 10 | 3.9 | 100.0 |
| other | swapper | 565 | 44.9 | 99.9 |
| other | kworker | 399 | 31.6 | 78.9 |
| other | asd:poller | 224 | 17.8 | 9.1 |
| other | asd:placer | 61 | 4.8 | 69.4 |

Top 5 blocking stacks per role by total sleep time (sched.data, frame pointers; placer user frames stop in libc):

| role | sleep s/s | % of role sleep | sleeps/s | mean ms | stack |
|---|---|---|---|---|---|
| placer | 24.065 | 83.5 | 7095 | 3.392 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| placer | 4.727 | 16.4 | 1338 | 3.533 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_pop` |
| placer | 0.012 | 0.0 | 7 | 1.651 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| placer | 0.001 | 0.0 | 0 | 5.160 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_broadcast <- pthread_cond_wait <- cf_queue_pop` |
| poller | 0.596 | 68.3 | 200 | 2.978 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_signal <- cf_queue_push <- verbs_reg` |
| poller | 0.197 | 22.6 | 263 | 0.750 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller` |
| poller | 0.078 | 9.0 | 90 | 0.875 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_push <- verbs_reg` |
| poller | 0.001 | 0.1 | 4 | 0.264 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| poller | 0.000 | 0.0 | 0 | 0.003 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: je_tcache_bin_flush_small` |
| service | 9.548 | 99.9 | 232 | 41.090 | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait` |
| service | 0.006 | 0.1 | 39 | 0.150 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| service | 0.000 | 0.0 | 4 | 0.082 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- verbs_reg` |
| service | 0.000 | 0.0 | 0 | 0.012 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: je_arena_tcache_fill_small` |
| other | 19.531 | 49.5 | 212 | 92.201 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| other | 6.388 | 16.2 | 6 | 1000.802 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- drv_run_maintenance_loop <- mem_maint_free_pool` |
| other | 0.993 | 2.5 | 602 | 1.650 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_stats` |
| other | 0.992 | 2.5 | 171 | 5.812 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout` |
| other | 0.992 | 2.5 | 20 | 50.251 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- mesh_tender` |

