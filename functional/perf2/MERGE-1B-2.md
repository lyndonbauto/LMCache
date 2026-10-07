# prototype-stage-1b-2 merged into prototype-stage-1b: tests and perf, day 3 (2026-10-07)

Valentyn's Slack command (18:32 UTC): merge Simon's `prototype-stage-1b-2` (7 commits:
G-09 won't-fix, settled RDMA windows, one native call per layer, D-27 with deferred
responses) into `prototype-stage-1b`, test it, and run the same perf grid as the D-27
A/B (`D27-AB.md`). The merge is `df919802`, made locally and **not pushed**; on the box it
came from a git bundle.

## Result

The merge is clean and its tests pass on the MI300X, but **the merged build fails layerwise
full hits at high concurrency**: 28 of 32 requests at 8k c=32, 14 of 16 at 16k c=16, and
31 of 32 at 16k c=32. `prototype-stage-1b` and `1c` had no errors at any point. Below that
(c ≤ 8) and on all partial hits, the merged build matches 1b and 1c within noise. aon is
unaffected.

Cause, from the LMCache log of those sessions:

1. 1b-2's D-27 hands layer loading to a 32-thread pool, but the adapter has 8 RDMA
   windows. With more than 8 retrieves in flight, the rest are refused ("every RDMA window
   is leased (8) or quarantined") and fall back to whole-object loads.
2. The fallbacks compete for the same server and miss the 1.5 s L1 load timeout
   ("loading 64 objects into L1 took longer than 1.5s").
3. The first failure makes the per-worker sequencer stop every in-flight retrieve of that
   worker (`RetrieveAbortedError: retrieve generation 10 was stopped by a failure`), as
   1b-2's design specifies.
4. vLLM runs with `failure_policy=fail`, so it fails the whole batch instead of
   recomputing.

`1c` doesn't hit this: its fetch pool has `pipelined_window_count()` threads, so a ninth
retrieve waits for a window instead of being refused. Possible fixes for 1b-2: cap the
pool at the window count, or have a retrieve wait for a lease instead of falling back.

## Tests (in `lmc-c`, HIP build of `df919802`)

- Build: clean (`logs/build_1b2.log` on the box).
- The 15 Python test files that 1b-2 changes: 165 passed, including the GPU tests for the
  one-call-per-layer plan (`test_layerwise_h2d_native_plan.py`) and owned block IDs.
- `tests/v1/{multiprocess,layerwise,distributed,platform}` (without the RDMA integration
  test): 2901 passed, 148 skipped, 6 failed, 19 errors. None is in code the merge touches:
  - `test_cache_server.py` (zmq) and `test_mq_register_kv_cache`: the tests expect
    register to return `None`, but it returns `RegisterKvCacheResponse` since `b795e69f`,
    which is already on `prototype-stage-1b`.
  - The gRPC variants, `test_client.py` and `test_grpc_transport.py`: the container has
    no generated `grpc_impl/_proto_gen` modules (`common_pb2`).
  - The S3 and Bigtable adapter tests: optional dependencies not installed.

## Table (p50 s; 1b2 = merged build; INVALID = failed requests)

Same server (`3f3940e42`, `KV_SINK_STATS=1`, `rdma.queue_pairs` 16) and harness
(`scripts/lwaon3.sh`) as `D27-AB.md`; one run per point, n=4 at c ≤ 4.

| cached + new | c | aon 1b | aon 1c | aon 1b2 | lw 1b | lw 1c | lw 1b2 | lw 1c / lw 1b | lw 1b2 / lw 1b | lw 1b2 / aon 1b2 |
|---|---|---|---|---|---|---|---|---|---|---|
| 8k (full) | 1 | 0.196 | 0.199 | 0.207 | 0.174 | 0.177 | 0.185 | 1.01 | 1.06 | 0.90 |
| 8k (full) | 2 | 0.236 | 0.248 | 0.235 | 0.330 | 0.332 | 0.345 | 1.01 | 1.05 | 1.47 |
| 8k (full) | 4 | 0.478 | 0.463 | 0.489 | 0.645 | 0.615 | 0.532 | 0.95 | 0.82 | 1.09 |
| 8k (full) | 8 | 0.731 | 0.805 | 0.789 | 1.294 | 1.199 | 1.258 | 0.93 | 0.97 | 1.60 |
| 8k (full) | 16 | 1.371 | 1.359 | 1.441 | 2.376 | 2.174 | 2.185 | 0.91 | 0.92 | 1.52 |
| 8k (full) | 32 | 2.457 | 2.466 | 2.669 | 4.667 | 4.241 | INVALID (28/32 failed) | 0.91 | - | - |
| 16k (full) | 1 | 0.374 | 0.398 | 0.389 | 0.332 | 0.341 | 0.323 | 1.03 | 0.97 | 0.83 |
| 16k (full) | 2 | 0.442 | 0.441 | 0.425 | 0.613 | 0.677 | 0.621 | 1.10 | 1.01 | 1.46 |
| 16k (full) | 4 | 0.979 | 1.005 | 0.929 | 1.053 | 1.272 | 1.197 | 1.21 | 1.14 | 1.29 |
| 16k (full) | 8 | 1.478 | 1.514 | 1.558 | 2.339 | 2.445 | 2.240 | 1.05 | 0.96 | 1.44 |
| 16k (full) | 16 | 2.759 | 2.608 | 2.617 | 4.586 | 4.489 | INVALID (14/16 failed) | 0.98 | - | - |
| 16k (full) | 32 | 4.987 | 4.915 | 4.803 | 9.172 | 8.194 | INVALID (31/32 failed) | 0.89 | - | - |
| 2k + 8k | 1 | 0.716 | 0.726 | 0.718 | 0.655 | 0.679 | 0.659 | 1.04 | 1.01 | 0.92 |
| 2k + 8k | 4 | 2.382 | 2.383 | 2.350 | 2.299 | 2.041 | 2.061 | 0.89 | 0.90 | 0.88 |
| 8k + 8k | 1 | 1.250 | 1.258 | 1.241 | 1.068 | 1.090 | 1.101 | 1.02 | 1.03 | 0.89 |
| 8k + 8k | 2 | 1.755 | 1.757 | 1.760 | 1.630 | 1.643 | 1.640 | 1.01 | 1.01 | 0.93 |
| 8k + 8k | 4 | 2.929 | 3.040 | 2.965 | 3.271 | 3.802 | 3.308 | 1.16 | 1.01 | 1.12 |
| 8k + 8k | 8 | 6.061 | 6.056 | 6.123 | 5.879 | 5.948 | 5.924 | 1.01 | 1.01 | 0.97 |
| 8k + 8k | 16 | 9.749 | 9.252 | 9.331 | 10.111 | 10.260 | 10.200 | 1.01 | 1.01 | 1.09 |
| 8k + 8k | 32 | 18.835 | 18.980 | 18.684 | 18.713 | 18.879 | 18.764 | 1.01 | 1.00 | 1.00 |
| 16k + 8k | 1 | 1.968 | 1.970 | 1.924 | 1.591 | 1.616 | 1.622 | 1.02 | 1.02 | 0.84 |
| 16k + 8k | 4 | 5.203 | 5.190 | 5.145 | 4.888 | 4.960 | 5.643 | 1.01 | 1.15 | 1.10 |

Where 1b2 is valid, it is within noise of 1c. The fetch path is bandwidth-bound on Soft-RoCE
(see `D27-AB.md`), so neither D-27 changes the c ≥ 2 picture against aon. The
one-call-per-layer change has no measurable TTFT effect at c=1 (8k 0.185 vs 0.174, 16k
0.323 vs 0.332, within run-to-run spread); its CPU saving wasn't measured.

Regenerate: `python functional/perf2/scripts/lwaon3_report.py functional/perf2/lwaon3
--labels 1b,1c,1b2`. Raw logs stay on the box under
`/root/lmc-work/functional/perf2/lwaon3/1b2/`.
