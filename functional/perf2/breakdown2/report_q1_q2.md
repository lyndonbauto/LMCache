## Q1 A: GiB moved in the 5 s window
- perftest: 53.7 GiB
- kvlayers: 30.1 GiB

## Q1 A: CPU by thread kind (samples ~ ms; ms per GiB)

| thread kind | perftest | ms/GiB | kvlayers | ms/GiB |
|---|---|---|---|---|
| kworker | 78640 | 1466 | 32101 | 1068 |
| ib_write_bw | 5022 | 94 | 0 | 0 |
| asd | 0 | 0 | 4467 | 149 |
| kthreadd | 453 | 8 | 882 | 29 |
| swapper | 44 | 1 | 295 | 10 |
| kvlayers | 0 | 0 | 68 | 2 |
| top | 23 | 0 | 16 | 1 |
| perf | 10 | 0 | 11 | 0 |

## Q1 A: rxe workers' top 30 symbols (by kvlayers samples)

| symbol | perftest | ms/GiB | kvlayers | ms/GiB | ratio |
|---|---|---|---|---|---|
| __raw_spin_lock_irqsave | 3552 | 66.2 | 3472 | 115.5 | 1.7x |
| memset_orig | 6002 | 111.9 | 3154 | 104.9 | 0.9x |
| __memcpy | 2379 | 44.3 | 2643 | 87.9 | 2.0x |
| crypto_shash_update | 17733 | 330.6 | 2596 | 86.4 | 0.3x |
| crc32_pclmul_le_16 | 3962 | 73.9 | 2472 | 82.2 | 1.1x |
| rdma_get_gid_attr | 5668 | 105.7 | 1804 | 60.0 | 0.6x |
| clear_page_erms | 895 | 16.7 | 1107 | 36.8 | 2.2x |
| ib_device_put | 5877 | 109.6 | 912 | 30.3 | 0.3x |
| __raw_callee_save___pv_queued_spin_unlock | 1454 | 27.1 | 889 | 29.6 | 1.1x |
| rxe_xmit_packet | 3433 | 64.0 | 860 | 28.6 | 0.4x |
| __rxe_put | 1637 | 30.5 | 646 | 21.5 | 0.7x |
| rxe_pool_get_index | 1605 | 29.9 | 624 | 20.8 | 0.7x |
| ib_device_get_by_netdev | 2407 | 44.9 | 623 | 20.7 | 0.5x |
| rxe_rcv | 1148 | 21.4 | 544 | 18.1 | 0.8x |
| rdma_put_gid_attr | 1488 | 27.7 | 514 | 17.1 | 0.6x |
| __alloc_skb | 967 | 18.0 | 507 | 16.9 | 0.9x |
| crc32_body | 707 | 13.2 | 498 | 16.6 | 1.3x |
| __ip_select_ident | 696 | 13.0 | 498 | 16.6 | 1.3x |
| ___slab_alloc | 695 | 13.0 | 457 | 15.2 | 1.2x |
| __raw_read_lock_irqsave | 1837 | 34.2 | 448 | 14.9 | 0.4x |
| rdma_read_gid_attr_ndev_rcu | 2336 | 43.5 | 373 | 12.4 | 0.3x |
| rxe_crc32 | 775 | 14.4 | 354 | 11.8 | 0.8x |
| dst_release | 1244 | 23.2 | 326 | 10.8 | 0.5x |
| rxe_receiver | 284 | 5.3 | 317 | 10.5 | 2.0x |
| _raw_spin_unlock_irqrestore | 532 | 9.9 | 308 | 10.2 | 1.0x |
| _raw_read_unlock_irqrestore | 1272 | 23.7 | 225 | 7.5 | 0.3x |
| xas_descend | 242 | 4.5 | 208 | 6.9 | 1.5x |
| __kmalloc_node_track_caller | 260 | 4.8 | 201 | 6.7 | 1.4x |
| rxe_find_route | 676 | 12.6 | 185 | 6.2 | 0.5x |
| slab_update_freelist.isra.0 | 284 | 5.3 | 167 | 5.6 | 1.0x |

## Q1 B: hot (32 MiB) vs cold (1 GiB), lines with >= 28 in flight

| run | GiB/s (stream) | lines | wire us median | wire us range | copy us | in flight |
|---|---|---|---|---|---|---|
| hot1 | 5.875 | 7 | 2340 | 2298-2398 | 47 | 31.5 |
| cold1 | 5.301 | 8 | 2679 | 2379-2821 | 42 | 32.0 |
| hot2 | 6.135 | 7 | 2243 | 2161-2331 | 45 | 31.5 |
| cold2 | 5.353 | 7 | 2637 | 2602-2728 | 40 | 32.0 |

## Q1 C: ib_write_bw -q 16 -t 2, alone vs alongside kvlayers

| run | ib_write_bw GiB/s |
|---|---|
| C_alone1 | 11.18 |
| C_with_kvl | 10.37 |
| C_alone2 | 11.11 |

kvlayers --qps 1 alongside: 0.934 GiB/s

## Q2 D: aon 8k c=16, 44.5 GiB fetched in the window

| thread kind | dso | samples | ms/GiB |
|---|---|---|---|
| asd | [kernel.kallsyms] | 26000 | 585 |
| lmcache | [kernel.kallsyms] | 16805 | 378 |
| lmcache | libc.so.6 | 10032 | 226 |
| swapper | [kernel.kallsyms] | 7515 | 169 |
| python | [kernel.kallsyms] | 4897 | 110 |
| asd | libc.so.6 | 4076 | 92 |
| python | libhsa-runtime64.so.1.18.70203 | 3758 | 85 |
| python | python3.12 | 1556 | 35 |
| lmcache | python3.12 | 1181 | 27 |
| asd | asd | 1136 | 26 |
| lmcache | libaerospike.so | 774 | 17 |
| vllm | python3.12 | 590 | 13 |
| python | libc.so.6 | 418 | 9 |
| vllm | tokenizers.abi3.so | 232 | 5 |
| vllm | libc.so.6 | 230 | 5 |

Total: 81172 samples, 1827 ms/GiB; kvlayers over the sink (Q1 A): 1259 ms/GiB
