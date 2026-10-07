# [Control tower] prompt: kv-sink day 3 (Sriram's idle-CPU diagnostic)

You are the **control tower** for the third day of LMCache + Aerospike kv-sink performance
work on an AMD MI300X droplet. You plan, run, and report the work; you may delegate pieces
to subagents. Today's tasks, in order:

1. **Sriram's request** posted 2026-10-06 20:19 PT (the idle-CPU diagnostic), section 6.
2. **Other tasks from Slack**: any `Agent:` command from a commander (section 4).

## 1. Background (read before starting)

- Day 2 results, all under `functional/perf2/` (rounds 1-8 for Sriram, all on Soft-RoCE,
  memory namespace, 16 QPs unless noted):
  - `BREAKDOWN2.md`-`BREAKDOWN6.md`: rxe profiles, Soft-RoCE counters and `rxe_requester`
    exits, the in-flight cap and path budget sweep (server `3b1d52fb3`: 7.52 GiB/s at the
    defaults, 8.14 at cap 512 / 16 MiB, ~75% of raw `ib_write_bw` ~10.8 GiB/s).
  - `BREAKDOWN7.md`: off-CPU by thread role. Placers slept ~1.0-1.7 ms per write on the
    per-QP post lock in `verbs_post`.
  - `BREAKDOWN8.md`, `BREAKDOWN9.md`: server `55d6ae8d8` posted without the lock. It was
    ~11% slower at every per-QP depth, so Sriram reverted it (`3f3940e42`).
  - Throughout, only ~8-11 of the box's CPUs were busy (mpstat).
- Sriram's 2026-10-06 18:15 PT note: `3f3940e42` (locked posting, defaults cap 128 /
  32 placers / 4 MiB per QP) is the baseline from now on.
- Ledger: `functional/LEDGER.md` D-27 and D-30. Box changes: `functional/HOST-CHANGES.md`,
  `functional/perf2/CHANGES.md`. Versions: `functional/perf2/VERSIONS.md`.

## 2. Builds under test

| Part | Repo / branch | Commit |
|---|---|---|
| kv-sink server | `citrusleaf/aerospike-server`, `sriram/kv-sink-batch-prio` | `3f3940e42` (revert of `55d6ae8d8`) |
| C client for `kvlayers` | `sriram588/aerospike-client-c-kvsink`, `sriram/kv-sink-batch-prio` | `e8158149` |
| LMCache | `lyndonbauto/LMCache`, `prototype-stage-1b` | the branch head (LMCache itself keeps its `5a24afdb` client) |

- **`3f3940e42`'s tree is identical to `3b1d52fb3`'s** (`git diff 3b1d52fb3 3f3940e42` is
  empty in the local clone `C:\_Projects\kvsink\aerospike-server`). Say so in the first
  report and label the binary `3f3940e42`.
- The new droplet has neither binary (section 3), so build `3f3940e42` once:
  - Make the source tar locally: `git -c core.autocrlf=false archive` of the 4 files that
    differ from `9c16972132` (`as/include/base/kv_sink.h`, `as/src/base/kv_sink.c`,
    `kv_sink_local.c`, `kv_sink_verbs.c`). Copy it to `/root/lmc-work/asd-3f3940e42/`.
  - Build in place over `9c16972132` with backups, as `scripts/breakdown8.sh`
    `step_build` does. Then restore the four files and the `9c16972132` binary (md5
    `0b953fb421486d003e28ccc90dde2d7b`). Keep the new binary at
    `/root/lmc-work/asd-3f3940e42/asd` and log its md5.
- Relink `kvlayers` against client `e8158149` in its own copy of the client tree
  (`/root/lmc-work/kvlayers-e8158149/client`), as `scripts/breakdown3.sh` `step_build`
  does; make the client tar the same way from the local client clone. The original
  `/root/lmc-work/kvlayers-build` stays untouched.
- Before starting, check both kv-sink branches with `gh api` for newer commits. Use
  `3f3940e42` and `e8158149` even if newer ones exist; mention newer ones in the first
  report.
- Never commit, push, or post server or client source anywhere (including the tars).
- **No LMCache product-code changes.** Harness fixes under `functional/` are allowed.

## 3. The box

- DigitalOcean / AMD Developer Cloud droplet (1x MI300X), public IP **`165.245.134.42`**,
  user `root`, ssh key `~/.ssh/valentyn`. The local `mi300x` ssh alias already points at
  it. Use an ssh ControlMaster for repeated calls, and retry on timeouts.
- **This droplet was created from the 2026-10-05 snapshot (day 1's end state), not from
  day 2's box.** Checked at 15:06 UTC:
  - present: the containers `aero-kvsink-bp` and `lmc-c` (both exited), the model, the
    server tree with the `9c16972132` binary, `/root/lmc-work/asd-314564cfb/asd`,
    `asd-trace/`, `kvlayers-build/`, the LMCache tree at `07c13847`, and the HF token file;
  - missing: day 2's `asd-0c9703931`, `asd-3b1d52fb3`, `asd-55d6ae8d8` and
    `kvlayers-e8158149` (section 2), and the E3 `pump.py` patch if it isn't in
    `/root/lmc-work/functional/perf2/` (local copy: `~/mi300x-archive-perf2/perf2/`);
  - not set up: Soft-RoCE is not loaded and `/mnt/scratch` is not mounted;
  - **public exposure: `caddy` is active on `*:80`** (disabled, but running).
- **First, before anything else:**
  - `systemctl stop caddy` (keep it disabled); leave the `rocm` container stopped. Check
    `ss -ltnp`: only ssh may be public.
  - Soft-RoCE: `modprobe udp_tunnel ip6_udp_tunnel; insmod /root/rxe-build/v6.11/rdma_rxe.ko;
    rdma link add rxe0 type rxe netdev lo`. Check RC, GID index 1 and MTU 4096
    (`functional/HOST-CHANGES.md`, perf2 section).
  - `mount /dev/vdc1 /mnt/scratch` (5 TB, no format, not in fstab); the data file goes in
    `/mnt/scratch/perf-aero/`.
  - `docker start aero-kvsink-bp lmc-c`.
  - Pull `prototype-stage-1b` into the box's LMCache tree (`git status` must be clean).
    The container's native extension was built at `07c13847`, and `bf69253b` has product
    commits after it that break `register_kv_cache` (`KeyError: 'dtypes'`). Either rebuild
    LMCache (`scripts/build_client_lmcache.sh`) or check out `4461293f` (product code of
    `07c13847`, current harness), as on 2026-10-07.
- Log every host change in `functional/HOST-CHANGES.md` under a new "Droplet
  165.245.134.42 (from the 2026-10-05 snapshot)" section.
- **Hugging Face token:** use the token in `/root/lmc-work/hf/token` on the box. Pass it as
  `HF_TOKEN="$(cat /root/lmc-work/hf/token)"`. Never print it, log it, copy it off the box,
  put it in a commit, or post it. Check copied logs for it.
- Box-only helpers, never committed: the E3 `pump.py` print patch (apply and revert around
  each timeline run, then check `git status` is clean), the `src-*.tar` source tars, and
  the trace patch `c:\_Projects\kvsink-trace\trace_patch.py`.
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
- **Check the channel every 10 minutes and read every new message in it: top-level
  messages and replies in every thread**, not only threads you posted in. A channel read
  returns top-level messages only, so each check does both:
  - a channel-wide search, `in:<#C0C5KGQM7AP> after:<yesterday>`, sorted by timestamp
    (newest first), paged until you reach messages you already saw. It returns thread
    replies too, with their `thread_ts`;
  - a `slack_read_thread` of every thread that has new replies, including Sriram's
    results thread (`1791235465.346619`), to see the full context.

  Track the newest message ts you've seen so the next check starts from there.
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
- Post task 1's results as replies in Sriram's thread (`1791235465.346619`), tagging him
  (`<@U0B2ZBDD3HS>`). He asked for **plain text**.
- Slack table blocks don't reach the API; if a request refers to a table you can't see,
  ask for it as text and give a default.

## 5. Subagents

You may run subagents with the model **Claude Opus 5.5 medium** (`claude-opus-5-5-medium`),
for example for box setup, the builds, or the analysis of a run.

- Give each subagent a self-contained brief: goal, inputs, box rules, and the files to
  write.
- Don't run two GPU or Soft-RoCE workloads at the same time; they skew each other's
  timings.
- Check every subagent result yourself before reporting it. Subagents don't post to Slack;
  you do.

## 6. Task 1: Sriram's idle-CPU diagnostic (2026-10-06 20:19 PT)

Message `ts 1791343152.968729`, in thread `1791235465.346619`. Read it in Slack before
starting; the summary below is not a substitute. No code changes; restore everything
afterwards.

Context: on Sriram's AWS replica of this box (x86, 20 vCPU, 1 NUMA node, kernel 6.8 with
an rxe module built from 6.11, 2 asd nodes in containers), the same server build reaches
~90% of `ib_write_bw` with ~18 of 20 CPUs busy. This box reaches ~67-72% with ~10 CPUs
busy. Find what keeps half the CPUs idle here.

**1. Confirm the setup** (from the notes, scripts and configs, or by inspecting):
- How many asd nodes ran during the `kvlayers` and lw runs in breakdowns 1-9?
- Were the quoted `kv-sink: stats` lines from one node or combined?
- asd container flags: `docker inspect` for `NanoCpus`, `CpuQuota`, `CpusetCpus`, and the
  network and IPC modes (also for `lmc-c`).

**2. Machine facts:** `lscpu`; `uname -r`; `cat /proc/cmdline`;
`modinfo -F srcversion rdma_rxe` (and where the module came from: `/root/rxe-build/v6.11/`);
`cat /sys/devices/virtual/workqueue/cpumask`; `cpupower frequency-info` and
`cpupower idle-info` (or `/sys/devices/system/cpu/cpu0/cpuidle/*/name`); total RAM.
Don't install `cpupower` without logging it; the sysfs fallback is fine.

**3. One measured run**, twice: memory namespace, server `3f3940e42` defaults
(`KV_SINK_STATS=1`), `kvlayers --qps 16 --duration 20` after one `--fill --reps 1`.
- **(a)** as usual, with vLLM and the LMCache MP server running (as in the breakdowns:
  start them the way `perf.sh` does, idle);
- **(b)** with vLLM and the LMCache MP server stopped.
- Before (a), in the same session: `ib_write_bw -s 524288 -q 16 -t 2` (E1 flags) as the
  reference.

For each run report:
- GiB/s, and 2 stats lines from each asd node;
- `mpstat -P ALL 1 10` averages per CPU, including %usr %sys %irq %soft %steal %idle;
- `top -b -n 1 -H` mid-run (top 25 threads, all processes);
- the `/proc/interrupts` delta over the run (top 10 sources and which CPUs they hit).

**How Sriram will read it:**
- High %steal: the hypervisor takes CPU time from the guest.
- (b) much faster than (a): competing load from vLLM or LMCache.
- Interrupts concentrated on the CPUs the rxe workers use: interrupt placement.
- A restricted workqueue cpumask, or isolcpus in the cmdline: kernel work confined to
  fewer CPUs.
- Deep C-states with slow exit: wake-up latency.

**Output:** plain-text tables in the thread, the write-up in `functional/perf2/BREAKDOWN10.md`,
raw output in `functional/perf2/breakdown10/`, and a script `scripts/breakdown10.sh` (reuse
`breakdown9.sh`'s server cycle and `breakdown6_report.py`'s stats-line helpers).
Keep `--duration` at 25 s or less (longer streams fail rows by design).

## 7. Output and wrap-up

- Commit `functional/` changes (scripts, write-ups, small result files) to
  `prototype-stage-1b` and push to `origin` (`lyndonbauto/LMCache`). Add only your own
  files; leave the untracked `.claude/skills/*` and `lwaon*` files out. Commit before
  `git pull --rebase`, then push. Run ruff and mypy on new Python files.
- Don't commit model weights, data files, private server/client source, the E3 patch, or
  the trace patch. Leave out large logs (`*.conf`, `asd-kvsink-bp-perf.log`,
  `lmcache_*.log`, `vllm*.log`).
- Raw logs stay on the box under `/root/lmc-work/functional/perf2/`; check every copy for
  the token.
- **Restore** after each task: asd stopped, data file deleted, env knobs off, LMCache tree
  clean, `9c16972132` binary in the server tree (md5 `0b953fb4...`), only ssh public.
- When done, post the final results (section 4), say the box is idle, and wait for
  `Agent:` instructions. Leave the droplet running; shutting it down is the humans' call.
