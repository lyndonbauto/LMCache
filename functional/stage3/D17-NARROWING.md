# D-17 narrowing: wrong output after a failed layerwise load under recompute

Worker gpu-d17-narrow, 2026-10-01 20:54-21:45Z. Box tree `0a339c63` (it includes
the D-14 fix `e9cd0689`), rebuilt in lmc-c. vLLM `0.27.1.dev5+gf46a9dfe2.d20260827`
(ROCm). kv-sink `512b0c207` (a fencing build). Llama-3.1-8B-Instruct,
`VLLM_BATCH_INVARIANT=1`, temperature 0. Box evidence:
`/root/lmc-work/functional/stage3/d17/<row>/` (`summary_d17_<row>.txt`,
`report_d17_<row>_all.md`, LMCache and vLLM logs). Driver: [d17.sh](d17.sh).

## Conclusion

**D-17 is a vLLM bug, not an LMCache one.** It is the open upstream issue
[vllm#49250](https://github.com/vllm-project/vllm/issues/49250) ("recompute
gives wrong output when a KV connector rejects a synchronous load (V2 runner)").
Its fixes, [#49252](https://github.com/vllm-project/vllm/pull/49252) and
[#53298](https://github.com/vllm-project/vllm/pull/53298), are open and not merged.
The issue has two parts:

1. **V2 model runner (both scheduling modes).** `GPUModelRunner.update_requests`
   (`vllm/v1/worker/gpu/model_runner.py`) writes the scheduler's rewound
   `num_computed_tokens` only to the CPU mirror `req_states.num_computed_tokens_np`.
   The GPU tensor `req_states.num_computed_tokens` is staged only when a request is
   added, and is otherwise only advanced by the `post_update` kernels. On the recompute
   step the GPU value is therefore still 1024, not 0. `prepare_prefill_inputs`
   sees `num_computed >= prefill_len` and writes no prompt input ids, and
   `prepare_pos_seq_lens` builds positions from 1024. The prompt KV is never
   rebuilt. That accounts for the wrong first token and the garbage text in every
   V2 row.
2. **Scheduler, async scheduling only.** `Scheduler._update_requests_with_invalid_blocks`
   (`vllm/v1/core/sched/scheduler.py`) does not roll back the failed request's
   `num_output_placeholders`. So the token sampled in the discarded step (or the one
   in flight behind it) is still delivered as output token 1, and decoding continues
   from it. This explains the V1 + async row: the first token is wrong, and the
   continuation is correct and on topic.

LMCache's part is only exposure. The MP connector reports a layerwise load
as **synchronous** (`get_num_new_matched_tokens` returns `load_async=False` when
`use_layerwise`). That is the only path that hits both vLLM bugs, because the step
runs its forward pass and samples before the load failure is known. LMCache's own
contract holds: the daemon publishes a failure only after its queued copies land.
The evidence:
- In the repro, layer 2 was declared lost at 21:04:42.796, the worker reported
  64 blocks at .813, and the retrieve closed at .821. The whole-object fallback
  raises before any GPU launch (`_finish_from_whole_objects`, then `reload_whole`).
- With vLLM fixed (V1 runner with async off, or V2 with part 1 patched and async
  off), the same fault and the same LMCache build give exact output in every case:
  pipe05 2/2 three times, pipe06 5/5 twice.
- Even the V1 + async run continues correctly after its one leaked token, so the KV
  that the recompute rebuilt was right.

Prefix-cache reuse of poisoned blocks is not involved: every Stage 3 and D-17 V2
reproduction ran with `--no-enable-prefix-caching`, which is the harness default.

## Matrix

Every row runs T-PIPE-05 on P-exact-10 (4 chunks, 1024 tokens, 64 blocks of 16):
store, settle, LMCache restart, delete segment 5 of chunk 1 (meta kept), then
`probe` and `again`. The oracle is the store send (exact against the batch-invariant
baselines). The pipe06 rows use Stage 3's pipe06: kv-sink is SIGSTOPped for 2.5 s at
lookup end for stall10/11/12, then after13/14 are sent. Layerwise and pipelined are on, cap 4, unless
stated otherwise. APC = vLLM prefix caching.

| Row | Change from repro | Runner, async | Failure path seen | vLLM recovery | Output (probe / again) |
|---|---|---|---|---|---|
| `repro` | none (Stage 3 pipe05 on the rebuilt tree) | V2, on | layer 2 lost, then whole fallback cannot load 1 object, `failed`. Connector: "reporting 64 blocks" (all of the request's blocks) | rescheduled, 1024 tokens | **wrong 0/2**: `' New York City, New York, USA…'`, `' New York York York…'` |
| Stage 3 `pipe05_noasync` | async off | V2, off | same | rescheduled, 1023 tokens | **wrong 0/2**: right first token, then garbage |
| `v1runner` | `VLLM_USE_V2_MODEL_RUNNER=0` | V1, on | same | rescheduled, 1024 tokens | **wrong 0/2**: `'1. 2677 Record E10-2: the school in Cairo has access code 2677.'` (one leaked token, then a correct continuation) |
| `noasync_v1` | V1 runner, async off | V1, off | same | rescheduled, 1023 tokens | **exact 2/2** |
| `fix_noasync` | scratch vLLM with only part 1 of #49250, async off | V2 + part 1, off | same | rescheduled, 1023 tokens | **exact 2/2** |
| `fix_noasync_r2` | repeat of `fix_noasync` | V2 + part 1, off | same | rescheduled, 1023 tokens | **exact 2/2** |
| `fix_async` | part 1 only, async on | V2 + part 1, on | same | rescheduled, 1024 tokens | **wrong 0/2**: `' New York Times does not have a record of this information. Answer: 26…'` (part 2 is still missing) |
| `pipe06_fixna` | pipe06 fault, part 1, async off | V2 + part 1, off | stall10/11/12: layer 0 deadline, `failed`, 64 blocks reported each time | rescheduled ×3, 1023 tokens | **exact 5/5** (3 failed and recomputed, after13/14 `refused`) |
| `pipe06_v1na` | pipe06 fault, V1 runner, async off | V1, off | stall10/11: layer 0 deadline, `failed`; stall12 `refused` | rescheduled ×2 | **exact 5/5** |
| (a) `nopipe` | `--pipelined-fetch` off, plain adapter (no RDMA) | V2, on | no load failure: lookup/prefetch finds chunk 1 missing and the hit shrinks to chunk 0 (`retrieved_count=1`, `not_deferred`) | none | exact 2/2 (the fault cannot reach the forward pass) |
| (b) `nolw` | layerwise off, pipelined off | V2, on | same as `nopipe` (async load path) | none | exact 2/2 |
| (c) `apc` | APC on | V2, on | none: vLLM still held the prompt from the store send (it was not restarted), and the small LMCache tail hit was `pipelined` | none | exact 2/2. The fault was not reached, so this row is N/A |
| (d) `failpol` | `kv_load_failure_policy=fail` | V2, on | same as repro | "Failing 1 request(s) due to KV load failure" ×2 | **clean error**: HTTP 500 for both, no wrong tokens (pass) |

On (e), "fails before the forward pass": in this product a failure that LMCache
detects before the forward pass (missing records found at lookup or prefetch) does
not take the recompute path at all. It shrinks the hit instead, as `nopipe` and `nolw`
show. The earliest layerwise failure is the layer-0 deadline in pipe06, which hits D-17
exactly like the layer-2 failure, and the upstream reproducer rejects a load before
copying any KV. So the defect is "any synchronous load rejection", not "mid-forward".
No separate fault was built for this.

Blocks reported against blocks used: in every failed load the connector reported 64
blocks, which are all of P-exact-10's blocks (1024 / 16). vLLM's
`_update_requests_with_invalid_blocks` truncated the request to 0 computed tokens
("1024 tokens affected" with async on, where one token was in flight; 1023 with it
off). Nothing was left unreported.

## Fix proposal

1. **vLLM (the actual fix).** Carry the upstream fix into the ROCm vLLM build used
   for this work. Prefer #53298, the more complete one: it also covers spec decode
   and EAGLE. At minimum, carry both parts of #49252.
   - Part 1, in `GPUModelRunner.update_requests` (V2): when the scheduler's
     `num_computed_tokens` is below `num_computed_tokens_np[req_index]`, call
     `req_states.num_computed_tokens.stage_write_elem(req_index, n)`, then
     `apply_write()` once after the loop. This is the 10-line change in
     [d17_mkfix.py](d17_mkfix.py), and it alone fixed every async-off row.
   - Part 2, in `_update_requests_with_invalid_blocks`: roll back
     `num_output_placeholders` for each affected request, and discard the in-flight
     frames (`async_tokens_to_discard`). Async scheduling is vLLM's default, so
     without part 2 the deployment is still wrong.
   - #53298 does not apply cleanly to 0.27.1 (6 hunks fail), so it needs a backport.
2. **LMCache guard (until a fixed vLLM is pinned).** In
   `LMCacheMPConnector.__init__` (`lmcache/integration/vllm/lmcache_mp_connector.py`),
   when `use_layerwise` is on and `kv_transfer_config.kv_load_failure_policy ==
   "recompute"`, refuse to start, or at least log an error once, with a pointer to
   vllm#49250. Gate this on a feature check of the running vLLM, not a version string.
   A good check is whether the scheduler or runner has the rewind hook that #53298
   adds, for example `SchedulerOutput.rewound_req_ids`. Do not switch layerwise
   loads to `load_async=True` as a workaround (it is upstream's suggestion): that removes
   the layer/compute overlap that layerwise exists for. Also fix the
   `wait_for_layer_load` docstring. It states that vLLM then "recomputes" the blocks,
   which is true only on a fixed vLLM.
3. **Until then, in the functional plan:** run T-PIPE-05/06 and T-FLT-05/06/07
   either under `fail`, or under `recompute` with the patched vLLM, and record
   recompute with async scheduling as blocked by D-17.

### End-to-end test for the fix

`d17.sh repro pipe06` (plus `noasync_v1` / `fix_noasync` as controls) on the fixed
vLLM, **with async scheduling on and off**. The pass rule: `probe` and `again` exact
against the oracle (2/2), and pipe06 exact 5/5, with vLLM logging "Recovered from
KV load failure" for each failed load. The recompute path must actually fire, so a
run with no "Recovered" line fails. As a test in the product, `tests/v1/` gets a GPU
test that drives `LMCacheMPConnector` with layerwise on against an MP server whose
retrieve is forced to fail at layer *k* (k = 0 and k > 0), under `recompute`. It
compares the generated token IDs with a no-connector run, for both
`--async-scheduling` and `--no-async-scheduling`. Upstream's
`test_reject_recompute_matches_baseline` covers the vLLM side with a dummy connector.
The LMCache test covers the real layerwise path.

## Notes

- The harness's `vllm_check` `engine_dead_errors` count is a false positive on
  these runs. Its pattern `EngineCore.*(died|failed)` matches LMCache's
  `(EngineCore pid=…) … Layerwise KV load failed` warning. vLLM stayed alive
  throughout.
- The scratch vLLM (`/work/scratch/vllm-d17fix`, used only through `PYTHONPATH`) is
  left on the box for reuse. The installed vLLM was not modified.
