# Stage 3 and Stage 5 harness: runbook for the GPU worker

Two host drivers run on top of `functional/harness/run_steps.sh` sessions:
`stage3/stage3.sh` for the pipelined path (functional-test-plan.md 5.4-5.6, c9
stage 3) and `stage5/stage5.sh` for the faults (5.7 and section 7). Both were
dry-run on the CPU (`stage3.sh dry`, `stage5.sh dry`, `dryrun_server.sh`); no
vLLM run has happened yet.

```bash
# On the box, from the host, with the GPU free and Stage 2c finished:
cd /root/lmc-work/functional/stage3
setsid nohup bash /root/lmc-work/LMCache/functional/stage3/stage3.sh > stage3.txt 2>&1 < /dev/null &
GPT_BASE=/work/functional/stage2/gptoss_ref/<run>.json bash .../stage3.sh pipe11   # needs the Stage 2c oracle
cd /root/lmc-work/functional/stage5 && setsid nohup bash .../stage5/stage5.sh > stage5.txt 2>&1 < /dev/null &
```

Progress goes to `<stage>/progress.log`, one directory per section with the
`run_steps.sh` outputs, the vLLM and LMCache logs, `report_*.md`, and the
kv-sink log of every group.

## Setup every session gets

- kv-sink server (`aero-kvsink`, 512b0c207, fencing) restarted before every
  group and warmed with `kvsink_smoke.sh`. On a cold server, 2 of the 3
  pipelined smoke tests passing is the expected result (issue 5). If fewer
  pass, which happens when the smoke's pytest crashes (S3 in SUMMARY.md), the
  warm-up is retried once. The restart
  also clears the region leak from each `kill9` (issue 9), and a group never
  stores more than about 6 GiB.
- LMCache: `--use-layerwise --pipelined-fetch --pipelined-max-chunks <cap>`,
  with `--l2-adapter` aerospike `127.0.0.1:3100` and RDMA `{"transport":"RC",
  "device_name":"rxe0","gid_index":1,"window_count":2,"window_bytes":cap*chunk}`.
  vLLM runs with `VLLM_BATCH_INVARIANT=1`, so outputs can be compared with the
  Stage 2b oracles `BASE1`/`BASE2` (Llama) or `GPT_BASE` (gpt-oss).
- D-12 decision: e2e04 and e2e05 run once per value in `CAPS` (default
  `"64 4"`). Rows from the cap-64 run that cover prompts over 4 chunks are
  expected to fall back (D-12). Report those as "blocked by D-12", not as a
  pass or a fail.
- Policy: the fault sections use `kv_load_failure_policy` recompute; every
  other section uses fail.
- `hit_report.py` checks the outcomes. `--require TAG=pipelined` checks that
  every modelled hit in that send has `pipelined_outcome=pipelined`.
- Every pipelined row is "pass on a fencing server" and is rerun when a
  no-fence build lands.

## Sections

| Section | Tests | Fault / method | Pass criterion |
|---|---|---|---|
| stage3 cfg08 | c9 stage 3, T-CFG-08 | none | registration and staging lines present; the 4-chunk pure L2 hit is `pipelined` and equal to the oracle |
| stage3 e2e04 | T-E2E-04 | none; P-exact + P-ragged cold, restart, warm | outputs equal; prompts within the cap `pipelined`, prompts over it `not_deferred` |
| stage3 e2e05 | T-E2E-05 (pipelined half) | none; at the cap and one chunk over | at the cap: `pipelined`; over: plain path, equal |
| stage3 pipe05 | T-PIPE-05 | `l2seg` deletes segment 5 of chunk 1 of P-exact-10 (meta kept) | `fell_back`, output equal, no error |
| stage3 pipe06 | T-PIPE-06, T-PIPE-07 (partial) | kv-sink SIGSTOP 2.5 s, armed on `MP lookup/prefetch end:`, 3 times; then after13/after14 | stalled requests recompute (equal); later fetches `pipelined` (window leased again) |
| stage3 pipe12 | T-PIPE-12 | stored with `max_record_bytes` 262144, read with discovery (1 MiB), and the reverse | under fail: a clean fallback or error, never wrong output |
| stage3 pipe11 | T-PIPE-11 | gpt-oss pure L2 hit, `--separate-object-groups` | rxe0 data packets match the sliding-window-limited size (11520 or 13824), not the full size (18432) |
| stage3 rdma06gpu | T-RDMA-06 GPU half | vLLM stores P-exact-00..16; the byte-oracle pytest reads them back (`RDMA_ORACLE_STORED_SET`) | 100/100 records equal, RDMA vs plain get |
| stage5 flt01 | T-FLT-01 | `fault_inject` `gap_tail_ratios [0.5]` wrapping the Aerospike adapter (pipelining is off: the wrapper does not forward it) | leading run served, the rest recomputed, outputs equal |
| stage5 flt05 | T-FLT-05 | `kill9` of LMCache after `MP retrieve start:`, under recompute and fail | vLLM alive; recompute: equal; fail: a clean error; requests succeed after `server_up` |
| stage5 flt06 | T-FLT-06 | P-multi, LMCache restart between turns | turns 1-3 `pipelined` from L2, all outputs equal |
| stage5 flt07 | T-FLT-07 | stand-in: kv-sink frozen 2 s on lookup (`FLT07_MODE=link` once the dedicated link exists) | stalled request recomputes; the next two requests `pipelined` |
| stage5 sec7 | section 7 | LMCache SIGSTOP 8 s after lookup (worker wait 5 s) | vLLM stops with `LayerProgressRetrieveGenerationTimeoutError`; after vllm_ensure, requests succeed |
| stage5 count7 | section 7 | grep every Stage 3/5 vLLM log | the error appears only in sec7; anywhere else is S1 |

## Fault hooks: what exists and what is missing

- **E0 fakes** (`ScriptedLayerArrivalSource`, `UnservableLayerArrivalSource`,
  `RecordingLayerLoadSink` in `lmcache/v1/layerwise/fakes.py`). These are
  constructor-injected test doubles. No flag or environment variable installs
  them in `lmcache server`, so no E3 test can use them.
- **The `fault_inject` L2 adapter** works in a real server, but it does not
  forward the pipelined methods. Wrapping the RDMA adapter therefore turns
  pipelining off. It is usable for T-FLT-01 (plain path) only.
- **What the E3 faults are built from instead:**
  - record deletion (`harness/l2_segments.py`);
  - host SIGSTOP of kv-sink, armed on a log line (`harness/host_actions.sh`);
  - SIGSTOP or SIGKILL of LMCache (`run_steps.sh freeze_server`/`kill9`);
  - a mismatched `max_record_bytes`.
- **Missing hooks** (product proposals, not done here):
  1. **T-PIPE-07 cannot be done exactly at E3.** A late write that lands
     *after* the window is leased again needs RDMA writes delayed while TCP
     lookups continue. Freezing a process stalls both. Network tools cannot
     reach the RDMA path either: rxe0 sits on `lo` with GID 127.0.0.1, so its
     traffic skips `lo` qdiscs and netfilter. Each T-RDMA-06 run moved 6.72 GB
     over `lo`, all TCP; the 3.36 GB of RDMA writes never appeared there.
     Needed: a kv-sink debug delay per slot, or a client hook.
  2. **No visibility of stale-generation immediates.** They are dropped
     silently in C++ (`ArrivalStatus::kStaleGeneration`). Needed: a counter or
     metric, so T-PIPE-07 can check that a late write was seen and discarded.
  3. **No runtime log of the per-layer fetch plan** (slots and bytes per
     layer). T-PIPE-11 uses the rxe0 packet counters
     (`rdma statistic show link rxe0/1`) instead.
  4. **No way to drop a single layer's slots.** T-PIPE-06 freezes the whole
     server, so every layer stalls together.
- **T-FLT-07 (RDMA path down only).** This needs its own link: a veth pair
  into a separate netns, with new rxe devices on it. That is a host change
  needing approval; `stage5/flt07_rdma_down.sh` documents it and refuses to
  touch `lo`, rxe0 or the default-route device.

## Dry-run results (CPU, 2026-10-01)

- **Server configs.** All five `lmcache server` configurations start on the
  CPU with `rdma=RC` against kv-sink and log no errors:
  - llama cap 64;
  - llama cap 4;
  - llama cap 4 with 256 KiB records;
  - gpt-oss cap 4;
  - fault_inject over RDMA.
- **Armed freeze.** It stopped kv-sink (state T) 0.3 s after the trigger line,
  and kv-sink answered again afterwards.
- **Report parser.** `hit_report.py` reproduces the committed Stage 2b reports
  byte for byte.
- **Not exercised without vLLM:**
  - the `send`, `vllm_check` and `l2seg` steps;
  - the rxe packet thresholds;
  - the LMCache freeze timing of sec7.
