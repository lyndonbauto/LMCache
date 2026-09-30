# Functional test plan: Aerospike KV cache tier

The first round of testing for LMCache with the Aerospike L2 adapter, the
RDMA data path and the pipelined (layer-by-layer) retrieve. It answers one
question: **does it do the right thing, every time, including when things
fail?** Speed is out of scope here; the benchmark plan comes after this one
passes.

Related: [exercise-goal.md](exercise-goal.md),
[system-design.md](system-design.md), [c9-bring-up.md](c9-bring-up.md),
[vllm-load-failure.md](vllm-load-failure.md),
[`aerospike_rdma.md`](../distributed/l2_adapters/aerospike_rdma.md).

## 1. What "pass" means

Three rules apply to every test in this plan:

1. **Output never changes.** With greedy sampling, a completion served from
   the cache is token-identical to the same prompt with no LMCache. A
   mismatch is a stop-the-line defect, whatever else passes.
2. **Failure degrades to recompute.** Any fault (missing record, dead node,
   declined slot, full window) ends in a correct completion, served more
   slowly, with the engine still running. The one documented exception is
   in section 7.
3. **Every retrieve says how it was served.** `pipelined_outcome` on
   `MP_RETRIEVE_END` and the `lmcache_mp_num_deferred_retrieves_total`
   counter must match what the test set up. A test that expects
   `pipelined` and gets `fell_back` has failed, even if the output is right.

### Severity

| Severity | Meaning | Example |
|---|---|---|
| S1 | Wrong output, data from another request or tenant, engine crash not in section 7, memory corruption | Completion differs from baseline; vLLM dies on a declined slot |
| S2 | Cache silently not used when it should be, or a fault leaves state that needs a restart | `not_deferred` on an L2 hit; a window never leased again |
| S3 | Wrong or missing log, metric or error message | `fell_back` logged without the layer number |

The functional phase exits with **zero open S1 and S2 defects**.

## 2. The correctness oracle

Token equality is only a fair oracle if the baseline is deterministic.

- **Baseline:** the same vLLM build, same model, `temperature=0`, fixed
  seed, `--no-enable-prefix-caching`, **no** `--kv-transfer-config`.
- **Batch size 1 for equality tests.** GPU kernels are not guaranteed
  bit-identical across batch compositions, so a concurrent run can differ
  from the baseline with no LMCache at all. If the vLLM build offers
  batch-invariant kernels, turn them on and concurrent tests can use token
  equality too.
- **Concurrent tests without batch invariance** use a byte oracle instead:
  the KV bytes each request received must equal the bytes stored for its
  keys (T-E2E-09), plus a top-1 logprob agreement check against baseline.
- **Byte oracle for the transport.** For any key, the object delivered by
  RDMA into L1 is byte-identical to the same key read through the non-RDMA
  Aerospike path. This already exists at protocol level
  (`test_rdma_equivalence.py`); T-RDMA-06 extends it to production keys.

### Prompt corpus

A fixed, versioned set, so failures reproduce:

| Set | Content | Why |
|---|---|---|
| P-short | 20 prompts under one chunk | Nothing cacheable; must never touch L2 |
| P-exact | 20 prompts at exactly 1, 2, 4 and max chunks | Chunk-boundary off-by-one |
| P-ragged | 20 prompts at N chunks + 1 to N chunks + (chunk − 1) tokens | Partial last chunk is not stored; hit covers only full chunks |
| P-long | 10 prompts at `--pipelined-max-chunks` and one chunk over | The cap, and the refusal just past it |
| P-shared | 10 prompts sharing a long system prompt, differing in the tail | Prefix reuse across requests |
| P-multi | 10 scripted 5-turn conversations | Growing prefix, the multi-turn case |
| P-salt | P-shared sent under two `cache_salt` values | Tenant isolation |

## 3. Environments

Each environment adds one moving part, so a failure points at it. Tests name
the lowest environment they can run in.

| Env | What it is | Needed for |
|---|---|---|
| E0 | Any Linux box, CPU only, no fabric | All unit and conformance suites (runs in CI) |
| E1 | E0 + Soft-RoCE (`rxe0` on `lo`, GID index 1) + mock `kv-sink` writer | RDMA data path against our own mock |
| E2 | E1 + Aerospike server (CE for the plain path, the `kv-sink` server branch for RDMA) | Storage behaviour, real server replies |
| E3 | E2 + one GPU, vLLM with the MP connector | End-to-end, the C9 bring-up |
| E4 | E3 against a 2- to 3-node Aerospike cluster, plus a second LMCache host | Multi-node, sharing between hosts, node failure |
| E5a | E4 on ConnectX with RoCE | Real RC hardware |
| E5b | E4 on AWS with EFA (SRD) | The EFA path, which has never run |
| E6 | E4 on MI350P with ROCm (optional) | Vendor neutrality |

### The functional test box

Functional testing runs on one AMD Developer Cloud droplet
(`gpu-mi300x1-192gb`: one MI300X with 192 GB HBM3, 20 vCPUs, 240 GiB RAM,
a 720 GiB persistent boot disk, a 5 TiB non-persistent scratch NVMe, 25 Gbps
private networking, no RDMA NIC). E0 to E4 all run on it:

- **E1 to E4 use Soft-RoCE on `lo`.** Every process is on one host, so
  loopback RC is enough and needs no network.
- **E4's cluster is three Aerospike processes on the same host,** each with
  its own ports and its own partition of the scratch disk, meshed over
  loopback. Killing a node is killing a process.
- **E4's "second LMCache host" is a second LMCache server and a second vLLM
  instance on the same GPU,** each with `--gpu-memory-utilization 0.4`.
  This tests the sharing logic, not the network between hosts.
- **The run is ROCm, so this is also E6** on CDNA 3 rather than CDNA 4.

What it cannot cover, and what changes because of that:

- **No tensor parallelism above 1.** T-LKP-05's parallelism half runs only
  at unit level (`test_object_key_parallel.py`). An 8-GPU droplet
  (`gpu-mi300x8-1536gb`) covers it later.
- **T-E2E-11 runs Llama-3.3-70B at TP=1,** which fits: 140 GB of BF16
  weights leave about 30 GB for KV cache.
- **E5a and E5b are out of scope.** No ConnectX, no EFA. Soft-RoCE between
  two droplets over the private network is possible but still software, so
  it adds little over loopback.
- **The scratch disk does not survive the droplet being destroyed.** Fine
  for a cache; record results and configs on the boot disk.

**Model choice.** Functional tests use a small model so iteration is fast:
Llama-3.1-8B (32 layers, 8 KV heads, one 256-token chunk is 32 MiB). The
hybrid model is **gpt-oss-120b**, AMD's lead MI350P model in MLPerf
Inference v6.1: 36 layers alternating full attention and a 128-token
sliding window, 8 KV heads of dimension 64, about 36 KiB of KV per token.
Its window is shorter than one 256-token chunk, an edge case for the
sliding-window planner that Gemma-3's longer window does not hit. Whether
LMCache's MP path handles its KV layout (attention sinks, MoE) is untested,
so it is a day-one check. The 70B model is only needed once, in E4, to
prove the layout math at production size.

## 4. Entry criteria

Before any test above E0 starts:

- [ ] Stage 0 of [c9-bring-up.md](c9-bring-up.md) is green on the test box.
- [ ] The prompt corpus and the baseline completions for it are recorded
  and checked in.
- [ ] The Aerospike server build under test is identified by commit, and it
  implements `kv-sink-fetch-pipelined` with per-piece write-with-immediate
  (the current server fences and replies once; see
  [`aerospike_rdma.md`](../distributed/l2_adapters/aerospike_rdma.md),
  "What the prototype proves").
- [ ] `ulimit -l` allows the configured slab, and `ibv_devinfo` shows the
  port active.

## 5. Test cases

Status is **Exists** (a test file already covers it; the plan is to run it
in the named environment), **Extend** (a test exists but misses the case),
or **New**.

Priority: **P0** blocks the exit of this phase; **P1** must run before the
benchmark; **P2** is best effort.

### 5.1 Build and configuration

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-CFG-01 | Default build with no RDMA flags | Compiles and links with no libibverbs; adapter works without RDMA | E0 | P0 | Exists |
| T-CFG-02 | `BUILD_WITH_AEROSPIKE_RDMA=1` build; EFA build on a box with libefa | Both build; RDMA extension imports | E0 | P0 | Exists (manual) |
| T-CFG-03 | `fetch_timeout_seconds` ≥ `--l1-write-ttl-seconds` | Startup refuses with a message naming both | E0 | P0 | Exists |
| T-CFG-04 | RDMA with a growable L1 slab | `ValueError` naming the hazard | E0 | P0 | Exists |
| T-CFG-05 | `window_bytes` too small for `--pipelined-max-chunks` | Registration warns and the model is not pipelined | E0 | P1 | Extend |
| T-CFG-06 | Wrong GID index (0 on Soft-RoCE `lo`) | Fails at startup with an error that names the GID, not a late `ENETUNREACH` | E1 | P1 | New |
| T-CFG-07 | Slab over `RLIMIT_MEMLOCK` | Startup error names memlock | E1 | P1 | New |
| T-CFG-08 | Registration log for a supported model | `<model> fetches layer by layer from L2 adapter 0...` appears | E3 | P0 | Exists (runbook) |

### 5.2 Storage: the plain (non-RDMA) path

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-STO-01 | Put, exists, get, delete, for single-record and sharded objects | Round trip byte-identical; exists false after delete | E2 | P0 | Exists (`test_aerospike_l2_integration.py`) |
| T-STO-02 | Plane-aligned sharding: a record never straddles two layers | Every record's range lies in one K or V plane | E0 | P0 | Exists (`test_shard_plan.py`, `test_slot_plan_parity.py`) |
| T-STO-03 | Metadata record written last | Kill the writer after segments, before metadata: `exists` is false and nothing is served | E2 | P0 | New |
| T-STO-04 | Missing segment under intact metadata (delete one segment by hand) | Read fails, LMCache treats it as a miss, completion correct | E2 | P0 | New |
| T-STO-05 | Corrupt metadata (wrong total size, bad runs string) | Read fails as corrupt, never as a short read | E2 | P1 | Extend |
| T-STO-06 | Records written before plane-aligned sharding | Still readable | E2 | P1 | Exists (`test_aerospike_record_layouts_integration.py`) |
| T-STO-07 | TTL applied: record TTL equals `default_ttl_seconds` | Checked through the server's record metadata | E2 | P1 | New |
| T-STO-08 | Replication factor 2, commit level all | Write returns only after both replicas have it; read survives one node down | E4 | P1 | New |

### 5.3 Lookup and keys

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-LKP-01 | Batch exists over mixed hits and misses | Per-key results correct and in request order | E2 | P0 | Extend |
| T-LKP-02 | Batch exists implemented with the Aerospike batch call | Same results as the per-key loop on 10,000 keys; one round trip per node, not per key | E2 | P0 | New (needs `do_batch_exists` override) |
| T-LKP-03 | Prefix semantics: hits at 0, 1, 3, 5 of 6 chunks, with a gap | Hit length is the leading run only; nothing after a gap is used | E2 | P0 | Extend |
| T-LKP-04 | Tenant isolation: P-salt | Salt A never hits salt B's entries | E3 | P0 | Exists (`test_cache_salt_l2_eviction.py` for eviction); New end to end |
| T-LKP-05 | Model and parallelism isolation: same prompt, different model; same model at TP=1 then TP=2 | No hit across either | E3 | P0 | New |
| T-LKP-06 | Two keys that share every field but `object_group_id` (hybrid model) | Stored and fetched separately | E0 | P1 | Extend |

### 5.4 RDMA data path (whole object)

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-RDMA-01 | RDMA write lands byte-identical to the source | memcmp equal | E1 | P0 | Exists (`test_rdma_equivalence.py`) |
| T-RDMA-02 | Nothing lands outside the requested offsets | Rest of slab still zero | E1 | P0 | Exists |
| T-RDMA-03 | Sink past the registered window | Refused, not written | E1 | P0 | Exists |
| T-RDMA-04 | Region handle is held per node | Two nodes, two handles | E1 | P0 | Exists |
| T-RDMA-05 | Per-node `kv-sink-register` fanout against a real cluster | Every node registers the windows | E4 | P0 | New (never run) |
| T-RDMA-06 | Byte oracle at production level: 100 real keys from P-exact, RDMA fetch vs plain get | Byte-identical for every key | E2 | P0 | New |

### 5.5 Pipelined retrieve

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-PIPE-01 | A layer is ready only when every piece has landed | Never reported early | E1 | P0 | Exists (`test_rdma_pipeline.py`) |
| T-PIPE-02 | Unsent layers' destination regions untouched while earlier layers are consumed | Untouched | E1 | P0 | Exists |
| T-PIPE-03 | Out-of-order arrival: layer 5 before layer 2 | Layers still handed to the GPU in ascending order | E0 | P0 | Exists (`test_layer_arrival_pump.py`) |
| T-PIPE-04 | Stale generation, unknown slot, duplicate immediate | Each distinguished and not counted | E1 | P0 | Exists |
| T-PIPE-05 | Server declines one slot | That layer is unservable; `fell_back`; completion correct | E3 | P0 | Exists at E0; New end to end |
| T-PIPE-06 | Layer never arrives | Deadline fires, both sides abandoned, window quarantined, completion correct | E3 | P0 | Exists at E0; New end to end |
| T-PIPE-07 | Late write from an abandoned fetch lands after the window is leased again | Not credited to the new fetch; new fetch's bytes correct | E1 | P0 | Exists (pool test); Extend to E3 |
| T-PIPE-08 | More concurrent retrieves than `window_count` | Extras `refused`, served by the whole-object path; all correct | E3 | P0 | New |
| T-PIPE-09 | Plan over the slot cap or over `--pipelined-max-chunks` | `refused` or `not_deferred` before anything is leased | E0 | P0 | Exists |
| T-PIPE-10 | Two requests with the same prefix at once, each `--pipelined-shared-keys` mode | Outcome matches the mode (`reused`, `shared_keys_busy`); both correct | E3 | P0 | Exists at E0 (`test_shared_keys.py`); New end to end |
| T-PIPE-11 | Hybrid model: sliding-window layers fetch only the chunks in their window | Plan and bytes match; completion correct | E3 | P0 | Exists at E0; New end to end |
| T-PIPE-12 | Records written under one `max_record_bytes`, read under another | Declined cleanly, `fell_back`, correct | E3 | P1 | New |

### 5.6 End to end with vLLM

All at batch size 1 against the recorded baseline unless stated.

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-E2E-01 | P-short | No L2 traffic; output equal | E3 | P0 | New |
| T-E2E-02 | P-exact and P-ragged, second send hits L1 | Output equal | E3 | P0 | New |
| T-E2E-03 | Same, but restart the LMCache server between sends so the hit is L2, pipelined off | Output equal; `loaded_whole` or `not_deferred` as configured | E3 | P0 | New |
| T-E2E-04 | Same, pipelined on | Output equal; `pipelined` on every eligible request | E3 | P0 | Exists (runbook stage 3); New automated |
| T-E2E-05 | P-long, at and one chunk over the cap | At the cap `pipelined`; over it served another way; both equal | E3 | P0 | New |
| T-E2E-06 | P-shared: 10 requests reusing one system prompt | Later requests hit the prefix; all equal | E3 | P0 | New |
| T-E2E-07 | P-multi: 5-turn conversations | Each turn hits the previous turns' full chunks; all equal | E3 | P0 | New |
| T-E2E-08 | Hybrid model through T-E2E-02 to 07 | All equal | E3 | P0 | New |
| T-E2E-09 | 16 concurrent clients over P-shared and P-multi | Byte oracle per request; top-1 logprob agreement with baseline ≥ 99.9% of tokens | E3 | P0 | New |
| T-E2E-10 | Metrics: counts per `pipelined_outcome` equal to what each test set up | Equal | E3 | P1 | New |
| T-E2E-11 | Llama-3.3-70B at TP=4, one pass of T-E2E-04 and 06 | Output equal; records and plans at production size | E4 | P1 | New |

### 5.7 Failure injection

Each test injects one fault while requests are running and checks rule 2.
Faults are applied with `kill`, `ip link set down` on the device under
`rxe0` or the NIC, `tc netem`, or the `fault_inject` L2 adapter wrapper
([fault_inject.md](../distributed/l2_adapters/fault_inject.md)).

| ID | Fault | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-FLT-01 | Load fails for keys that looked present (`fault_inject`, `gap_tail_ratios`) | Leading run served, rest recomputed, output equal | E3 | P0 | Exists (adapter); New with Aerospike inner |
| T-FLT-02 | Kill one Aerospike node mid-fetch, replication factor 1 | Affected requests recompute; engine alive; later requests succeed | E4 | P0 | New |
| T-FLT-03 | Same with replication factor 2 | Reads fail over to the replica; no recompute needed after the cluster settles | E4 | P1 | New |
| T-FLT-04 | Aerospike node restarts | Registrations dropped (current conservative behaviour); requests fall back until re-registration; no wrong output | E4 | P0 | New |
| T-FLT-05 | Kill the LMCache server mid-retrieve | vLLM reports the blocks failed and recomputes under `kv_load_failure_policy: "recompute"`; with `fail`, the request errors cleanly | E3 | P0 | New |
| T-FLT-06 | Restart the LMCache server between turns | L2 entries still found (the restart claim); output equal | E3 | P0 | New |
| T-FLT-07 | Link down for 2 s during a fetch | Fetch times out, window quarantined then reused, output equal | E3 | P0 | New |
| T-FLT-08 | 5% packet loss (`tc netem`) for 60 s | No wrong output; fallbacks counted | E5a | P1 | New |
| T-FLT-09 | Writer killed mid-store (between segments and metadata) | No partial entry ever reported present | E2 | P0 | Same as T-STO-03 |
| T-FLT-10 | Two engines store the same chunk at the same time, 1,000 times | Every later read returns bytes equal to one of the two writes, never a mix; output correct | E4 | P1 | New (tests the known interleaving gap) |
| T-FLT-11 | Server's reply names a slot it was not asked for | Protocol violation reported; request falls back | E1 | P1 | Exists (`kUnknownSlot`) |

### 5.8 Eviction and capacity

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-EVT-01 | Fill the namespace past its eviction threshold | Aerospike evicts; lookups after that are either correct hits or misses; zero wrong output | E2 | P0 | New |
| T-EVT-02 | Segments evicted while metadata survives | Read fails, miss, recompute; no crash | E2 | P0 | Same mechanism as T-STO-04 |
| T-EVT-03 | Records past TTL | Not found; recompute | E2 | P1 | New |
| T-EVT-04 | Eviction during an in-flight pipelined fetch | Affected layers declined; `fell_back`; correct | E3 | P1 | New |
| T-EVT-05 | L1 pressure while RDMA windows are leased | Windows never evicted; general L1 evicts normally | E0 | P0 | Exists (`test_l1_rdma_windows.py`) |
| T-EVT-06 | Client-side LRU disabled with two LMCache hosts sharing the cluster | Neither host deletes the other's entries | E4 | P1 | New |

### 5.9 Sharing between hosts

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-SHR-01 | Host A serves P-shared; host B sends the same prompts | Host B hits A's entries; output equal on both | E4 | P0 | New |
| T-SHR-02 | Both hosts store the same prefix simultaneously | Both correct; see T-FLT-10 | E4 | P1 | New |
| T-SHR-03 | Host A restarts while host B is fetching A's entries | Host B unaffected | E4 | P1 | New |

### 5.10 Fabric-specific

Rerun the P0 tests of 5.4, 5.5 and T-E2E-04, 06 and 07 on each fabric, plus:

| ID | Test | Pass criterion | Env | Pri | Status |
|---|---|---|---|---|---|
| T-FAB-01 | ConnectX RoCE: GID index auto-selection | Correct index chosen, or startup names the right one | E5a | P0 | New |
| T-FAB-02 | EFA: SRD queue pair creation, qkey, address handle for the server | Registration succeeds; no `UNKNOWN_PEER` | E5b | P0 | New (never run) |
| T-FAB-03 | EFA: unsolicited write receive capability present and negotiated | Capability reported; completions arrive without a posted receive, or receives are sized from the plan | E5b | P0 | New (`efa_imm_probe.cpp`) |
| T-FAB-04 | EFA: out-of-order piece arrival (SRD does not order) | Layers still ready only when complete; bytes correct | E5b | P0 | New; cannot be reproduced on Soft-RoCE |
| T-FAB-05 | More completions than receive slots under overload | No stuck queue pair; excess reported | E5a, E5b | P1 | New |

### 5.11 ROCm (optional, E6)

| ID | Test | Pass criterion | Pri |
|---|---|---|---|
| T-ROC-01 | `BUILD_WITH_HIP=1` build with the Aerospike and RDMA flags | Builds and imports | P1 |
| T-ROC-02 | Layer progress across processes (the CUDA IPC event path, on HIP) | Worker sees each layer's event | P1 |
| T-ROC-03 | T-E2E-02 to 07 on MI350P | Output equal | P1 |

## 6. Order of execution

Run in this order. Don't start a stage until the previous one's P0 tests
pass, so each failure has one new suspect.

1. **E0 in CI:** 5.1 and every E0 row. Already mostly green.
2. **E1 and E2:** 5.2, 5.3, 5.4, and the E1 rows of 5.5. T-LKP-02 needs the
   batch-exists change first.
3. **E3, following [c9-bring-up.md](c9-bring-up.md):** stages 2 and 3 of
   the runbook, then 5.6 at batch size 1, then the rest of 5.5 end to end.
4. **E3 faults:** the E3 rows of 5.7 and 5.8.
5. **E4:** multi-node, sharing, node failure, the 70B pass.
6. **E5a, then E5b:** the fabric reruns. EFA last, because it's the path
   that has never run.
7. **E6** if MI350P time is available.

## 7. Known behaviour to confirm, not fix, in this phase

These are current, documented behaviours. Tests should confirm them and
record the result rather than fail on them:

- **A progress timeout or stale generation still stops vLLM's engine.**
  `LMCacheMPConnector.wait_for_layer_load` reports a failed retrieve as
  failed blocks, but deliberately raises on those two errors because the
  daemon's state is then unknown
  ([vllm-load-failure.md](vllm-load-failure.md)). Test: stall a layer past
  the worker's 5 s wait and confirm the engine stops with that error, not
  anything else. Record how often it happens across all fault tests. If it
  appears outside deliberate stalls, raise it to S1.
- **vLLM's default `kv_load_failure_policy` is `fail`.** Run every fault
  test under `recompute`, and T-FLT-05 under both.
- **A hybrid model's recompute restarts from token 0.** Correct, just slow.
- **Aerospike node restart drops every registration.** Conservative until
  the server team answers the in-flight fetch question.
- **Pipelined fetch is single-node per record for now** (N1 deferred).

## 8. Blockers and owners

| Blocker | Blocks | Owner |
|---|---|---|
| Aerospike server with per-piece write-with-immediate and no fence | Every E2+ pipelined test | Aerospike server team |
| Answer on in-flight fetches after a node restart | Final behaviour of T-FLT-04 | Aerospike server team |
| `do_batch_exists` override using the Aerospike batch call | T-LKP-02 | LMCache adapter (Track A) |
| Create-only metadata write | Closing T-FLT-10 | LMCache adapter (Track A) |
| GPU box with Soft-RoCE | E3 onward | Infra |
| 3-node cluster and a second LMCache host | E4 | Infra |
| ConnectX and EFA access | E5a, E5b | Infra |
| MI350P access | E6 | AMD partnership |

## 9. Exit criteria

The functional phase is done when:

- [ ] Every P0 test passes in its lowest environment and in E4.
- [ ] Every P0 test in 5.4, 5.5 and 5.6 passes on at least one real fabric
  (E5a or E5b).
- [ ] Zero S1 and zero S2 defects open.
- [ ] Every fault test ends in correct output or a clean request error, with
  the engine alive, apart from the section 7 timeout behaviour.
- [ ] The results, the Aerospike server commit and the LMCache commit are
  recorded together, so the benchmark runs on exactly what was tested.
