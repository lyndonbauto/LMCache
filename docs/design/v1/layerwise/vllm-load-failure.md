# What vLLM does when a layerwise retrieve fails mid-step (R5)

Checked against vLLM **v0.30.0** (released 2026-09-22), the latest release
when this was written. It answers Track B's R5 and open question 1 of
[fetch-start-proposal.md](fetch-start-proposal.md).

## Summary

- **Raising inside attention kills the engine.** Every request on it fails,
  and the server has to be restarted. Today LMCache's connector raises for
  every layer-wait error except a generation timeout.
- **Reporting the blocks as failed is recoverable,** as long as it happens
  in the same step. vLLM discards that step's output for the affected
  requests, then fails or recomputes them according to
  `kv_load_failure_policy`. The rest of the batch is unaffected.
- **vLLM's default policy is `fail`,** which ends the request with an error.
  Recomputing needs `kv_load_failure_policy: "recompute"`.
- **On a hybrid model, recompute redoes the whole request,** not just the
  failed blocks.

## The two paths

### Raising in attention

`maybe_transfer_kv_layer` (`vllm/model_executor/layers/attention/kv_transfer_utils.py`)
calls `connector.wait_for_layer_load(layer_name)` before each attention layer
and does not catch anything. An exception escapes `execute_model`, and
`EngineCoreProc.run_engine_core` (`vllm/v1/engine/core.py`) logs "EngineCore
encountered a fatal error" and calls `_send_engine_dead()`.

LMCache's `LMCacheMPConnector.wait_for_layer_load` swallows only
`LayerProgressRetrieveGenerationTimeoutError`. So these reach vLLM and kill
the engine:

| Error | Raised when |
|---|---|
| `LayerProgressRetrieveFailedError` | The daemon abandoned the load (failure flag). This includes every pump failure, since the pump abandons the sink. |
| `LayerProgressRetrieveProgressTimeoutError` | The watermark stalled past the worker's wait (5 s). |
| `LayerProgressStaleGenerationError` | Shared memory shows a newer generation. |

This is true of MP layerwise load today, whether or not the fetch is
pipelined.

### Reporting failed blocks

After the forward pass, in the same step, the model runner collects
`connector.get_block_ids_with_load_errors()` into
`KVConnectorOutput.invalid_block_ids`. See `_get_kv_connector_output` in
`vllm/v1/worker/kv_connector_model_runner_mixin.py`, and `post_forward` in
`vllm/v1/worker/gpu/kv_connector.py`. `Scheduler.update_from_output` then:

1. Finds the running requests whose computed blocks include a failed one
   (`_handle_invalid_blocks` → `_update_requests_with_invalid_blocks`).
2. Skips those requests when processing the step's output, so the token
   sampled from bad KV is never emitted.
3. Acts according to `kv_load_failure_policy`:
   - **`fail` (default):** finishes them with `FINISHED_ERROR`, and evicts
     the failed blocks and everything after them from the prefix cache.
   - **`recompute`:** truncates `num_computed_tokens` to the first failed
     block and reschedules. A request with more than one KV cache group (a
     hybrid model) has no single valid prefix, so it restarts from token 0.
     A request sharing a failed block is rescheduled too.

The report must be in the **same step**. With layerwise load on, our
connector returns `load_async=False`, so vLLM treats the load as
synchronous. `KVConnectorBase_V1.get_block_ids_with_load_errors` says a
synchronous load's failures "should be reported in the forward pass in which
they are detected". A report one step late arrives after the bad token has
been emitted.

## What follows for LMCache

1. **The connector turns a failed retrieve into failed blocks (Track B).**
   When `wait_for_layer_load` catches `LayerProgressRetrieveFailedError`, it
   stops waiting on this step's remaining layers. It then reports every
   block of this step's retrieves from `get_block_ids_with_load_errors`, in
   the same step and once, instead of raising.

   The two timeouts keep raising. A progress timeout means the daemon may
   still be copying into those blocks, and recomputing into blocks that are
   still being written would corrupt them. With the pump's timeout below the
   worker's (R6), a progress timeout means the daemon is hung, not merely
   slow.
2. **The daemon fails a retrieve only by abandoning the sink (Track C).**
   A retrieve that cannot deliver every layer abandons the sink, which sets
   the failure flag. It never finishes the watermark with blocks unwritten
   and returns `False`. The worker then learns about the failure at its next
   layer wait, which is always within the step, because the failing layer is
   one it has not yet passed.

   The retrieve's `False` reply is then only a backstop. The adapter polls
   it with `query()` after the forward pass, so it can land a step late.
3. **Deployments set `kv_load_failure_policy: "recompute"`.** Otherwise a
   failed load fails the request. That is still better than losing the
   engine, but it is not a recovery.
4. **Layerwise load needs LMCache's connector.** The `LMCacheMPConnector`
   bundled with vLLM v0.30.0 has no layerwise support: its
   `wait_for_layer_load` returns immediately. Load LMCache's with
   `"kv_connector_module_path": "lmcache.integration.vllm.lmcache_mp_connector"`.

For the fetch-start proposal, this means a mid-fetch failure no longer needs
the daemon to recover for correctness. Abandoning the sink yields a
recompute. The in-daemon fallback, which continues the same load with
whole-object loads, stays as an optimization: on our hybrid targets a
recompute is a full prefill of the request.
