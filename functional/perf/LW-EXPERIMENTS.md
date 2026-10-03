# Layer-by-layer (lw) investigation experiments (Lyndon's "mitigation 2")

These experiments check [LW-INVESTIGATION.md](LW-INVESTIGATION.md) (section 7) on the MI300X
box. They are not the lw sweep (which stays stopped after Lyndon's 21:05Z order). Setup as in
phase 2: Llama-3.1-8B, TP=1, vLLM 0.27.1 (TRITON_ATTN), LMCache MP server, Aerospike
kv-sink-bp on a 64 GiB data file on `/mnt/scratch/perf-aero` (2.0x sizing), lw over
Soft-RoCE `rxe0`. Harness: `ibbw.sh`, the `exp_*` / `exp2_*` sections of `perf.sh`, and
`exp_table.py` (commits c38d5bc9, 459b2e48). TTFTs are p50 of 4 requests, in seconds.

## Verdict

- **The ~1 GiB/s lw cap is the server's sink path, not Soft-RoCE.** On one queue pair (QP),
  rxe0 moves 512 KiB RDMA writes at 18.6 Gb/s (2.16 GiB/s), twice what lw gets (8.8 Gb/s,
  1.03-1.09 GiB/s). With 8 QPs, rxe reaches 73 Gb/s (8.6 GiB/s), above aon's TCP rate
  (6.1 GiB/s). The server is not CPU-bound during a fetch: no asd thread used more than
  ~15% of a core. It is latency-bound, placing one record at a time behind a QD1 disk read.
- **Layerwise itself costs nothing.** Layerwise over TCP (no `--pipelined-fetch`) equals aon
  within 0.02 s at 8k (c=1 0.205 against 0.203; c=4 0.479 against 0.502).
- **Layer 0 lands at 33-40 ms; then one layer lands every 28.6 ms.** The last layer lands at
  0.88-0.94 s, which sets TTFT on a full hit. These match the predicted ~35 ms and ~30 ms.
- **On partial hits at c=1, lw over Soft-RoCE beats aon over TCP.** At 2k cached + 8k new it
  is 0.627 s against 0.692 s (-9%); at 8k + 8k it is 1.024 s against 1.240 s (-17%). The fetch
  hides behind the prefill. At c=4, lw's serialized retrieves make it the slowest cache mode
  at 8k + 8k (4.03 s against aon's 2.95 s).
- **With a cached prefix, the new tokens' prefill is slow on this stack.** From scheduling to
  the end of the new-token store takes 0.65 s (2k + 8k) and 1.05 s (8k + 8k) in every mode.
  Recomputing the whole prompt (nocache TTFT) takes only 0.44 s and 0.80 s. So at c=1, every
  cache mode loses to nocache on partial hits; at c=4, only 8k + 8k with aon beats it.

## E1. rxe raw ceiling (`ibbw`)

perftest 6.20 in `lmc-c` (host network), on device `rxe0` with GID 1, MTU 4096 and 512 KiB
messages, with `-D 10 -F --report_gbits`. Server and client ran over loopback with
`--bind_source_ip 127.0.0.1`. `ss` confirmed that every listener was on 127.0.0.1.

| Test | QPs | TX depth | Gb/s (average) | GiB/s | Client CPU (% of 20 cores) |
|---|---|---|---|---|---|
| ib_write_bw | 1 | 8 | **18.58** | 2.16 | 5.3 |
| ib_write_bw | 1 | 128 | 18.91 | 2.20 | 5.3 |
| ib_write_bw | 4 | 8 | 46.39 | 5.40 | 6.3 |
| ib_write_bw | 4 | 128 | 45.60 | 5.31 | 6.3 |
| ib_write_bw | 8 | 8 | **73.47** | 8.55 | 8.4 |
| ib_write_bw | 8 | 128 | 72.40 | 8.43 | 8.2 |
| ib_read_bw | 1 | default | 16.17 | 1.88 | 5.4 |
| ib_read_bw | 4 | default | 42.75 | 4.98 | 6.3 |

- TX depth 8 (the server's 8 x 512 KiB in-flight budget per region) and TX depth 128 give
  the same rate, so the 4 MiB in-flight budget does not cap one QP.
- During `-q 1 -t 8`, `/proc/stat` showed about 4.5% user + 4.5% sys of 20 cores (about 1.8
  cores). `ib_write_bw` was at 100% of one core (its busy poll), and each `rxe_wq` kworker
  was at 3-4%.
- So lw's 8.8 Gb/s is 47% of the one-QP ceiling, and the remaining gap is in the server's
  sink path. Even a perfect single-QP sink path (2.2 GiB/s) stays below aon over TCP
  (6.1 GiB/s). To pass it on Soft-RoCE takes about 4 or more QPs.

## E2. Layerwise over TCP (`tcp-lw`), 8k full hit

`--use-layerwise` without `--pipelined-fetch`, same data file, same prompts as aon.

| Mode | c | TTFT p50 | TTFT mean | Outcome |
|---|---|---|---|---|
| nocache | 1 | 0.322 | 0.322 | - |
| aon | 1 | 0.203 | 0.209 | not_deferred x4 |
| tcp-lw | 1 | **0.205** | 0.211 | not_deferred x4 |
| lw (rxe, E3 run) | 1 | 0.943 | 0.954 | pipelined x4 |
| nocache | 4 | 1.425 | 1.258 | - |
| aon | 4 | 0.502 | 0.486 | not_deferred x4 |
| tcp-lw | 4 | **0.479** | 0.471 | not_deferred x4 |

As predicted, all retrieves are `not_deferred`, and TTFT matches aon. The PIECEWISE CUDA graphs
and per-layer waits add nothing measurable to TTFT. Decode is slower: total time is 1.18 s
against 0.93 s at c=1, probably because decode runs on PIECEWISE instead of full graphs.

## E3. Per-layer timeline (`timeline`), lw 8k c=1

This run used a throwaway patch on the box only. It added two `print`s in
`lmcache/v1/layerwise/pump.py`: one at `begin_fetch`, and one before each `load_layer`
(the layer is resident). The patch was reverted right after the run (`git checkout --`).
`git status` was clean afterwards, and the patch was never committed. asd's threads were
sampled with `top -H -d 0.2`.

| Retrieve | Layer 0 resident | Layer 1 | Median spacing (min-max) | Layer 31 resident | `Retrieved ... in` | TTFT |
|---|---|---|---|---|---|---|
| 1 | 39.6 ms | 69.3 ms | 29.3 ms (25.4-31.9) | 943 ms | 0.964 s | 1.013 |
| 2 | 37.1 ms | 64.5 ms | 28.8 ms (25.6-30.9) | 918 ms | 0.931 s | 0.958 |
| 3 | 32.7 ms | 61.4 ms | 27.8 ms (17.6-31.0) | 879 ms | 0.891 s | 0.919 |
| 4 | 34.5 ms | 64.2 ms | 28.6 ms (20.2-30.0) | 889 ms | 0.904 s | 0.927 |

- Times are measured from `begin_fetch`. Each layer is 32 MiB (8k tokens), so 28.6 ms per
  layer is 1.09 GiB/s, steady from the first layer to the last. There is no ramp and no tail.
- The asd threads during the fetches: the busiest thread ran at 5-15% of a core, often in
  state `D` (waiting on disk I/O). No thread was near 100%. So placement is not CPU-bound;
  each record waits on its QD1 `O_DIRECT` read before the next one is placed (server issue 6).

## E4. Partial hits (`partial`)

Prompt = stored prompt `i` of length P (the cached prefix) + 8192 new tokens. Every point
gets its own suffix (salted by session and point), so the hits are exactly the prefix
(`hit / expected` = n x P on every row). lw is over Soft-RoCE with the default 5 s wait.

| Cached + new | c | nocache | aon (TCP) | tcp-lw | lw (rxe) |
|---|---|---|---|---|---|
| 2k + 8k | 1 | **0.440** | 0.692 | 0.687 | 0.627 |
| 2k + 8k | 4 | **1.477** | 2.346 | 2.367 | 2.236 |
| 8k + 8k | 1 | **0.796** | 1.240 | 1.195 | 1.024 |
| 8k + 8k | 4 | 3.463 | **2.947** | 3.865 | 4.030 |

TTFT means at c=4: 2k + 8k: 1.557 / 2.033 / 2.045 / 1.929; 8k + 8k: 3.309 / 2.938 / 3.358 /
3.323. No engine stopped at c=4. The lw retrieves themselves took 0.24 s (2k) and 0.92-0.97 s
(8k), the same 1.05 GiB/s as on full hits.

**Where the time goes (c=1, LMCache DEBUG log).** The window runs from vLLM scheduling the
request (block allocation for 16384 / 10240 tokens) to `L1 write finished` for the store of
the 8k new tokens. The first token follows within a few tens of ms.

| Cached + new | aon | tcp-lw | lw (rxe) | nocache TTFT (whole prompt) |
|---|---|---|---|---|
| 2k + 8k | 0.65 s | 0.65 s | 0.64 s (retrieve 0.24 s inside it) | 0.44 s |
| 8k + 8k | 1.05 s | 1.02 s | 1.05 s (retrieve 0.97 s, store done 65 ms after) | 0.80 s |

- aon and tcp-lw fetch the prefix over TCP during the lookup (0.16 s for 8k), then run this
  window.
- lw starts the window at once and overlaps the fetch with it, so it saves the prefetch
  time.
- Each window covers the prefill of 8k new tokens against the cached context, plus the
  1 GiB device-to-host store copy. It is 0.2-0.25 s longer than recomputing the whole prompt.
  This run does not split it into its parts (vLLM TRITON_ATTN with context, and the store
  copy in the step).

## Theory points (LW-INVESTIGATION.md section 3) and predictions (section 4)

| # | Theory | Verdict after the experiments | Evidence |
|---|---|---|---|
| 1 | Transports differ: TCP ~6 GiB/s against Soft-RoCE ~1 GiB/s, and lw TTFT ≈ bytes / 1 GiB/s | **Confirmed, and the cap is now located: the sink path, not rxe** | tcp-lw = aon (E2). rxe gives 2.16 GiB/s on 1 QP and 8.55 GiB/s on 8 QPs (E1). Layers land at a steady 1.09 GiB/s, with no asd thread CPU-bound (E3). |
| 2 | Full hit: nothing to overlap; layer 0 early, the last layer at the end | **Confirmed and measured** | Layer 0 at 33-40 ms, then 28.6 ms per layer, layer 31 at 0.88-0.94 s, TTFT 0.92-1.01 s (E3) |
| 3 | Concurrent lw retrieves are serialized (the LMCache affinity thread) | **Confirmed again on partial hits** | 8k + 8k c=4: the 4 retrieves take 1.01 / 0.96 / 0.96 / 0.94 s and end back to back (0.95-1.00 s apart), and lw is the slowest cache mode (4.03 s) |
| §4 | lw 8k + 8k on rxe ≈ 1.04 s | **Confirmed**: 1.024 s | E4 |
| §4 | aon 8k + 8k on TCP ≈ 0.69 s (C = 0.48 s) | **Wrong**: 1.240 s | The prefill with an 8k cached context plus the store takes ~1.05 s, not 0.48 s (E4) |
| §4 | 2k + 8k: lw ties or slightly beats aon; 8k + 8k: lw loses to aon over TCP | **lw wins both at c=1** (-9%, -17%) | It wins at 8k + 8k because C is ~1 s, not 0.48 s, so the whole rxe fetch hides behind it |

## Defects and observations

1. **Harness (fixed, 459b2e48): the first partial-hit pass was invalid.** Every point reused
   one suffix per prompt index. aon and lw store what they compute, so after the first aon
   point, the later points were full hits of 10k or 16k (hits 40956 / 8192 and 65532 / 32768).
   Only aon c=1 was clean, and it matches the rerun (0.700 against 0.692; 1.232 against
   1.240). The suffix now gets a per-session, per-point salt (`--suffix-salt`). The table
   above comes from the rerun (sessions `E_<mode>2`). The first pass did give accidental
   full-hit points:

   | Full hit | c | aon | tcp-lw | lw (rxe) |
   |---|---|---|---|---|
   | 10k | 1 | - | 0.268 | 1.223 |
   | 10k | 4 | 0.602 | 0.642 | 4.874 |
   | 16k | 1 | - | 0.405 | 1.923 |
   | 16k | 4 | 0.897 | 0.954 | 7.798 |

   (lw at 16k c=4 reached a 7.8 s TTFT without an engine stop.)
2. **Harness: the 64 GiB data file filled to 70% (stop-writes) during the rerun.** The file
   was sized for the 8k prefixes. The partial points each store 8k new tokens per request,
   which added about 40 GiB. The last 10 stores (lw 8k + 8k, after the first token) failed
   with `AEROSPIKE_ERR_SERVER_FULL` (LMCache WARNING, asd `breached stop-writes limit
   (data-used-pct) ... used-pct:70`). The measured TTFTs are unaffected, because every
   retrieve read the prefixes stored earlier. `store_check` reported 7% because it reads the
   stats saved at the store step; it should read live stats.
3. The host has about 2,400 zombie (defunct) processes, which are leftover vLLM processes
   from earlier runs. They are harmless here but show up in `top`.
4. perftest is now installed in `lmc-c` (apt). No perftest process is left running.

## What this means for the options (LW-INVESTIGATION.md section 6)

- **Option 6 (server: placement off the poller, async reads, N QPs per region)** is the
  lever. rxe has headroom of 2x on one QP and 8x on 8 QPs. The server today uses 47% of
  one QP.
- **Option 7 (client: one sink per window, pipelined fetch off the affinity thread)** pays
  only together with option 6. Without it, c=4 stays serialized (E4).
- **lw already pays on partial hits at c=1 (E4).** On this ROCm stack, though, prefill with
  a cached context is slow enough that recomputing beats every cache mode at c=1. Anyone
  comparing cache modes on partial hits should include nocache.
