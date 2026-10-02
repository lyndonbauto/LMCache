# Stage 3 on the new kv-sink stack (GPU)

**Outcome: every Stage 3 test reran on the batch-read stack with no wrong
token, and the `pipelined` criterion now holds.** D-12 does not reproduce on
server `046e8558d`. At cap 64, all 40 eligible Llama L2 requests were
`pipelined` (old stack: 9). gpt-oss had 26/26 within-cap L2 prompts
`pipelined` (old: 7/26). No group logged a late completion, a region error,
a failed write or a dropped region. The harness now keeps vLLM off public
interfaces (the TCPStore master was on `*:<port>`). Worker
`gpu-newstack-s3`, 2026-10-01 23:45Z to 2026-10-02 02:10Z, GPU time
23:49-02:07Z (about 2.3 h). Versions are in [`VERSIONS.md`](VERSIONS.md),
changes in [`CHANGES.md`](CHANGES.md), and scripts in [`scripts/`](scripts/)
plus [`sanity.sh`](sanity.sh). Box: `/root/lmc-work/functional/stage3-newstack/`.

Stack: LMCache `prototype-stage1` at `b6b0caae` (1a `934052cf`, merge
`e4701b9a`), rebuilt in `lmc-c` against client `523d51ea`; kv-sink server
`8.1.3.0-111-g046e8558d` in `aero-kvsink-bp` on 127.0.0.1:3700; plain path
against Aerospike CE 8.2 on 3000.

## Decisions in force

- No product fixes (Lyndon, 21:00Z): D-12 and D-17 are not fixed, because
  the code is being replaced.
- D-17 (vLLM bug vllm#49250): pipe05 and pipe06 run under `fail`. Their
  `recompute` halves are blocked by D-17.
- gpt-oss oracle: vLLM's prefix cache at block 256 decides the verdict;
  block 16 is reported alongside. gpt-oss runs at concurrency 1 (D-16).
- Old-stack results stay in the ledger. Each row's status comes from this
  run, and the old result goes in its Notes.

## Results

| Test | What | Result | Evidence (box) |
| --- | --- | --- | --- |
| Security (harness) | No vLLM/LMCache listener off loopback after any start | **pass** once the fix was in: every session's `listen_check` ok. See below | `listen/`, `*/listeners_*.txt` |
| Integration suites | `tests/v1/layerwise`, `tests/v1/distributed/rdma`, every `test_aerospike_*` IT, in `lmc-c` with the GPU visible, against 3700 | **pass**: 478 passed, 14 skipped, 0 failed (the same skips as the CPU run) | `its/pytest.txt` |
| T-E2E-01..03 (plain path, CE) | P-short-v2 twice, P-exact cold then L1 hit, restart, then L2 hit; layerwise on and off | **pass**: 100/100 exact per mode; no Aerospike traffic during the P-short-v2 warm send; L1 and L2 hits as modelled; 40/40 `not_deferred` | `e2e123/` |
| T-CFG-08 | Registration line, and a 4-chunk pure L2 hit | **pass**: line at both registrations; `pipelined`, exact; rxe0 33,280 packets against 32,768 4 KiB data packets (+1.6%) | `cfg08/` |
| T-E2E-04 | P-exact + P-ragged cold, restart, warm from L2, caps 64 and 4 | **pass** (namespace 16G): cap 64 had 40/40 eligible `pipelined` (35 short + 3 + 2 long), exact. Cap 4 had 26/26 `pipelined` and 9+3+2 over-cap `not_deferred`, all exact. The first pass on the 8G namespace lost one store per cap to `AEROSPIKE_ERR_SERVER_FULL` (P-ragged-19 missed, output exact), so the config was raised and the short groups rerun | `e2e04/` (`*_short16g` = verdict; `*_short` = 8G pass) |
| T-E2E-05 | At the cap and one chunk over | **pass** at both caps: at the cap `pipelined`, over it `not_deferred`, exact (cap 64 on the old stack: `fell_back`, D-12) | `e2e05/` |
| T-PIPE-05 | Segment 5 of chunk 1 deleted, `fail` | **partial**, as on the old stack: layer 2 never arrives mid-forward, a clean HTTP 500 twice, vLLM alive, no wrong tokens. `recompute` half blocked by D-17 | `pipe05/` |
| T-PIPE-06 | kv-sink SIGSTOP 2.5 s ×3, `fail`; after the 30 s quarantine | **partial**: stall10 missed the layer-4 deadline, `fell_back`, exact; stall11 gave a clean 500; stall12 was `refused` (quarantine), exact (marked NO only because `refused` is not in its allowed list); **after13/after14 `pipelined`**, exact (old: `fell_back` on a D-12 late completion). `recompute` half blocked by D-17 | `pipe06/` |
| T-PIPE-07 | Late write after re-lease | **partial** (E3 cannot delay RDMA alone): the first fetches after the abandoned ones were `pipelined` and exact, and nothing was credited to them | `pipe06/` |
| T-PIPE-12 | `max_record_bytes` 256 KiB vs discovery, both directions | **pass**: both reads `fell_back`, exact | `pipe12/` |
| T-PIPE-11 | gpt-oss pure L2 hit, `--separate-object-groups` | **pass**: `pipelined`, exact; staging matches the plan for 36 layers; rxe0 10,530 packets against 18,432 full and 11,520 / 13,824 limited to 1 or 2 sliding-window chunks. See the packet notes below | `pipe11/` |
| T-E2E-08 (pipelined half) | gpt-oss T-E2E-02..07 at cap 4 with Stage 2c flags, plus the L2 tail on a fresh server | **pass**: e2e08p 360/360 exact against pc256 (pc16: 352/360, the known 4 P-ragged ×2); 26/26 within-cap L2 prompts `pipelined`. The full sequence fills 16 GiB again (38 `SERVER_FULL`), so l2sh/l2mu were rerun: e2e08pl2 120/120 exact (pc256 and pc16), l2mu 30 `pipelined` + 20 `not_deferred`, l2sh 10 `not_deferred` (over the cap) | `gpt_e2e08p/`, `gpt_e2e08pl2/` |
| T-RDMA-06 GPU half | vLLM stores P-exact-00..16; byte oracle reads 100 records by RDMA and by plain get | **pass** | `rdma06gpu/` |
| D-15 probe | Clean LMCache shutdown (SIGTERM, 60 s grace) right after a pipelined fetch, twice at fetch issue, once at retrieve start | **No crash**: 4/4 TERM exits with status 143 (5-20 s), no crash lines, vLLM alive, 30/30 exact; the next send was 5/5 `pipelined`. See below | `d15/` (`*_g60` = verdict), `d15x/` |

## Security fix: vLLM on loopback (harness only)

**Cause.** vLLM's single-process executor calls `init_process_group` with
`tcp://<get_ip()>:<random port>`. torch's TCPStore master listens on every
interface whatever host it is given. With the loopback env vars set, the log
said `distributed_init_method=tcp://127.0.0.1:52257`, yet `ss` showed
`*:52257` (`VLLM::EngineCor`). `GLOO_SOCKET_IFNAME=lo` alone already moved
the gloo listeners to 127.0.0.1.

**Fix (`adf9aa5f`).**
- `harness/loopback_env.sh` is sourced by `run_steps`, `run_ref`,
  `run_lmcache` and `run_baseline`. It sets
  `VLLM_HOST_IP`/`VLLM_LOOPBACK_IP`/`MASTER_ADDR=127.0.0.1` and
  `GLOO`/`NCCL_SOCKET_IFNAME=lo`, and puts `harness/loopback_shim/` on
  `PYTHONPATH`.
- `loopback_shim/sitecustomize.py` patches
  `torch.distributed.rendezvous._create_c10d_store` when that module is
  imported. Rank 0's store for a loopback host is then created on a socket
  bound to that host and passed as `master_listen_fd`. The engine logs
  `loopback_shim: TCPStore master bound to 127.0.0.1:<port>`.
- `harness/listen_check.sh` runs after every LMCache server and vLLM start
  and fails the step if any TCP/UDP listener is off loopback.

The LMCache server's own ports (ZMQ 6555, HTTP 8080, Prometheus) were already
on 127.0.0.1. The fix took about 10 minutes.

**One remaining public listener, not ours to change:** `udp 0.0.0.0:4791`
and `[::]:4791`. This is the `rdma_rxe` kernel module's RoCEv2 socket, which
exists while rxe is loaded, whatever the harness does. `listen_check`
allows it explicitly (`udp:4791`), along with `tcp:22` for sshd. Risk is low,
because rxe only accepts packets for a device on the ingress netdev, and
`rxe0` is on `lo`. Even so, ufw's allow-incoming default lets the packets
reach the socket. Options:
1. `ufw deny 4791/udp` (a host change; needs a Slack check-in).
2. Accept it, and record that RoCEv2 packets arriving on eth0 are dropped by
   rxe (no rxe device on eth0).
3. Unload `rdma_rxe` between test sessions.

## Packet counts (T-PIPE-11, and the old ~2.2× question)

The packet size is now 4 KiB (the new client and server negotiate rxe0's
MTU of 4096). The Llama control (cfg08) measured 1.6% over the data
packets.

- **pipe11** (separate object groups, P-exact-10, 4 chunks): 10,530 packets.
  If the 18 full-attention layers read 4 chunks (9,216 packets) and the 18
  sliding-window layers read only the 128-token window (half a chunk, 1,152),
  the prediction is 10,368 × 1.016 = 10,530, an exact match. That is below
  the harness's "1 chunk per sliding layer" bound (11,520) and far below the
  full fetch (18,432).
- **The old ~2.2× excess (100,360 against 46,080 at 1 KiB) does not
  reproduce.** On the new protocol the bytes match a window-limited plan to
  within the ack overhead. The old count was most likely an artifact of the
  old server (its packet size or its whole-object `kv-sink-fetch`), which no
  longer exists. Closing that exactly would need the old server.
- **e2e08p's l03elig** (one object group; the harness calls it the
  "every layer reads every chunk" control): 217,620 packets for 62 chunks
  `pipelined`. A full fetch would be 285,696 × 1.016 = 290,250, and
  "sliding layers read only their last chunk" would be 202,752 × 1.016 =
  205,920. The measurement equals the second figure plus 5 half-chunks
  (11,520). So the new planner window-limits sliding layers even with one
  object group, and the premise of the "control" no longer holds.
  Explaining the 5 extra half-chunks needs a per-layer fetch-plan log (a
  product change, not done; HARNESS.md missing hook 3).

## D-15 on the new driver

- The 5 s grace in `stop_server` is too short for a clean shutdown. The
  usage-telemetry flush to `stats.lmcache.ai:8080` times out (a 5 s read
  timeout per message, over several threads), so a SIGTERM'd server needs
  14-17 s even on CPU with no fetch (`d15x/`: pipelined+RDMA 17.1 s, RDMA only
  15.2 s, plain 14.1 s, all exit 143). Every earlier "restart" in every stage
  therefore ended in kill -9 after 5 s. The first D-15 run (`d15/*_d15*`,
  5 s grace) recorded only kill -9 exits (137) for that reason.
- With a 60 s grace (`d15_g60`), SIGTERM at fetch issue (twice) and at
  retrieve start (once), plus one right after a pipelined send, gave 4 clean
  exits (143) after 5-20 s. The LMCache log had no crash lines (`Fatal Python
  error`, segfault, abort, double free), vLLM stayed alive, and all 30
  outputs were exact. The requests that ran while LMCache was down were
  served without it (expected). After the restart, the next send was 5/5
  `pipelined`.
- The D-15 integration test (`test_no_server_write_reaches_l1_after_close`)
  skips on the new stack, because a fetch no longer falls back, so no write is
  left outstanding at `close()`. The teardown ordering D-15 describes
  (deregister before revoking the window MR) was not checked in code here.
  Status: **not reproduced on the new driver at E3**; still open as a code
  finding.
- Observation (first run, kill -9 mid-fetch): the next send, 5 s after
  re-registration, made no LMCache lookups at all (5/5 recomputed, exact).
  That matches D-01 (requests before vLLM's next heartbeat miss). It was not
  seen after clean shutdowns.

## Defects and findings

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| D-12 | S2 | Old server's late completion | **Not reproduced on the new stack (server 046e8558).** Cap-64 runs stayed `pipelined` with no late completions; the same held for every group |
| D-15 | S2 | Teardown leaves remote RDMA access open | Not reproduced at E3 on the new driver (no crash in 4 clean shutdowns during or after fetches); its IT skips; open as a code finding |
| D-17 | S1 (vLLM) | `recompute` after a mid-forward layerwise failure gives wrong tokens | Unchanged; fault tests under `fail` |
| D-18 (new) | Info | LMCache's usage telemetry posts to `stats.lmcache.ai:8080` from the test box, and its shutdown flush makes a clean stop take about 14 s (read timeouts), past the harness's 5 s grace | Open. Options: set `LMCACHE_TRACK_USAGE=false` (or `DO_NOT_TRACK=1`) in `loopback_env.sh`, or raise the restart grace |
| D-19 (new) | Security | vLLM's TCPStore master listened on all interfaces (`*:<port>`), and ufw allows incoming | **Fixed in the harness** (`adf9aa5f`). The remaining `udp 0.0.0.0:4791` (rdma_rxe) is a host item, options above |
| Harness | Info | `aerospike-kvsink-bp.conf` had an 8G namespace (the old server's had 16G), so e2e04's short group hit `SERVER_FULL` | Fixed (`b2ea0f98`, 16G); short groups rerun |

## Harness changes

| Commit | Change |
| --- | --- |
| `adf9aa5f` | Harness: bind vLLM to loopback + public-listener check |
| `6cb99246` | `sanity.sh` (`its`, `e2e123`, `d15`); `run_steps.sh` `term` step and LMCache exit status; `stage3.sh` `STAGE_DIR`, `LW_S`, `listen_check` alert |
| `1e364379`, `a4716acb` | `term grace=<s>`; D-15 probe waits 60 s and restarts the server before waiting on the interrupted send |
| `b2ea0f98` | kv-sink-bp namespace 8G → 16G; `sanity.sh e2e04s16` |

Old-protocol-only pieces were not used: `kvsink_smoke.sh` and
`kvsink_warm.py` (`KVSINK_WARM=0`; the new server needs no warm-up), and
`stage3.sh dry` / `dryrun_server.sh` (they hard-code 3100). The per-session
kv-sink counters (late completions, region errors, failed writes and
posts, dropped regions) were read from the new server's log in each group.

## Next steps

- Decide D-18 (telemetry off in the harness, or a longer grace) before
  Stage 5, whose `kill9`/`restart` timing assumes a 5 s stop.
- Decide on `udp:4791` (options 1-3 above).
- T-PIPE-11 / e2e08p: a per-layer fetch-plan log would explain the 5 extra
  half-chunks (a product change, needs an "Agent:" ask).
- T-PIPE-07 exact, and the `recompute` halves of T-PIPE-05/06: still need a
  kv-sink per-slot delay hook, and vllm#49250 fixed in the ROCm vLLM (D-17).
