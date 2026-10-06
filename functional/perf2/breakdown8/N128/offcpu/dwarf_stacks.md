Top switch-out stacks by count (dwarf capture, 1 s, all states):

| role | count | % of role | state | stack |
|---|---|---|---|---|
| placer | 6964 | 73.1 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| placer | 1065 | 11.2 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_mutex_lock <- cf_queue_lock (inlined) <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| placer | 1060 | 11.1 | R | `k: syscall_exit_to_user_mode | u: write` |
| placer | 189 | 2.0 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_call_function_single <- asm_sysvec_call_function_single | u: memcpy (inlined) <- verbs_post <- place (inlined)` |
| placer | 76 | 0.8 | R | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| poller | 214 | 25.8 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller <- detached_shim_fn` |
| poller | 212 | 25.5 | R | `k: syscall_exit_to_user_mode | u: pthread_cond_signal <- cf_queue_push <- drain` |
| poller | 191 | 23.0 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_signal <- cf_queue_push <- drain` |
| poller | 97 | 11.7 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_mutex_lock <- cf_queue_lock (inlined) <- cf_queue_push <- drain` |
| poller | 76 | 9.2 | R | `k: syscall_exit_to_user_mode | u: pthread_mutex_unlock <- cf_queue_unlock (inlined) <- cf_queue_push <- drain` |
| service | 221 | 78.6 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- run_service` |
| service | 27 | 9.6 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- as_kv_sink_submit <- read_sink (inlined) <- as_read_start <- as_tsvc_process_transaction` |
| service | 20 | 7.1 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- channel_tender` |
| service | 9 | 3.2 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- drain` |
| service | 2 | 0.7 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_call_function_single <- asm_sysvec_call_function_single | u: cf_mutex_lock <- as_partition_reserve_read_tr <- as_tsvc_process_transaction <- as_batch_queue_task <- start_transaction <- run_service` |
| other | 595 | 46.3 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_stats <- detached_shim_fn` |
| other | 219 | 17.0 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- as_batch_worker <- run_pool_worker <- run_pool <- pool_shim_fn` |
| other | 176 | 13.7 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout <- detached_shim_fn` |
| other | 175 | 13.6 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_retransmit <- detached_shim_fn` |
| other | 21 | 1.6 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- mesh_tender <- joinable_shim_fn` |

