sched window 5.03 s, 16178 writes/s (stats lines in the window). Off-CPU = sleeps + preemptions; per thread = role total / threads.

| role | threads | off-CPU s/s (total) | per thread | sleeps/s | sleeps per write | mean sleep ms | wake->run p50 / p99 ms | preempts/s | preempted s/s |
|---|---|---|---|---|---|---|---|---|---|
| placer | 32 | 30.78 | 0.962 | 2591 | 0.16 | 10.749 | 0.184 / 17.601 | 1790 | 2.928 |
| poller | 1 | 0.71 | 0.713 | 1902 | 0.12 | 0.356 | 0.003 / 7.030 | 69 | 0.036 |
| service | 21 | 18.91 | 0.900 | 308 | 0.02 | 61.322 | 0.017 / 14.723 | 5 | 0.003 |
| other | 50 | 39.43 | 0.789 | 1157 | 0.07 | 34.058 | 0.093 / 15.884 | 34 | 0.008 |

Wakers (who woke each role, and whether it then ran on the waker's CPU):

| role | waker | wakeups/s | % of role wakeups | same CPU % |
|---|---|---|---|---|
| placer | asd:placer | 2530 | 98.1 | 6.4 |
| placer | asd:poller | 48 | 1.9 | 3.7 |
| poller | swapper | 1513 | 79.7 | 100.0 |
| poller | asd:placer | 176 | 9.3 | 97.1 |
| poller | kworker | 176 | 9.3 | 96.8 |
| poller | asd:service | 13 | 0.7 | 83.6 |
| service | kvlayers | 199 | 69.5 | 34.7 |
| service | asd:poller | 31 | 10.7 | 3.9 |
| service | asd:other | 22 | 7.7 | 21.6 |
| service | asd:service | 14 | 4.9 | 0.0 |
| other | swapper | 396 | 33.9 | 99.9 |
| other | kworker | 372 | 31.9 | 84.2 |
| other | asd:poller | 328 | 28.1 | 30.7 |
| other | asd:placer | 65 | 5.6 | 86.6 |

Top 5 blocking stacks per role by total sleep time (sched.data, frame pointers; placer user frames stop in libc):

| role | sleep s/s | % of role sleep | sleeps/s | mean ms | stack |
|---|---|---|---|---|---|
| placer | 27.502 | 98.8 | 2531 | 10.865 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| placer | 0.342 | 1.2 | 55 | 6.195 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| placer | 0.006 | 0.0 | 5 | 1.297 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_pop` |
| poller | 0.666 | 98.5 | 1897 | 0.351 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller` |
| poller | 0.008 | 1.2 | 3 | 3.181 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| poller | 0.002 | 0.3 | 2 | 0.888 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: cf_queue_push <- verbs_reg` |
| service | 18.878 | 99.8 | 262 | 71.937 | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait` |
| service | 0.026 | 0.1 | 43 | 0.611 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall` |
| service | 0.002 | 0.0 | 3 | 0.760 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- verbs_reg` |
| other | 19.570 | 49.6 | 250 | 78.183 | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop` |
| other | 6.366 | 16.1 | 6 | 1001.386 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- drv_run_maintenance_loop <- mem_maint_free_pool` |
| other | 0.994 | 2.5 | 149 | 6.672 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout` |
| other | 0.994 | 2.5 | 20 | 50.537 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- mesh_tender` |
| other | 0.991 | 2.5 | 13 | 77.961 | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- timer_thr` |

