Top switch-out stacks by count (dwarf capture, 1 s, all states):

| role | count | % of role | state | stack |
|---|---|---|---|---|
| placer | 3127 | 63.1 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| placer | 919 | 18.5 | R | `k: syscall_exit_to_user_mode | u: write` |
| placer | 553 | 11.2 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_mutex_lock <- cf_queue_lock (inlined) <- cf_queue_pop <- run_placer <- detached_shim_fn` |
| placer | 137 | 2.8 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_call_function_single <- asm_sysvec_call_function_single | u: memcpy (inlined) <- verbs_post <- place (inlined)` |
| placer | 102 | 2.1 | R | `k: irqentry_exit_to_user_mode <- irqentry_exit <- sysvec_apic_timer_interrupt <- asm_sysvec_apic_timer_interrupt | u: memcpy (inlined) <- verbs_post <- place (inlined)` |
| poller | 315 | 46.7 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_poller <- detached_shim_fn` |
| poller | 116 | 17.2 | R | `k: syscall_exit_to_user_mode | u: pthread_cond_signal <- cf_queue_push <- drain` |
| poller | 92 | 13.6 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_signal <- cf_queue_push <- drain` |
| poller | 61 | 9.0 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_mutex_lock <- cf_queue_lock (inlined) <- cf_queue_push <- drain` |
| poller | 58 | 8.6 | R | `k: syscall_exit_to_user_mode | u: pthread_mutex_unlock <- cf_queue_unlock (inlined) <- cf_queue_push <- drain` |
| service | 235 | 57.7 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- run_service` |
| service | 91 | 22.4 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- as_kv_sink_submit <- read_sink (inlined) <- as_read_start <- as_tsvc_process_transaction` |
| service | 47 | 11.5 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: syscall <- sys_futex (inlined) <- cf_mutex_lock <- cf_mutex_lock <- drain` |
| service | 21 | 5.2 | S | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- channel_tender` |
| service | 3 | 0.7 | R | `k: schedule_hrtimeout_range_clock <- schedule_hrtimeout_range <- ep_poll <- do_epoll_wait <- __x64_sys_epoll_wait | u: epoll_wait <- cf_poll_wait <- run_service` |
| other | 615 | 46.9 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_stats <- detached_shim_fn` |
| other | 239 | 18.2 | S | `k: futex_wait_queue <- __futex_wait <- futex_wait <- do_futex <- __x64_sys_futex | u: pthread_cond_wait <- cf_queue_pop <- as_batch_worker <- run_pool_worker <- run_pool <- pool_shim_fn` |
| other | 161 | 12.3 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_proxy_timeout <- detached_shim_fn` |
| other | 159 | 12.1 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- run_retransmit <- detached_shim_fn` |
| other | 21 | 1.6 | S | `k: do_nanosleep <- hrtimer_nanosleep <- common_nsleep <- __x64_sys_clock_nanosleep | u: clock_nanosleep <- __nanosleep <- usleep <- hb_adjacency_tender <- joinable_shim_fn` |

