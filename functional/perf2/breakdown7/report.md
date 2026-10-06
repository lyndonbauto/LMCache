## C128-B4

`kvlayers` stream: 30.04 s, 221.84 GiB landed, 7.384 GiB/s, failed rows 1152

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

Top switch-out stacks by count (dwarf capture, 1 s, all states):

| role | count | % of role | state | stack |
|---|---|---|---|---|
| placer | 3269 | 32.3 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- verbs_post <- place (inlined)` |
| placer | 3078 | 30.4 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| placer | 2414 | 23.8 | R | `k: syscall_exit_to_user_mode | u: write` |
| placer | 1050 | 10.4 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_mutex_lock <- cf_queue_lock (inlined) <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| placer | 83 | 0.8 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_call_function_single <- asm_sysvec_call_function_single | u: memcpy (inlined) <- verbs_post <- place (inlined)` |
| poller | 1099 | 74.9 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller <- detached_shim_fn` |
| poller | 111 | 7.6 | R | `k: syscall_exit_to_user_mode | u: pthread_cond_signal <- cf_queue_push <- drain` |
| poller | 109 | 7.4 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_signal <- cf_queue_push <- drain` |
| poller | 44 | 3.0 | R | `k: syscall_exit_to_user_mode | u: pthread_mutex_unlock <- cf_queue_unlock (inlined) <- cf_queue_push <- drain` |
| poller | 35 | 2.4 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_mutex_lock <- cf_queue_lock (inlined) <- cf_queue_push <- drain` |
| service | 238 | 75.3 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- run_service` |
| service | 36 | 11.4 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- as_kv_sink_submit <- read_sink (inlined) <- as_read_start <- as_tsvc_process_transaction` |
| service | 20 | 6.3 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- channel_tender` |
| service | 17 | 5.4 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- drain` |
| service | 1 | 0.3 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- as_index_sprig_get_vlock <- as_index_get_vlock <- read_sink (inlined) <- as_read_start` |
| other | 566 | 44.8 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_stats <- detached_shim_fn` |
| other | 236 | 18.7 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- as_batch_worker <- run_pool_worker <- run_pool <- pool_shim_fn` |
| other | 172 | 13.6 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_retransmit <- detached_shim_fn` |
| other | 158 | 12.5 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout <- detached_shim_fn` |
| other | 20 | 1.6 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- mesh_tender <- joinable_shim_fn` |

## C512-B16

`kvlayers` stream: 30.09 s, 239.97 GiB landed, 7.976 GiB/s, failed rows 192

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

Top switch-out stacks by count (dwarf capture, 1 s, all states):

| role | count | % of role | state | stack |
|---|---|---|---|---|
| placer | 3064 | 56.5 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- verbs_post <- place (inlined)` |
| placer | 2126 | 39.2 | R | `k: syscall_exit_to_user_mode | u: write` |
| placer | 62 | 1.1 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_call_function_single <- asm_sysvec_call_function_single | u: memcpy (inlined) <- verbs_post <- place (inlined)` |
| placer | 60 | 1.1 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_apic_timer_interrupt <- asm_sysvec_apic_timer_interrupt | u: memcpy (inlined) <- verbs_post <- place (inlined)` |
| placer | 38 | 0.7 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| poller | 1001 | 94.6 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller <- detached_shim_fn` |
| poller | 12 | 1.1 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_call_function_single <- asm_sysvec_call_function_single | u: pthread_spin_lock` |
| poller | 10 | 0.9 | R | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller <- detached_shim_fn` |
| poller | 7 | 0.7 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_apic_timer_interrupt <- asm_sysvec_apic_timer_interrupt | u: pthread_spin_lock` |
| poller | 4 | 0.4 | R | `k: syscall_exit_to_user_mode | u: pthread_cond_signal <- cf_queue_push <- as_batch_buffer_complete (inlined) <- as_batch_transaction_end (inlined) <- as_batch_add_result <- send_read_response <- read_sink_done <- op_finish (inlined)` |
| service | 258 | 80.6 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- run_service` |
| service | 25 | 7.8 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- as_kv_sink_submit <- read_sink (inlined) <- as_read_start <- as_tsvc_process_transaction` |
| service | 21 | 6.6 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- channel_tender` |
| service | 6 | 1.9 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- drain` |
| service | 2 | 0.6 | R | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- run_service` |
| other | 517 | 41.9 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_stats <- detached_shim_fn` |
| other | 270 | 21.9 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- as_batch_worker <- run_pool_worker <- run_pool <- pool_shim_fn` |
| other | 150 | 12.2 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout <- detached_shim_fn` |
| other | 139 | 11.3 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_retransmit <- detached_shim_fn` |
| other | 25 | 2.0 | R | `k: syscall_exit_to_user_mode | u: sendto <- cf_socket_send_to <- cf_socket_send (inlined) <- cf_socket_send (inlined) <- do_try_send_all <- as_batch_send_buffer (inlined) <- as_batch_send_trailer (inlined) <- as_batch_buffer_end (inlined)` |

