# Functional testing, Day 1 (environment): summary

Box: one MI300X droplet (ROCm 10, kernel 6.8, torch 2.12+rocm10, vLLM
0.27.1). Branch `prototype-stage1`. Versions and host changes are in
`VERSIONS.md` and `CHANGES.md` beside this file on the box
(`/root/lmc-work/functional/day1/`), together with every log, script and raw
JSON. The harness is committed under `functional/harness/`, and the Aerospike
config under `functional/configs/`.

## Results by test ID

| Step | Test | Result | Evidence (under `day1/` on the box) |
| --- | --- | --- | --- |
| 1 | T-CFG-01: Aerospike build, no RDMA flags | Pass. Builds in 67 s; `lmcache_aerospike` does not link `libibverbs`, and it imports | `step1/build_A_no_rdma.log`, `step1/check_A.txt` |
| 1 | T-CFG-02: Aerospike + RDMA build | Pass. Builds in 68 s; links `libibverbs`, imports, and the RDMA-only symbols are present | `step1/build_B_rdma.log`, `step1/check_B.txt` |
| 1 | T-ROC-01: HIP build with the Aerospike and RDMA flags | Pass (the T-CFG-02 build, with `BUILD_WITH_HIP=1`) | `step1/build_B_rdma.log` |
| 1 | Unit suites from the plan | Pass. Event IPC 40 passed; layerwise, deferred retrieve and pipelined loading 475 passed. No skips | `step1/suite_1_events.txt`, `step1/suite_2_layerwise.txt` |
| 2 | T-RDMA-01 to 04 (Soft-RoCE) | Pass, after the fix in "Soft-RoCE on this host". The RDMA suite on `rxe0` passes 17/17 with no skips, and the harnesses ran on the device: byte-identical landing (01), nothing outside the requested offsets (02), a write past the window refused (03), a region handle per node (04). The E1-level pipeline checks pass too | `step2/rdma_suite.txt`, `step2/rdma_harness_direct.txt`, `step2/verbs_checks.txt` |
| 3 | T-STO-01, T-STO-06, T-LKP-01, T-LKP-02 | Pass. 14/14 integration tests against Aerospike CE 8.2.0, no skips, including the 10,000-key batch exists and read-touch TTL tests | `step3/integration.txt` |
| 4 | Corpus and Llama-3.1-8B baselines | Done. 130 prompts, built in token space so lengths are exact. The baseline is deterministic: 130/130 exact across two fresh servers, both with and without batch invariance | `step4/compare_base.md`, `step4b/bi_base_summary.txt` |
| 5 | gpt-oss-120b check | Pass. Registers (36 layers) and serves with LMCache, layerwise off and on. P-exact cached hits 20/20 identical to the baseline; P-shared prefix hits 10/10 identical to vLLM's own prefix cache (see "gpt-oss-120b") | `step5_bi/` |
| 6 | T-E2E-02: plain path, L1 hit, layerwise off and on | Pass. P-exact 20/20 token-identical to the baseline; every retrieve `not_deferred` | `step6_bi/` |
| 6 | T-E2E-03: plain path, L2 hit after an LMCache restart, layerwise off and on | Pass. 20/20 token-identical; 23,110 Aerospike reads after the restart; `not_deferred` | `step6_bi/` |

## The oracle (decides Day 2)

Rule 1 of the test plan is token equality with the no-LMCache baseline at
batch size 1. The plain baseline is deterministic, but a cache hit changes the
shape of the computation: vLLM computes only the last token over the loaded
KV, and GPU kernels are not bit-identical across such shapes. Without batch
invariance, cached Llama outputs match on 16 to 18 of 20 P-exact prompts.
Every answer is still correct, the first token agrees 20/20, and the first-token
logprob difference is at most 0.12.

This vLLM build supports `VLLM_BATCH_INVARIANT=1` on ROCm. With it:

| Llama-3.1-8B, all 130 prompts | Layerwise off | Layerwise on |
| --- | --- | --- |
| Cold send vs baseline | 130/130 exact | 130/130 exact |
| Warm send (cached; 98 retrieves, 384 chunks) vs baseline | 130/130 exact | 130/130 exact |

**Oracle for Day 2: token equality, with `VLLM_BATCH_INVARIANT=1` set for
both the baseline and every LMCache run.** The recorded batch-invariant
baselines are `step4/bi_run1.json` and `bi_run2.json` for Llama. The kernels
are about 4x slower, so no timing from these runs means anything; functional
tests don't need timings. The plan's fallback oracle (correct answer, top-1
first token, divergence within the baseline spread) is not needed.

## gpt-oss-120b (the hybrid model)

- **Setup.** Downloaded (183 GB with the original and Metal weights) and
  served without LMCache. The baselines for P-exact and P-shared are
  deterministic: 30/30 across two fresh servers, with and without batch
  invariance.
- **With LMCache** (CPU L1 only, layerwise off and then on), registration logs
  `Registered KV cache for GPU ID ... with 36 layers`, and 507 chunks were
  retrieved per session.
- **Cached hits match the oracle:**
  - P-exact (whole-prompt hits): 20/20 token-identical to the batch-invariant
    baseline, cold and warm, layerwise off and on.
  - P-shared (an 8-chunk shared prefix from cache, then a computed tail): 7/10
    against the no-cache baseline. vLLM's **own** prefix cache, with no
    LMCache, gives exactly the same 7/10, while no prefix cache at the same
    block size gives 10/10. So this is vLLM's kernels on a cached prefix:
    batch invariance does not cover the prefix split for gpt-oss's sink and
    sliding-window attention. LMCache output is **10/10 token-identical to
    vLLM's own prefix cache**, cold and warm, layerwise off and on.
- **KV layout.** The sliding window of 128 (below one 256-token chunk),
  attention sinks and MoE (MXFP4 emulated on gfx942) all work through the MP
  connector.
- **Oracle for hybrid-model prefix hits on Day 2:** compare against vLLM with
  its own prefix cache, at the same block size, under batch invariance.
- **Weak answer check.** With raw prompts, gpt-oss rarely answers the registry
  question correctly (about 2 in 30), so the wrong-answer check adds little for
  this model. Token equality is the oracle.

Evidence: `step5_bi/compare_*.md` and `step5_bi/pc_check.txt`. The first
attempt, without batch invariance, is in `step5/`.

## Defects and findings

| Severity | Finding | Cause (if known) | Owner |
| --- | --- | --- | --- |
| S2 | If the LMCache server restarts faster than vLLM's heartbeat interval (10 s), vLLM never re-registers. Every later lookup misses ("No GPU context found"), with no recovery | The worker notices a restart only when a heartbeat fails while the server is down; it does not detect a new server instance | vLLM MP adapter |
| S2 | vLLM takes up to one heartbeat interval to re-register after an LMCache restart, and requests in that window recompute | Same mechanism | vLLM MP adapter |
| S3 | A failed L2 store logs `Store task N to adapter 0 failed for keys: [...]` with no reason | The store controller drops the error detail | Storage manager |
| S3 | With the MP connector, gpt-oss-120b runs with KV block size 16, where vLLM alone picks 64 for its ROCm attention backend | LMCache's resolved MP geometry | MP connector |
| Info | For gpt-oss-120b, a cached prefix changes the output even in vLLM alone (vLLM's prefix cache vs no cache: 7/10 exact on P-shared under batch invariance). LMCache matches vLLM's prefix cache exactly | vLLM's batch-invariant kernels are not prefix-split-invariant for sink and sliding-window attention | vLLM (upstream) |
| Info | The Aerospike container I started was reachable from the internet; outside IPs connected. Fixed: every port bound to 127.0.0.1, and vLLM and LMCache's HTTP server now bind to 127.0.0.1 in the harness. Earlier vLLM sessions on this box listened on 0.0.0.0:8000 | Host networking | Test setup (fixed) |
| Info | Aerospike writes failed with "queue too deep: exceeds max 8" under P-exact's 10 GiB bursts. Fixed with `max-write-cache 2G` | CE default write cache on a file device | Test setup (fixed) |
| Info | Pre-existing test failures, identical without today's changes: `test_engine_driven_transfer.py` (ImportError of `TransferDirection`); `test_cache_server.py` and `test_mq.py` (they expect registration to return `None`); `test_torch_ops.py` `cpu_py_ops`/`multi_layer_block_kv_transfer` | Stale tests | Owners of those tests |

### Merge review (GitHub step 4)

Fixed in `16d471e8`:

- `lmcache.mp.use_layerwise` was parsed two ways. A CLI string `"false"` turned
  layerwise on in the connector and off in the worker, which is silent KV
  corruption.
- StorageManager used two rules to pick "the pipelined adapter", so plans and
  fetches could come from different adapters.
- `downsample_and_stage_block_ids` validated block-id lists with `assert`.

Listed for their owners, not fixed today:

1. The timeout budget does not fit. Pump timeout (2.5 s), then whole-object
   fallback (2.5 s), plus any shared-key wait exceeds the worker's 5 s
   per-layer wait, and a worker timeout stops the engine. (Tracks B and C.)
2. Nothing cross-checks that Track C's planner and Track B's per-layer staging
   compute the same per-layer byte ranges. Drift would be silent corruption.
3. The whole-object fallback re-runs `_objects_of` after it has just failed,
   and on success `objects_to_place` and `request_cache_keys` run two or three
   times. (Track C.)
4. "Too many slots" raises `ValueError` where `PlanTooLargeError` is
   documented, and the check is duplicated. (Track C.)
5. Cleanups:
   - the test-only C++ `SlotPlanner` and `FetchPlanner.participating_chunks`;
   - the sliding-window rule, written four times;
   - defaults duplicated between the server config and `PipelinedFetchConfig`;
   - unreachable defensive code and three `Optional` fields in
     `lmcache_driven_transfer.retrieve`;
   - per-layer staging's Python call count;
   - the test fakes imported by `lmcache.v1.layerwise`;
   - the duplicated shared-memory attach helper.

## Soft-RoCE on this host

`modprobe rdma_rxe` fails with "Invalid argument". The in-tree module does not
match this host's `ib_core`, which comes from MLNX OFED 24.10 via DKMS and which
`amdgpu` and `ionic` depend on. MLNX OFED ships only a dummy `rxe`.

The fix, approved by Lyndon Bauto and Simon Zhao and logged in `CHANGES.md`:

1. The upstream Linux v6.11 `drivers/infiniband/sw/rxe` is built out of tree
   against OFED's headers and `Module.symvers` (`/root/r_build.sh`; output in
   `/root/rxe-build/v6.11/`). It depends on OFED's `ib_core`, `ib_uverbs` and
   `mlx_compat`.
2. It is loaded with `insmod`, installing nothing into `/lib/modules`, and
   `rxe0` is created on `lo`.
3. The port is ACTIVE, GID index 1 is `::ffff:127.0.0.1` (RoCE v2), and
   `ibv_rc_pingpong` passes over GID 1 at 4 KiB and 64 KiB.

Notes:

- **Where the verbs tools run.** The host's user-space verbs library is
  OFED's, with only the `ionic` and `mlx5` providers, so it cannot see `rxe0`.
  The verbs tools and tests run in `lmc-c`, which has Ubuntu's `rdma-core` 50
  with the `rxe` provider. `lmc-c` was saved as `lmcache-rocm:day1` and
  recreated with `--device /dev/infiniband/uverbs0`.
- **It does not survive a reboot.** After one, run:
  `modprobe udp_tunnel ip6_udp_tunnel && insmod /root/rxe-build/v6.11/rdma_rxe.ko && rdma link add rxe0 type rxe netdev lo`
- **A test-build bug** kept two RDMA tests from running. Their C++ harnesses
  were never built, because the Makefile's first target was `pyharness`, not
  `all`. Fixed in `bb833c9c` (a test-build change, not product code).

## Blockers

- **Day 3.** The RDMA pipelined path needs the Aerospike `kv-sink` server
  build, which signals each piece with write-with-immediate, doesn't fence,
  and works over RC on Soft-RoCE. Soft-RoCE is now available.

## GPU time

About 3 GPU-hours today: roughly 0.5 h for the morning's ROCm event fix and
2.5 h for Day 1. The plan's running total was about 18, so it is now about 21
of 150.

## Next steps (Day 2, not started)

- **Oracle:** run every test with `VLLM_BATCH_INVARIANT=1`.
- **LMCache restarts:** keep them longer than vLLM's heartbeat interval, or
  restart vLLM too, until the S2 above is fixed.
- **gpt-oss prompts:** send its prompts through its chat template, so wrong KV
  also shows up as a wrong answer. With raw prompts it answers the registry
  question correctly only about 3 times in 30.
