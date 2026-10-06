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

