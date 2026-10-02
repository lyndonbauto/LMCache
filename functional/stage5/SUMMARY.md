# Stage 5: failure injection on the new kv-sink stack (GPU)

**Outcome: no wrong token in any Stage 5 run, but T-FLT-05 fails.**
T-FLT-01 and T-FLT-06 pass on both the plain path (Aerospike CE) and the
pipelined path (kv-sink). Plan section 7 is confirmed: an 8 s LMCache stall
stops vLLM's engine with `LayerProgressRetrieveProgressTimeoutError`. The
T-FLT-07 stand-in (kv-sink frozen 2 s) is partial. T-FLT-05 found two new
defects:
- **D-21:** a SIGKILL of the LMCache server during a layerwise retrieve can
  **wedge vLLM's engine for good**. The engine thread blocks in
  `Event.synchronize` while `/health` stays 200. This happened in 2 of the 7
  layerwise kills that landed inside a retrieve, both on the plain path.
- **D-22:** a SIGKILL during a lookup/prefetch leaves the request **waiting
  forever**, with layerwise on or off.

The other layerwise kills stopped the engine on a section-7 timeout, which
is documented behaviour. So the plan's "vLLM recomputes" expectation for
T-FLT-05 is not met with layerwise on. D-17 was never triggered: no request
was rescheduled after a failed mid-forward load.

Worker `gpu-stage5`, 2026-10-02 03:17-05:35Z, GPU time 03:20-05:13Z (about
1.9 h). Versions are in [`VERSIONS.md`](VERSIONS.md), host changes (none)
in [`CHANGES.md`](CHANGES.md), and the harness in [`stage5.sh`](stage5.sh)
and [`launch.sh`](launch.sh). Box: `/root/lmc-work/functional/stage5/`
(drivers `run1.txt`..`run4.txt`).

Stack: LMCache `prototype-stage1`, product `b6b0caae`, `lmc-c` built
against client `523d51ea`. The pipelined path uses kv-sink
`8.1.3.0-111-g046e8558d` in `aero-kvsink-bp` (127.0.0.1:3700), cap 4 (64
for flt05), with `--no-l1-use-lazy`. The plain path uses Aerospike CE 8.2
on 3000, with its set truncated before each group. Llama-3.1-8B, layerwise
on unless stated, async scheduling on, `VLLM_BATCH_INVARIANT=1`,
`STOP_GRACE=60`.

## Decisions in force

- No product fixes (Lyndon, 21:00Z). D-21 and D-22 are recorded, not fixed.
- D-17 (vllm#49250): where a recompute half would hit a failed layerwise
  load mid-forward, the test runs under `fail`. T-FLT-07 runs under `fail`
  for that reason. T-FLT-05 ran under both policies, as the plan asks. With
  layerwise on, every kill ended in an engine stop, a wedge, a hang or an
  exact completion. None ended in a rescheduled request, so D-17 never
  applied.
- T-FLT-07 uses the stand-in (kv-sink SIGSTOP 2 s). A real link-down is not
  possible on this box (see the prep notes below). T-FLT-08 is not in this
  stage.

## Results

| Test | What | Policy | Result | Evidence (box) |
| --- | --- | --- | --- | --- |
| T-FLT-01 plain | `fault_inject` (`gap_tail_ratios [0.5]`) over the CE adapter; 11 prompts (P-exact 4-chunk, P-ragged 6-16 chunks) cold, restart, warm | recompute | **pass**: warm 11/11 exact. Every warm load dropped 1 key (`FaultInject: task N dropped 1/4 load keys`), and LMCache served exactly the leading half (P-exact 512 of 1,023 hit tokens, P-ragged-16 2,048 of 4,096); vLLM recomputed the rest. Cold 11/11 exact | `flt01/*plain*` |
| T-FLT-01 rdma | The same with the RDMA adapter against kv-sink as the inner adapter | recompute | **pass**: the same numbers, 11/11 exact. `fault_inject` exposes no pipelined sink, so these loads are whole objects (`not_deferred`) | `flt01/*rdma*` |
| T-FLT-05 | SIGKILL of the LMCache server during P-exact-15's retrieve (64 chunks, L2 hit after a restart); later requests after the server is back | both | **fail** (D-21, D-22). No wrong token. Per-case table below | `flt05/` |
| T-FLT-06 plain | P-multi, 10 conversations × 5 turns, LMCache restarted between every turn | recompute | **pass**: 50/50 exact; every turn's L2 hit as modelled (`not_deferred`) | `flt06/*plain*` |
| T-FLT-06 pipe | The same on the pipelined path, cap 4 | recompute | **pass**: 50/50 exact. Turns 1-3 were 10/10 `pipelined` each (256 / 512 / 1,024 hit tokens). Turn 4 was 10/10 `not_deferred` (5 chunks, over the cap). Hits as modelled; no kv-sink errors | `flt06/*pipe*` |
| T-FLT-07 (stand-in) | kv-sink SIGSTOP 2 s at lookup end, then P-exact-10; P-exact-11/12 after the 30 s quarantine | fail (D-17) | **partial**: layer 3 missed its 1.5 s deadline (`Pipelined fetch failed on layer 3; loading 4 object(s) whole`). The request `fell_back` and was served whole once kv-sink resumed, exact, vLLM alive, and no load failure reached vLLM. After 32 s both requests were `pipelined` and exact, so the window was leased again. No log line names the quarantine. A real link-down is not possible here | `flt07/` |
| Plan section 7 (pipe) | LMCache SIGSTOP 8 s right after the lookup | recompute | **pass (confirmed)**: the engine stopped 5 s in with `LayerProgressRetrieveProgressTimeoutError ... launch ordinal 1 (watermark 0)`; the victim got HTTP 500. After the 130 s worker reap, a new vLLM on the *same* LMCache server served P-exact-11 `pipelined` and exact. The first run (run 2) restarted vLLM at once, and it could not start (D-23) | `sec7/*pipe*`, `sec7/run2_sec7_pipe/` |
| Plan section 7 (plain) | The same on the plain path | recompute | **recorded**: no stall. The victim waited out the freeze and completed exact, and vLLM stayed alive (on this path the 8 s freeze landed before any layer wait). No section-7 error | `sec7/*plain*` |
| count7 | Section-7 errors in every Stage 3 (old and new stack), Stage 4 and Stage 5 vLLM log | n/a | **pass**: 16 lines, all in deliberate faults (Stage 5 flt05 and sec7), **0 outside**. Stage 5's 8 engine stops: 5 progress timeouts at watermark 0 (flt05 ×3, sec7 ×2), 2 at watermark 11 of 32 (flt05 pipe +0.5 s), 1 generation timeout (flt05 pipe, killed before the retrieve) | `count7_final.txt` |

### T-FLT-05 cases

P-exact-15 is 64 chunks. On the pipelined path, cap 64 makes it
`pipelined`. "Kill at" is when the SIGKILL landed, relative to the LMCache
log line it waited for. `wait_log` polls every 50 ms, so the delays below
are lower bounds.

| Case (tag) | Layerwise | Policy | Kill at | What happened | Victim | Later request |
| --- | --- | --- | --- | --- | --- | --- |
| `pipe_recompute` | on | recompute | retrieve start +51 ms | Engine stopped: progress timeout, watermark 0 (section 7) | HTTP 500 | new vLLM: `pipelined`, exact |
| `pipe_fail` | on | fail | retrieve start +51 ms | Same | HTTP 500 | `pipelined`, exact |
| `pipe_fail_d05` / `pipe_recompute_d05` | on | both | retrieve start +0.5 s, mid-fetch | Engine stopped: progress timeout, watermark 11 of 32 (section 7) | HTTP 500 | `pipelined`, exact |
| `pipe_recompute_lookup` | on | recompute | lookup end (the pipe lookup takes 2 ms) | Engine stopped: **generation** timeout (the retrieve went to a dead server) | HTTP 500 | `pipelined`, exact |
| `plain_recompute` | on | recompute | retrieve start +33 ms | Engine stopped: progress timeout, watermark 0 | HTTP 500 | `not_deferred`, exact |
| `plain_fail` | on | fail | retrieve start +42 ms | **D-21: engine wedged.** The main thread sat in `torch.cuda.Event.synchronize` (`AsyncOutput.get_output`), `/health` stayed 200, the victim timed out at 300 s, and vLLM never re-registered | timeout | not sent (session stopped) |
| `plain_fail_d006_r4` | on | fail | retrieve start +94 ms, before retrieve end | **D-21 again**, same stack | timeout (150 s) | not sent |
| `plain_{fail,recompute}_d02`, `plain_fail_d003_r4`, `_d0045_r4`, `_d01_r4` | on | both | +200 / +71 / +73 / about +130 ms, after retrieve end | Retrieve already done (it takes about 65 ms) | exact | exact |
| `plain_recompute_lookup` | on | recompute | lookup start (prefetch in flight) | **D-22: the request never finished.** The engine was alive and idle; after LMCache came back, vLLM re-registered and the next request was fine | timeout (150 s) | exact |
| `plainnolw_recompute_lookup` | off | recompute | lookup start | **D-22**, same | timeout (150 s) | exact |
| `plainnolw_recompute` / `plainnolw_fail` (control) | off | both | retrieve start +23 / +5 ms | Retrieve already done (start and end logged in the same millisecond), so not mid-retrieve. vLLM alive | exact (3.2 s) | exact |

Every "exact" is token-equal to the batch-invariant baselines. No request
returned wrong tokens.

## Findings

- **D-21 (new; S2 proposed, S1 arguable): vLLM's engine can wedge for good
  when the LMCache server dies during a layerwise retrieve.** Stacks:
  `flt05/pystacks_flt05_plain_fail_engine.txt` and
  `flt05/pystacks_flt05_plain_fail_d006_r4.txt`. The engine's main thread
  waits in `torch.cuda.Event.synchronize` for the step's output. The worker
  threads are idle, and the heartbeat thread loops in `probe_server`.
  Likely cause (not confirmed in code): the dead server had already
  published some layers' launch ordinals, so the worker's `wait_for_layer_load`
  passed them and queued GPU-side waits on the server's IPC events (ROCm
  timeline semaphores, `event_ipc.md`). The server died before signalling,
  so the GPU stream waits forever, and no CPU-side timeout covers a queued
  GPU wait. The other kills landed before the first launch (watermark 0)
  or between layers (watermark 11), where the 5 s CPU wait catches them.
  Not in section 7: there is no error, `/health` stays 200, and only a
  vLLM restart recovers. It happened in 2 of the 3 plain-path kills that
  landed inside the retrieve (+42 and +94 ms; the +33 ms kill hit watermark
  0 and stopped the engine instead), and in 0 of the 4 on the pipelined path.
- **D-22 (new, S2): a lookup/prefetch in flight when the LMCache server dies
  is never failed.** On the plain path the prefetch of 64 chunks takes about
  200 ms. A SIGKILL in that window left the request in vLLM's waiting queue
  until the client gave up (150 s), with layerwise on and off. The adapter
  went to degraded mode about 14 s later, and after LMCache came back it
  re-registered and served new requests. The stuck request's lookup was
  never failed or retried. Stacks: `flt05/pystacks_flt05_*_lookup.txt`.
  The plan expects it to recompute.
- **D-23 (new, S3): after an engine stop, the LMCache server keeps the dead
  engine's KV cache mapped until it reaps the worker** (missed heartbeats,
  `worker_reap_timeout_seconds` 120 s). A vLLM restarted 10 s after the stop
  failed with `Free memory on device cuda:0 (92.94/191.69 GiB) on startup
  is less than desired GPU memory utilization (0.6, 115.01 GiB)`. After a
  130 s wait it started and was served normally. The harness now waits
  (`SEC7_REAP_WAIT`).
- **D-24 (new, Info, doc): `vllm-load-failure.md` says the connector
  swallows `LayerProgressRetrieveGenerationTimeoutError`.** The code
  (`LMCacheMPConnector.wait_for_layer_load` docstring: "Timeouts and a stale
  generation keep raising") and this run both show it stops the engine
  (`flt05_pipe_recompute_lookup`). Plan section 7 lists only progress
  timeouts and stale generations.
- **T-FLT-05 and section 7.** With layerwise on, a dead LMCache server
  always surfaces as a section-7 timeout (or D-21, D-22), never as failed
  blocks that vLLM recomputes. Section 7 documents this, but the plan's
  T-FLT-05 row expects a recompute. Meeting it needs the connector to
  report the blocks instead of raising when the daemon is known dead (a
  product change).
- **LMCache logs an ERROR at every vLLM start with layerwise + `recompute`**
  (`... needs a vLLM that rewinds requests after a failed KV load
  (vllm#49250)`). This is the D-17 guard working as intended.
- **The layerwise-off control never caught a retrieve in flight.** The MP
  retrieve start and end lines are logged in the same millisecond, so this
  harness cannot place a kill inside it. That control only shows that a
  kill right after the retrieve is harmless; the lookup-time kill (D-22) is
  its real fault.

## Defects

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| D-21 (new) | S2 proposed (S1 arguable: an engine failure not in section 7) | SIGKILL of LMCache during a layerwise retrieve can wedge vLLM's engine for good (GPU wait on a dead process's IPC event); `/health` stays 200 | Open; 2 reproductions, stacks captured |
| D-22 (new) | S2 | A lookup/prefetch in flight when LMCache dies is never failed; the request waits until the client gives up (layerwise on and off) | Open |
| D-23 (new) | S3 | After an engine stop, LMCache holds the dead engine's KV cache memory until the 120 s worker reap; an immediate vLLM restart fails on free memory | Open; harness waits 130 s |
| D-24 (new) | Info | `vllm-load-failure.md` and plan section 7 omit that a generation timeout also stops the engine | Open (doc) |
| D-17 | S1 (vLLM) | Recompute after a failed mid-forward layerwise load gives wrong tokens | Unchanged. Not triggered here: no Stage 5 fault produced a rescheduled request |
| D-18 | Info | Clean LMCache shutdown takes 14-20 s (telemetry flush) | Worked around (`STOP_GRACE=60`): every restart was a clean exit 143 |

## Harness changes

| Commit | Change |
| --- | --- |
| `fcbc9e1f` | `stage5.sh` on the new stack: plain (CE, truncated per group) and pipelined (kv-sink 3700) paths; `STOP_GRACE` 60; flt01 on CE and on the RDMA inner adapter; flt05 cases (victim at cap 64, `server_up` before `vllm_ensure`, layerwise-off control); flt07 stand-in under `fail` with a 32 s quarantine wait; count7 over every section-7 error in Stages 3-5; `launch.sh` |
| `4070f7e3` | flt05: `set -u` fix (run 1 stopped at flt05) |
| `f59ce1aa` | flt05: kill point (`lookup_start`) and delay per case, repeat suffix, 150 s victim timeout, automatic stack capture when the victim is still waiting 30 s after the kill and `/health` is ok |
| `da65e4ca` | sec7: wait 130 s for the worker reap before starting a new vLLM on the same LMCache server (D-23) |

## Next steps

- D-21: confirm the cause in code (the worker queues GPU waits on IPC events
  whose producer can die). Options: a CPU-side bounded wait on each layer's
  event before the stream wait, or a semaphore wait with a timeout. Needs an
  "Agent:" ask (no product fixes now).
- D-22: fail in-flight lookups when the heartbeat enters degraded mode, or
  add a lookup timeout (product).
- T-FLT-05's recompute expectation with layerwise on needs either a plan
  change (accept section 7) or the connector reporting failed blocks when
  the daemon is dead.
- T-FLT-07: a real link-down needs one of the options below; the stand-in
  stays partial.

## Prep notes (cpu-prep-s3s5 and cpu-prep-s4-flt07, 2026-10-01; old protocol)

The harness was first prepared and dry-run on the CPU against the old kv-sink
server (127.0.0.1:3100). The dry run checked that the `fault_inject` config
over the RDMA adapter parses and starts, that the P-multi turn ids and flt01
prompts are as intended, and that `flt07_rdma_down.sh check` refuses
because the dedicated link does not exist.

**T-FLT-07: why the real link-down is not possible here.** rxe0 is bound
to `lo` with GID 127.0.0.1, and its packets bypass `lo` qdiscs and
netfilter. Taking `lo` or rxe0 down would break every other service. Lyndon
Bauto approved a veth/netns link (mitigation 1) at 18:32 UTC. The probe
built it (netns `kvs`, veth `vkvs0`/`vkvs1`, rxe1 on the host end, rxe2
inside the netns) and tore it down; nothing is left. It does not work:
`ibv_rc_pingpong` from rxe1 to rxe2 timed out, and the netns counted 65
`UdpNoPorts`. The v6.11 rdma_rxe on this box opens its UDP 4791 socket and
does its route lookups only in the initial netns (details in
`CHANGES.md`). Options that remain, each needing a new approval:

1. **Hairpin in the host netns.** Both rxe devices in the host netns on two
   veth pairs, netns `kvs` only forwarding; move the `local` fib rule from
   pref 0 to after two `ip rule from 10.250.0.1 to 10.250.1.1` rules (and
   the reverse); `accept_local` on the two host veths. A system-wide
   routing-policy change.
2. **Newer rdma_rxe** with per-netns sockets. Loading it means `rmmod`,
   which deletes rxe0; recreate it between GPU work items. The build
   against the OFED core may need porting.
3. **Default: keep the stand-in** (used here), mark T-FLT-07 partial, and
   do the real link-down on a fabric (E5a).

Options 1 and 2 also need LMCache to see the new uverbs device (a new
container from `lmcache-rocm:day1` with `uverbs1`), and the kv-sink server
under test in a container that sees only the server-side device.
