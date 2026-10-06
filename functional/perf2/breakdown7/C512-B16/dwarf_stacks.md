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

