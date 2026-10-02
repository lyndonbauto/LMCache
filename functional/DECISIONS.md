# Functional test campaign: decisions

Every decision taken during the LMCache + Aerospike functional campaign
(2026-09-30 to 2026-10-02): the options on the table, what was chosen, and
who chose it.

- **Human** means a named person chose it in #aie-agent-output.
- **Default** means the control tower posted a default with options, nobody
  chose otherwise, and Lyndon Bauto asked on 2026-10-02 (16:53Z) for the
  decisions to be recorded as final. Any of them can still be reopened.

Results are in [`EXIT-REVIEW.md`](EXIT-REVIEW.md) and [`LEDGER.md`](LEDGER.md);
host changes are in [`HOST-CHANGES.md`](HOST-CHANGES.md).

## Scope and direction

| # | Decision | Options | Chosen | By |
| --- | --- | --- | --- | --- |
| 1 | Product fixes during testing | Fix S1/S2 defects as found; or skip fixes and keep testing | Skip fixes; continue every test that is meaningful without them, because the RDMA server code and the LMCache client are being replaced (2026-10-01 21:00Z) | Human (Lyndon) |
| 2 | New stack | Stay on the old kv-sink server and client; or switch now and retest | Switch now to LMCache `prototype-stage-1a` (934052cf), server `sriram/kv-sink-batch-prio` (046e8558), C client 523d51ea, and retest every relevant test (22:00Z) | Human (Lyndon) |
| 3 | Smaller gaps | Raise each in Slack; or collect them in one file | Collect them in [`OPEN-GAPS.md`](OPEN-GAPS.md), linked from the ledger (18:32Z) | Human (Lyndon) |

## Security and host

| # | Decision | Options | Chosen | By |
| --- | --- | --- | --- | --- |
| 4 | JupyterLab on public 0.0.0.0:8888 (`rocm` container, `caddy` proxy on 80) | (1) stop and remove it; (2) bind to loopback; (3) leave | (1): stopped `rocm` and disabled `caddy`; image kept, undo in `HOST-CHANGES.md` | Human (Lyndon) |
| 5 | vLLM engine listener on `*` (D-19) | (1) bind every vLLM start to loopback in the harness and check with `ss`; (2) option 1 plus a ufw or cloud-firewall deny rule (host change); (3) leave | (1): loopback binding plus a public-listener check on every start ("fix it if it is not going to delay testing much", 23:00Z) | Human (Lyndon) |
| 6 | UDP 4791 public (rdma_rxe) | (1) accept: needed for Soft-RoCE, and the box has no peers; (2) firewall it, which is a ufw change | (1) accept, unchanged | Default |
| 7 | LMCache usage telemetry to `stats.lmcache.ai` (D-18; also makes a clean stop take 14-17 s) | (1) leave on and wait 60 s on stop; (2) turn off in the harness (`LMCACHE_TRACK_USAGE=false`) | (1) leave on | Default |

## Test method and oracle

| # | Decision | Options | Chosen | By |
| --- | --- | --- | --- | --- |
| 8 | T-E2E-01 "zero L2 traffic": prompts of 209+ tokens plus 48 generated tokens fill a chunk, which LMCache stores | (1) amend the pass rule to allow writes of chunks completed by generated tokens; (2) rebuild P-short at 208 tokens or fewer, with a new baseline, and rerun; (3) keep P-short and cap `max_tokens` per prompt | (2); passes on P-short-v2 | Human (Lyndon) |
| 9 | Stage 3 on the old server, before a no-fence build existed | (1) accept fencing for this round; (2) hold Stage 3 until a no-fence build exists; (3) run now on the fencing build, mark the pipelined rows "pass on a fencing server", rerun later | (3); later superseded by the new-stack retest | Human (Lyndon) |
| 10 | gpt-oss prefix-hit oracle | Compare with vLLM's own prefix cache at block 16; or at block 256 | Block 256 decides, block 16 also reported | Default |
| 11 | T-E2E-09 (concurrency) model | Both models; or Llama only (gpt-oss is not batch-invariant across batch sizes even without LMCache) | Llama only | Default |
| 12 | T-E2E-09 verdict mode | `recompute` or `wait` shared-keys mode | `wait` | Default |
| 13 | D-12 (old server: late completions at large batch) | Four mitigations were posted (thread of 16:57Z) | Mitigations 2 and 4: record D-12 at the default cap, and run long prompts once more at cap 4. D-12 does not reproduce on the new server, so new-stack runs use the default cap 64, including the 70B | Default, then Human (no fixes, decision 1) |

## Defects

| # | Decision | Options | Chosen | By |
| --- | --- | --- | --- | --- |
| 14 | D-14: two writers mixing segments of one object | (1) per-write segment keys, last writer wins; (2) option 1 plus create-only metadata (first writer wins, loser deletes its own segments); (3) accept and document ([design record](../docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md)) | (2), fixed in e9cd0689; T-FLT-10 passes | Human (Lyndon, 18:56Z) |
| 15 | D-14 on the pipelined path | (1) one extra batch read of write IDs; (2) carry the write ID from lookup; (3) defer | (1) | Human (Lyndon, 19:43Z) |
| 16 | D-17: vLLM gives wrong tokens when it recomputes after a failed layer load (vllm#49250) | (1) run fault tests with `fail` and record each `recompute` half as blocked by D-17; (2) test on a patched scratch vLLM | (1); no fix (decision 1) | Default, after Human (decision 1) |
| 17 | D-20: concurrent same-prefix lookups miss | Fix; or record only (S3, outputs exact) | Record only | Default |
| 18 | D-21: LMCache killed mid layer-by-layer fetch can freeze vLLM for good | Severity S2, record only; or S1, fix before exit | S2, record only | Default |
| 19 | D-22: lookup in flight when LMCache dies never finishes | Fix; or record only | Record only (decision 1) | Default |
| 20 | D-25: a full L1 refuses a whole 32-chunk store | Record only; or fix (evict on demand, or grant part of a batch) | Record only | Default |
| 21 | D-26: Aerospike CE write-cache overload drops a whole 70B store task | (1) record only; (2) LMCache retry with backoff, or smaller store tasks; (3) raise `max-write-cache` for good (host config change) | (1); the 70B test raised it to 8 GiB for its session only | Default |

## Test status calls

| # | Decision | Options | Chosen | By |
| --- | --- | --- | --- | --- |
| 22 | T-FLT-07 "link down 2 s" | (1) veth pair plus network namespace with its own rxe device (host change); (3) stand-in: freeze kv-sink for 2 s, mark partial | (1) chosen by Lyndon but did not work (rdma_rxe binds only in the initial namespace); fell back to (3), partial | Human, then Default |
| 23 | T-FLT-08 | (1) RoCE-only netem on an idle box; (2) all of loopback; (3) N/A on this box | (3) N/A | Default |
| 24 | T-FLT-05 expectation | Keep "fail"; or amend the plan to accept vLLM stopping itself on a section-7 timeout | Keep fail | Default |
| 25 | T-EVT-04 status | Partial until D-17 is fixed; or pass under `fail` | Partial | Default |
| 26 | T-FLT-04 kv-sink expectation | "Falls back" (plan); or "stays pipelined" (the new client re-registers inside the fetch) | Note "stays pipelined" in the ledger; verdict pass | Default |
| 27 | T-E2E-11 "three kv-sink processes" note | Run three processes; or one large node | One 64 GiB node. Three processes form a cluster, and LMCache refuses pipelined plans on a multi-node cluster | Default |

## Left to the owners

- The droplet keeps billing while idle (about 66.6 of 150 h used at
  2026-10-02 16:45Z). Keeping or destroying it is a human call; the campaign
  never touched it.
