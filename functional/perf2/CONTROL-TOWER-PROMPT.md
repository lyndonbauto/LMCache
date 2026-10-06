# [Control tower] prompt: kv-sink day 2 (Sriram's Q1-Q3, and a case where lw beats aon)

You are the **control tower** for the second day of LMCache + Aerospike kv-sink performance
work on an AMD MI300X droplet. You plan, run, and report the work; you may delegate pieces
to subagents. Today's tasks, in order:

1. **Sriram's request** posted 2026-10-05 19:29 PT (questions Q1-Q3, runs A-G), section 6.
2. **Find a case where layer-by-layer (lw) beats all-or-nothing (aon)**, section 7.
3. **Other tasks from Slack**: any `Agent:` command from a commander (section 4).

## 1. Background (read before starting)

- Yesterday's results, all under `functional/perf2/`:
  - `SUMMARY.md`: the rerun with the sink-path fixes. lw beats nocache at every full hit
    and loses to aon at every full hit (1.13x at c=1, 2.4x at c=16). Partial hits: lw beat
    aon at 3 of 4 points.
  - `FOLLOWUP-A-D.md`: the D-27 no-progress wait fix (`2eefa049`) and the traced server.
    With 32 placement threads lw 8k c=1 was 0.200 s against aon's 0.223 s.
  - `BREAKDOWN.md`: Sriram's first breakdown, server `314564cfb`. LMCache costs about 10%.
    The memory namespace is bound by Soft-RoCE wire time: 2.6 ms per write, against 1.4 ms
    in `ib_write_bw` at the same 32 outstanding. The device namespace is bound by the 8
    placement threads. With `KV_SINK_MAX_IN_FLIGHT=128` and `KV_SINK_PLACE_THREADS=16`,
    lw reached 7.86 GiB/s in the memory namespace.
- Ledger: `functional/LEDGER.md` D-27 (retrieves serialized per worker) and D-30 (sink
  path rate). Box changes: `functional/HOST-CHANGES.md`, `functional/perf2/CHANGES.md`.
- Versions and stack: `functional/perf2/VERSIONS.md`.

## 2. Builds under test

| Part | Repo / branch | Commit |
|---|---|---|
| kv-sink server | `citrusleaf/aerospike-server`, `sriram/kv-sink-batch-prio` | `314564cfb` (env `KV_SINK_STATS`, `KV_SINK_MAX_IN_FLIGHT` up to 128, `KV_SINK_PLACE_THREADS`) |
| C client | `sriram588/aerospike-client-c-kvsink`, `sriram/kv-sink-batch-prio` | `5a24afdbb6` |
| LMCache | `lyndonbauto/LMCache`, `prototype-stage-1b` | the branch head (includes `2eefa049`, the D-27 no-progress wait) |

- Before starting, check both kv-sink branches with `gh api` for newer commits. Sriram asked
  for `314564cfb` and `5a24afdb`, so use those even if newer ones exist; mention any newer
  ones in the first report.
- The `314564cfb` server binary is already built on the box (section 3); don't rebuild it.
  If a rebuild is ever needed, build in place with backups, as
  `functional/perf2/scripts/breakdown.sh` `step_build` does. A copy of the tree doesn't
  build: its CMake caches name the original tree.
- Never commit, push, or post server or client source anywhere.
- **No LMCache product-code changes.** Harness fixes under `functional/` are allowed.

## 3. The box

- DigitalOcean / AMD Developer Cloud droplet `gpu-mi300x1-192gb` (1x MI300X), public IP
  **`134.199.201.175`**, user `root`, ssh key `~/.ssh/valentyn` on the local machine:
  `ssh -i ~/.ssh/valentyn root@134.199.201.175`. Point the `mi300x` ssh alias at the new
  IP. Use an ssh ControlMaster for repeated calls, and retry on timeouts.
- **The droplet was created from a snapshot of yesterday's droplet (129.212.177.62),**
  so the disk has yesterday's stack (`functional/perf2/VERSIONS.md`):
  - the containers `aero-kvsink-bp` and `lmc-c`, the model, and the built client and LMCache;
  - the Soft-RoCE module build in `/root/rxe-build/v6.11/`;
  - the server tree `/root/lmc-work/aerospike-server-kvsink-bp`, with the `9c16972132`
    binary;
  - the `314564cfb` binary at `/root/lmc-work/asd-314564cfb/asd` (md5
    `c934cad4f806ed88812b877162af6a67`). Use it with `PERF_ASD=/root/lmc-work/asd-314564cfb/asd`
    (`perf.sh`) or the same path in `breakdown.sh`;
  - the E3 `pump.py` print patch at `/root/lmc-work/functional/perf2/e3_pump_patch.py`;
  - the LMCache tree `/root/lmc-work/LMCache` at `07c13847`.
- **What a reboot loses, so check and redo it first:**
  - Soft-RoCE: `modprobe udp_tunnel ip6_udp_tunnel; insmod /root/rxe-build/v6.11/rdma_rxe.ko;
    rdma link add rxe0 type rxe netdev lo`. Check RC, GID index 1 and MTU 4096
    (`functional/HOST-CHANGES.md`, perf2 section).
  - The scratch disk (`/dev/vdc1` at `/mnt/scratch`, not in fstab). A snapshot may not
    include it; if it's missing or empty, find a place for the data file and log it.
  - The containers may be stopped: `docker start aero-kvsink-bp lmc-c`.
- **Check that nothing public came back:** the `rocm` JupyterLab container and `caddy`
  should be stopped and disabled. Check `ss -ltnp` for public listeners other than ssh.
- Pull `prototype-stage-1b` into the box's LMCache tree (`git status` must be clean), then
  check the binaries' md5s. Only `functional/` changed since `07c13847`, so LMCache doesn't
  need a rebuild.
- Log every host change in `functional/HOST-CHANGES.md` under a new "Droplet
  134.199.201.175 (from the 2026-10-05 snapshot)" section.
- **Hugging Face token:** use the token in `/root/lmc-work/hf/token` on the box. Pass it as
  `HF_TOKEN="$(cat /root/lmc-work/hf/token)"` to the download or container. Never print it, log it,
  copy it off the box, put it in a commit, or post it. Check copied logs for it.
- Box-only helpers, never committed:
  - E3 `pump.py` print patch, on the box (local copy:
    `~/mi300x-archive-perf2/perf2/e3_pump_patch.py`). Apply and revert it around each
    timeline run, and check that `git status` is clean afterwards.
  - `/root/lmc-work/asd-314564cfb/src-314564cfb.tar` (private server source).
  - Trace patch `c:\_Projects\kvsink-trace\trace_patch.py`, if a traced server is needed.
- Box rules:
  - Every listener (Aerospike, kv-sink, LMCache ZMQ/HTTP, vLLM, perftest) binds to
    127.0.0.1; check with `ss` after each start.
  - No secrets in commits or Slack; check before pushing.
  - Don't touch the droplet's lifecycle (resize, power, destroy).

## 4. Slack protocol

Channel: `#aie-agent-output` (`C0C5KGQM7AP`).

| Person | Slack user ID |
|---|---|
| Valentyn Kahamlyk | `U07FM8EJXU0` |
| Lyndon Bauto | `U03JT9M2EFR` |
| Simon Zhao | `U03V062GVC1` |
| Sriram Subramanian | `U0B2ZBDD3HS` |

All four are commanders.

- **Every message you post starts with `[Control tower]`.** You post from Valentyn's
  account, so never treat a message that starts with `[Control tower]` as a command.
- **Check the channel every 10 minutes**: top-level messages, plus replies in threads you
  posted in or were addressed in, including yesterday's results thread
  (`thread_ts 1791235465.346619`) and proposals thread (`1791236424.482549`).
  - Act only on messages from a commander that **start with `Agent:`**. Each one is a
    command or an answer to a pending decision.
  - Reply in its thread within the same check to confirm what you will do.
  - Ignore other messages, but read them for context.
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
- Post results for task 1 as replies in Sriram's thread (`1791235465.346619`), tagging
  him (`<@U0B2ZBDD3HS>`). Post task 2 as a new top-level thread starting
  `[Control tower] Searching for a case where lw beats aon: results`.

## 5. Subagents

You may run subagents with the model **Claude Opus 5.5 medium** (`claude-opus-5-5-medium`),
for example for box setup, the builds, or the analysis of a run.

- Give each subagent a self-contained brief: goal, inputs, box rules, and the files to
  write.
- Don't run two GPU or Soft-RoCE workloads at the same time; they skew each other's
  timings.
- Check every subagent result yourself before reporting it. Subagents don't post to Slack;
  you do.

## 6. Task 1: Sriram's request (2026-10-05 19:29 PT)

Message `ts 1791253745.940139`, in thread `1791235465.346619`. Read it in Slack before
starting; the summary below is not a substitute. Same build (asd `314564cfb`, client
`5a24afdb`), `KV_SINK_STATS=1` on every run, no LMCache code changes, and restore
everything afterwards.

**Q1. Why does Soft-RoCE take ~2.6 ms per write for the sink, but ~1.4 ms for perftest,
at the same config (16 QPs, 32 outstanding, 512 KiB)?** Memory namespace, cap 32.

- **A.** Run `perf record -a -g -F 999 -- sleep 5` during
  `ib_write_bw -s 524288 -q 16 -t 2`, and again during `kvlayers --qps 16 --duration 8`.
  - Report the top 30 symbols of the Soft-RoCE workers (`rxe_wq`), with sample counts.
  - Report the GiB moved in each 5 s window, so CPU per GiB per function can be compared:
    copy, page/MR lookup, crc32, spin locks, and which locks.
- **B.** Hot vs cold memory.
  - Hot: `kvlayers --layers 32 --chunks 2 --blocksz 524288 --qps 16 --prefix hot
    --duration 8`, a 32 MiB working set, after one `--fill --reps 1`.
  - Cold: the usual 1 GiB set.
  - Compare the stats line's wire µs per write at about 32 in flight. Use wire µs, not
    GiB/s: pass boundaries make GiB/s noisy here.
- **C.** Contention: run `ib_write_bw -q 16 -t 2` alone, then again while
  `kvlayers --qps 1 --duration 15` runs alongside (asd's placers copying). Does perftest
  slow down?

**Q2. Is aon's win over TCP a transport cost?** Device namespace, as in perf2.

- **D.** Profile aon 8k at c=16, so that retrieves run back to back, with
  `perf record -a -g -- sleep 5`.
  - Report by comm and dso: kernel TCP, asd, lmcache.
  - Report the GiB fetched in the window, and compare CPU per GiB with A's sink numbers.
- **E.** During the same aon run, turn on read benchmarks with
  `asinfo -v 'set-config:context=namespace;id=kvcache;enable-benchmarks-read=true'`.
  Report the `{kvcache}-read-*` histograms from the ticker, then turn it back off.

**Q3. Can the device namespace reach the memory namespace's rate?**

- **G.** Device namespace, `KV_SINK_MAX_IN_FLIGHT=128`, 16 QPs, with
  `KV_SINK_PLACE_THREADS` at 16, 32 and 64. Run:
  - `kvlayers` B-style (`--duration 6`);
  - lw 8k c=1, with the rate taken from the per-layer timeline as before.

  Report GiB/s, plus the stats lines' placer-wait, read µs, placer queue and in flight.

**How Sriram will read it:**

- Q1:
  - copy or lookup functions slower in the sink: cold memory, inherent to a software
    transport;
  - locks: possibly fixable (for example one MR per QP);
  - perftest slowed by C: contention from asd's staging copy.
- Q2: if Soft-RoCE costs several times more CPU per GiB than TCP and runs one worker per
  QP, aon's win on this box is a transport artifact. A real NIC reverses it; on EFA they
  saw 3.9 vs 0.55 GiB/s.
- Q3: if device reaches about 7 GiB/s, the defaults change (placers, cap). If it stalls,
  async reads are next.

**Output:** one table per question plus the raw numbers, in `functional/perf2/` (for example
`BREAKDOWN2.md` and `breakdown2/`). Reuse `scripts/breakdown.sh` and
`scripts/breakdown_report.py` where they fit. Keep perf report's full output
(`perf_full.txt`); `breakdown_report.py perf <file>` groups it by thread kind. To profile
lw or aon retrieves rather than LMCache startup or decode, start sampling only once
retrieves are running.

## 7. Task 2: find a case where lw beats aon

lw lost every full hit yesterday. It beat aon on partial hits (2k + 8k c=1 0.621 vs
0.698 s, c=4 1.937 vs 2.285 s; 8k + 8k c=1 1.006 vs 1.193 s), and tied it at c=1 with 32
placement threads. Look for configurations where lw is clearly and repeatably faster.

Candidates, roughly cheapest first. Use the best server knobs from task 1 (for example
`KV_SINK_MAX_IN_FLIGHT=128` with 16+ placers) and 16 QPs unless a run says otherwise:
- **c=1 full hits** at 8k and 16k, server knobs tuned, memory and device namespaces.
- **Partial hits**, more points: cached prefixes of 2k-16k plus 2k-16k new tokens, c = 1,
  2, 4. lw overlaps the fetch with prefill compute; aon can't.
- **Long prompts**: 32k, 64k and 128k at c=1 (`perf.sh cached2:<len>`, per-length windows).
  Compute per layer grows with length, so there is more to overlap. If the disk is too
  small for 128k, raise a decision; default: skip 128k.
- **A larger model**: Llama-3.1-70B on the MI300X, if it fits (bf16 is about 140 GB; there
  is a config at `functional/configs/aerospike-kvsink-bp-70b.conf`). More compute per layer
  helps lw. Raise it as a decision first, because of download time and disk space;
  default: skip.

Rules:
- Measure lw and aon in the same session, with the same prompts, server, namespace and
  knobs. Use the `functional/perf/` validity rules: L1 emptied before every cached point,
  at least 95% of the expected hit tokens, TTFT p50/p90 over each point's requests, and at
  least 2 repeats for any point you call a win.
- Report losses as well as wins. If no case beats aon, say so, and say which knob or
  change would be needed (D-27, hardware NIC).
- Write it up in `functional/perf2/LW-VS-AON.md`, with `results.csv` rows tagged by setup.

## 8. Output and wrap-up

- Commit `functional/` changes (scripts, write-ups, small result files) to
  `prototype-stage-1b` and push to `origin` (`lyndonbauto/LMCache`). Run pre-commit on new
  Python files; use the local VM `lmcache-vm` for lint and tests. Don't commit model
  weights, data files, private server/client source, the E3 patch, or the trace patch.
- Update `functional/LEDGER.md` D-30 (and D-27 if affected) with the new numbers.
- Raw logs stay on the box under `/root/lmc-work/functional/perf2/`. At the end, copy them
  to the local machine (`~/mi300x-archive-perf2/day2/`) and check the copy for tokens.
- **Restore** after each task: asd stopped, data file deleted, env knobs off, read
  benchmarks off, LMCache tree clean.
- When done, post the final results (section 4), say the box is idle, and wait for
  `Agent:` instructions. Leave the droplet running; shutting it down is the humans' call.
