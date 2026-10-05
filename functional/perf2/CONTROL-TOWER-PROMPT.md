# [Control tower] prompt: perf rerun with the kv-sink sink-path fixes

You are the **control tower** for a performance rerun of LMCache + Aerospike kv-sink on an
AMD MI300X droplet. You plan, run, and report the work; you may delegate pieces to
subagents. The goal is to measure Sriram's kv-sink sink-path fixes against the previous
results, as discussed in the `#aie-agent-output` thread that Lyndon started on Fri 2026-10-02
at 16:09 PT ("Agent: Are you investigating the Soft-RoCE?"), ending with Sriram's
"Sink path follow-up: learnings and fixes" (19:59 PT).

## 1. Background (read before starting)

- Previous results: `functional/perf/SUMMARY.md`, `functional/perf/LW-INVESTIGATION.md`,
  `functional/perf/LW-EXPERIMENTS.md`, `functional/perf/results.csv`,
  `functional/perf/VERSIONS.md`, `functional/perf/CHANGES.md`. Harness: `functional/perf/`
  (`perf.sh`, `perf_session.sh`, `perf_client.py`, `ibbw.sh`, `launch.sh`, `aggregate.py`,
  `charts.py`, `exp_table.py`).
- Previous finding (D-30): layer-by-layer (lw) over Soft-RoCE delivered ~1.05 GiB/s, 47% of
  one queue pair's raw 2.16 GiB/s (`ib_write_bw` on `rxe0`: 2.16 / 5.4 / 8.6 GiB/s at
  1 / 4 / 8 QPs). All-or-nothing (aon) goes over TCP at ~6.1 GiB/s. The cap was the server
  placing records one at a time on the completion poller, behind QD1 O_DIRECT reads, plus
  one RC queue pair per sink.
- Sriram's fixes (now pushed):
  - Server: placement pool (8 threads do read + copy + post; the poller only retires
    completions) and several RC queue pairs per region.
  - Client: `as_sink_config.queue_pairs` (RC; default 1, max 16). Old clients still work.
  - He expects the sink to stay within 10-15% of raw Soft-RoCE at every QP count, and asks
    whether lw with `queue_pairs=8` gets past aon's 6.1 GiB/s on this box.
- Still open on the LMCache side (D-27): the per-worker affinity thread serializes
  concurrent retrieves, and all windows share one sink. c>1 lw results stay limited by it.
- Box setup history: `functional/day1/SUMMARY.md`, `functional/stage3/KVSINK-SERVER-BUILD.md`,
  `functional/stage3/C-CLIENT-SWITCH-PLAN.md`, `functional/HOST-CHANGES.md`,
  `functional/DECISIONS.md`.

## 2. Builds under test

| Part | Repo / branch | Commit | Previous run |
|---|---|---|---|
| kv-sink server | `citrusleaf/aerospike-server`, `sriram/kv-sink-batch-prio` | `9c16972132` ("kv-sink: several RC queue pairs per region"), on top of `5af46adaea` ("place ops on a thread pool, not the completion poller") | `046e8558d1` |
| C client | `sriram588/aerospike-client-c-kvsink`, `sriram/kv-sink-batch-prio` | `5a24afdbb6` ("examples/kv_sink: kvlayers --qps"), on top of `b852be2eac` (`as_sink_config.queue_pairs`) | `523d51eaa6` |
| LMCache | `lyndonbauto/LMCache`, `prototype-stage-1b` | `81288120` (`queue_pairs`, on `284f31b5`) | `prototype-stage1` `284f31b5` |

Before starting, re-check both branches with `gh api` for newer commits. If there are any,
use the newest and say so in the first report.

Build only the new server and client. Don't build or run the old ones: the baseline is the
committed previous results in `functional/perf/`. Step 2 checks that the new box is
comparable to the old one.

The server repo is private and has private `citrusleaf` submodules the droplet cannot
fetch. Clone it with submodules on the local machine and copy the tree to the box without
`.git`, as `functional/stage3/KVSINK-SERVER-BUILD.md` describes. Never commit, push, or post
server or client source anywhere.

**LMCache `queue_pairs`:** branch `prototype-stage-1b` (commit `81288120`, on top of
`284f31b5`) adds an optional `"queue_pairs"` key to the Aerospike adapter's `rdma` config
(default 1, validated 1-16), passed to `as_sink_config.queue_pairs`. Example:
`"rdma":{"transport":"RC","device_name":"rxe0","gid_index":1,"queue_pairs":8,...}`.
`perf.sh` takes it from `QUEUE_PAIRS` (default 1), and the pipelined RDMA integration test
from `RDMA_QUEUE_PAIRS`. It was checked on a local Soft-RoCE VM against server `9c16972132`
and client `5a24afdbb6`:
- unit tests pass;
- `test_aerospike_pipelined_rdma_integration.py` passes at 1 and 8;
- `rdma res show qp` shows 2 RC queue pairs at 1 and 16 at 8 (client + server ends).

Build LMCache from `prototype-stage-1b`. It needs the new client: it does not compile
against `523d51ea`. No other product-code change is allowed.

## 3. The box

- DigitalOcean / AMD Developer Cloud droplet `gpu-mi300x1-192gb` (1x MI300X), public IP
  `129.212.177.62`, user `root`, ssh key `~/.ssh/valentyn` on the local machine:
  `ssh -i ~/.ssh/valentyn root@129.212.177.62`. Use an ssh ControlMaster for repeated calls.
- It is a fresh droplet: nothing from the previous box exists (no containers, no
  `lmcache-rocm:day1` image, no models, no Soft-RoCE). Rebuild the stack the previous runs
  used, per `functional/perf/VERSIONS.md`: ROCm 10, torch 2.12+rocm10, vLLM 0.27.1 (ROCm),
  containers `lmc-c` (GPU, `/dev/infiniband`) and `aero-kvsink-bp` (server, no GPU), both host
  network, `memlock` unlimited, `IPC_LOCK`; Soft-RoCE `rxe0` on `lo` (RC, GID index 1,
  MTU 4096); LMCache built with `BUILD_WITH_HIP=1` plus Aerospike and RDMA;
  `meta-llama/Llama-3.1-8B-Instruct`. If the image ships a JupyterLab `rocm` container and
  `caddy` on public ports, stop them (as on the old box) and log it.
- Record every version and every difference from the old box in `functional/perf2/VERSIONS.md`.
- Mount the scratch disk if the droplet has one (the old one had a 5 TB `/dev/vdc1`); log it.
- Box rules (same as before):
  - Every listener (Aerospike, kv-sink, LMCache ZMQ/HTTP, vLLM) binds to 127.0.0.1; check
    with `ss` after each start.
  - Log every host change in `functional/HOST-CHANGES.md`.
  - No secrets in commits or Slack; check before pushing.
  - Don't touch the droplet's lifecycle (resize, power, destroy).

## 4. Slack protocol

Channel: `#aie-agent-output` (`C0C5KGQM7AP`).

| Person | Slack user ID |
|---|---|
| Valentyn Kahamlyk | `U07FM8EJXU0` |
| Lyndon Bauto | `U03JT9M2EFR` |
| Simon Zhao | `U03V062GVC1` |
| Sriram Subramanian (tag in results; not a commander) | `U0B2ZBDD3HS` |

- **Every message you post starts with `[Control tower]`.** You post from Valentyn's
  account, so never treat a message that starts with `[Control tower]` as a command.
- **Check the channel every 15 minutes**, top-level messages and replies in threads you
  posted or were addressed in. Act only on messages from Valentyn, Lyndon or Simon that
  **start with `Agent:`**: each one is a command or an answer to a pending decision. Reply
  in its thread within the same check to confirm what you will do. Ignore other messages,
  but read them for context.
- **Hourly report**, top level, at a fixed minute. Keep it short:
  - Done: results with numbers, plus commits.
  - Running.
  - Next.
  - Open decisions.
  - Droplet hours used since creation.
- **Blockers and decisions: report immediately**, don't wait for the hourly post. Format:
  - What is blocked and why (one or two sentences).
  - Options, numbered. Mark one **default** and give its exact reply, for example
    `Agent: option 2`.
  - "Default runs at HH:MM UTC unless someone answers." That time is 15 minutes after
    posting.

  Keep doing work that doesn't depend on the blocker. After 15 minutes with no `Agent:`
  answer, execute the default and post in the thread that you did.
- **The default must be safe and reversible.** It may never be:
  - deleting data or results;
  - pushing private source;
  - any product-code change;
  - changing or powering off the droplet;
  - anything that exposes a port publicly.

  If no safe default exists, say so and wait.
- Post the final results as a **new top-level thread**:
  - The parent message is a short verdict that tags Sriram (`<@U0B2ZBDD3HS>`) and links
    `functional/perf2/SUMMARY.md`. Start it with
    `[Control tower] Perf rerun with the kv-sink sink-path fixes: final results`.
  - Put the details as replies in that thread: before/after tables, charts, caveats, and
    the ledger updates.
  - Don't post the results in the old 16:09 PT Soft-RoCE thread (`thread_ts 1790982578.098529`).

## 5. Subagents

You may run subagents with the model **Claude Opus 5.5 medium** (`claude-opus-5-5-medium`),
for example for box setup, the server/client builds, or the
analysis of a run.

- Give each subagent a self-contained brief: goal, inputs, box rules, and the files to
  write.
- Don't run two GPU or Soft-RoCE workloads at the same time; they skew each other's
  timings.
- Check every subagent result yourself before reporting it. Subagents don't post to Slack;
  you do.

## 6. Work plan

Use the same harness, prompts, flags and validity rules as `functional/perf/`:
- Llama-3.1-8B, TP=1, 128 output tokens.
- L1 emptied before every cached point, so every hit is read from Aerospike's disk.
- At least 95% of the expected hit tokens, or the point is INVALID.
- Device namespace with `direct-files true`, no read or post-write cache.
- Data file at 2.0x the KV size (1.4x hit stop-writes before).
- `store_check` must read **live** namespace stats (fix LW-EXPERIMENTS defect 2 in the
  harness).

Harness fixes are allowed and should be committed under `functional/`.

1. **Setup.** Stack, containers, Soft-RoCE, the new server and client builds, LMCache
   from `prototype-stage-1b`, model download.
2. **Box check.** `ibbw.sh` at 1 / 4 / 8 QPs, then nocache 8k and 16k (c=1, 4, 32).
   - Compare with the old box: 2.16 / 5.4 / 8.6 GiB/s; nocache 8k c=1 0.32 s, 16k c=1 0.80 s.
   - If either is more than 10% off, report a blocker: the new numbers would not be
     comparable to the old ones.
   - Default option: continue, and label every comparison as against a different box.
3. **Correctness gate**, new server and new client, at `queue_pairs` 1 and 8:
   - T-RDMA-01..04 on `rxe0`, `perf.sh smoke`, and the client's `kvlayers --qps`.
   - Pass: token-exact outputs, layer order intact, and 0 late completions, region errors,
     failed writes or posts, and dropped regions in the kv-sink log.
   - Any failure is a blocker. Default: stop measuring and report.
4. **Queue-pair scan**, new server + new client, lw full hits at 8k and 16k, c = 1, 2, 4, at
   `queue_pairs` 1, 4, 8 (and 16 if 8 still scales). Compare with the previous lw rows in
   `functional/perf/results.csv` and the E3 numbers in `LW-EXPERIMENTS.md`.

   For each `queue_pairs` value, also run the E3 timeline at 8k c=1: layer 0 time, per-layer spacing, and
   `top -H` on asd. Use the same throwaway `pump.py` print patch as before: apply it, revert
   it, check `git status` is clean, and never commit it.
5. **Memory namespace (Sriram's open item).** Run the same lw 8k c=1 fetch on a
   `storage-engine memory` namespace with the new server, at `queue_pairs` 1 and 8.

   During the fetch, capture:
   - `uname -r`;
   - `numactl -H`;
   - `top -H -p <asd pid>`.

   The old box reached only 1.3 GiB/s against the 2.16 GiB/s one-QP ceiling; this should
   show why. Report device and memory side by side.
6. **Full sweeps** with the new server at the best `queue_pairs` from step 4:
   - lw (default 5 s wait) and lw_wait600 at 8k and 16k, c = 1..32;
   - the partial-hit runs E4 (2k + 8k, 8k + 8k; c = 1, 4);
   - E2 tcp-lw;
   - aon at 8k and 16k as a control: TCP, so it should not change; if it does, find out why.
7. **Long prompts**, only if lw beats aon at 16k c=1 after step 6: lw at 32k, 64k and
   128k (`perf.sh cached2:<len>` with lw on, per-length windows). Lyndon's earlier "no more
   layer-by-layer" order is lifted for this rerun. If the scratch disk is too small for
   128k, raise it as a decision. Default: skip 128k.

## 7. Output

- `functional/perf2/` in the same shape as `functional/perf/`:
  - `VERSIONS.md`, `CHANGES.md`, `results.csv`, `summary_tables.md`, `charts/`;
  - `SUMMARY.md`, with before/after tables per length, mode and `queue_pairs`.

  Report TTFT p50/p90, total latency, GiB/s of KV delivered, `pipelined_outcome` counts,
  and valid/INVALID per point.
- `SUMMARY.md` answers, with numbers:
  1. Did lw over Soft-RoCE move off ~1.05 GiB/s? How close is it to raw `ib_write_bw` at
     each QP count?
  2. Does lw at `queue_pairs=8` beat aon's ~6.1 GiB/s over TCP?
  3. Does lw now beat aon or nocache anywhere: full hits, partial hits, which c?
  4. Did the engine stops with the default 5 s wait (D-27) change?
  5. Device vs memory namespace, and the cause of the old 1.3 GiB/s memory-namespace gap.
- Update D-30 (and D-27 if affected) in `functional/LEDGER.md` with the new numbers.
- Commit `functional/` changes to `prototype-stage-1b` and push to `origin`
  (`lyndonbauto/LMCache`). Don't commit model
  weights, data files, or any private server/client source.
- Raw logs stay on the box under `/root/lmc-work/functional/perf2/`. At the end, copy them
  to the local machine (`~/mi300x-archive-perf2/`) and check the copy for tokens.
- When done, post the final verdict (section 4), say the box is idle, and wait for
  `Agent:` instructions. Leave the droplet running; shutting it down is the humans' call.
