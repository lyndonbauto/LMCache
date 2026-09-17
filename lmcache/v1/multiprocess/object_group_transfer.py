# SPDX-License-Identifier: Apache-2.0
"""Object-group KV transfer helpers, including layerwise H2D retrieve."""

# Standard
import inspect
from collections.abc import Sequence

# Third Party
import torch

# First Party
from lmcache.logging import init_logger
from lmcache.v1.gpu_connector.gpu_ops import lmcache_memcpy_async_h2d
from lmcache.v1.memory_management import MemoryObj
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerLaunch, LayerwiseSchedule
from lmcache.v1.platform.base.cache_context import BaseCacheContext
import lmcache.lmcache_native as lmcache_native
from lmcache import device_ops

logger = init_logger(__name__)


def _invoke_multi_layer_block_kv_transfer(
    paged_buffer_ptrs: torch.Tensor,
    staging_ptrs: list[int],
    block_ids_gpu: torch.Tensor,
    device: torch.device,
    direction: int,
    shape_desc: object,
    lmcache_chunk_size: int,
    engine_kv_format: int,
    skip_prefix_n_blocks: int,
    layer_offset: int,
    n_layers: int,
) -> None:
    """Call the native block transfer, including optional layer sub-range."""
    try:
        # First Party
        import lmcache.cuda_ops as cuda_ops

        sig = inspect.signature(cuda_ops.multi_layer_block_kv_transfer)
        if "layer_offset" in sig.parameters:
            cuda_ops.multi_layer_block_kv_transfer(
                paged_buffer_ptrs,
                staging_ptrs,
                block_ids_gpu,
                device,
                direction,
                shape_desc,
                lmcache_chunk_size,
                engine_kv_format,
                skip_prefix_n_blocks,
                layer_offset,
                n_layers,
            )
            return
    except (ImportError, ValueError, TypeError):
        pass

    if layer_offset != 0 or n_layers != 1:
        raise RuntimeError(
            "layerwise MP retrieve requires a CUDA extension that supports "
            "layer_offset and n_layers on multi_layer_block_kv_transfer"
        )
    device_ops.multi_layer_block_kv_transfer(
        paged_buffer_ptrs,
        staging_ptrs,
        block_ids_gpu,
        device,
        direction,
        shape_desc,
        lmcache_chunk_size,
        engine_kv_format,
        skip_prefix_n_blocks,
    )


def _kernel_group_to_object_group(
    cache_context: BaseCacheContext,
) -> dict[int, int]:
    """Map each kernel group index to its object group index."""
    mapping: dict[int, int] = {}
    manager = cache_context.kv_layer_groups_manager
    for object_group_id, object_group in enumerate(manager.object_groups):
        for kernel_group_id in object_group.kernel_group_indices:
            mapping[kernel_group_id] = object_group_id
    return mapping


def transfer_kv_layerwise_h2d(
    cache_context: BaseCacheContext,
    block_ids_gpu: list[torch.Tensor],
    memory_objs_by_group: Sequence[Sequence[MemoryObj | None]],
    skip_first_n_tokens: int,
    schedule: LayerwiseSchedule,
    progress: LayerProgressRecord,
    event_pool: DaemonLayerLaunchEventPool,
    retrieve_generation: int,
    *,
    transfer_key: str,
) -> None:
    """Retrieve KV with one H2D kernel launch per scheduled layer.

    Staging still copies whole memory objects to GPU temp buffers before the
    first launch that needs a batch; per-layer launches then reuse those
    buffers. Overlap is therefore between attention on layer *L* and the
    transfer stream work for layer *L+1* (kernel, and any staging for later
    batches not yet copied), not between staging and the first layer of the
    same object.

    Args:
        cache_context: Registered worker cache context on the daemon.
        block_ids_gpu: Staged GPU block-id tensors per kernel group.
        memory_objs_by_group: Memory objects per object group (with None padding
            for skipped prefix chunks).
        skip_first_n_tokens: Tokens to skip at the start of the retrieve range.
        schedule: Global per-layer launch order for this layout.
        progress: Shared progress record for this worker instance.
        event_pool: IPC events recorded after each ordinal on ``cache_context.stream``.
        retrieve_generation: Generation tag shared with the worker waiter.
        transfer_key: Phase-timing identity for observability.

    Raises:
        ValueError: If a batch contains null memory objects on H2D.
        RuntimeError: If the native extension lacks layer-range support.
    """
    del transfer_key  # reserved for future phase timing on layerwise path

    # Local import avoids a module cycle with lmcache_driven_transfer.
    from lmcache.v1.multiprocess.modules.lmcache_driven_transfer import (
        _recalculate_blocks_to_skip,
        batched_iteration_with_skip,
    )

    lmcache_chunk_size = cache_context.lmcache_tokens_per_chunk
    kv_groups_manager = cache_context.kv_layer_groups_manager
    kg_to_og = _kernel_group_to_object_group(cache_context)
    direction_int = int(lmcache_native.TransferDirection.H2D)

    attn_desc = kv_groups_manager.get_attn_desc()
    progress.begin_retrieve(retrieve_generation)

    staged_batches: set[tuple[int, int]] = set()

    try:
        for ordinal, launch in enumerate(schedule.launches):
            object_group_id = kg_to_og[launch.kernel_group_index]
            memory_objs = memory_objs_by_group[object_group_id]
            object_group = kv_groups_manager.object_groups[object_group_id]
            kernel_group_ids = object_group.kernel_group_indices

            num_objects_to_skip = 0
            if not attn_desc.is_full_attention(object_group_id):
                sw_size_chunks = attn_desc.num_chunks_in_sw[object_group_id]
                num_objects_to_skip = max(0, len(memory_objs) - sw_size_chunks)

            for start_object_idx, memory_object_batch in batched_iteration_with_skip(
                memory_objs,
                cache_context.max_batch_size,
                skip_count=num_objects_to_skip,
            ):
                if any(mo is None for mo in memory_object_batch):
                    raise ValueError(
                        "MemoryObj is None for some objects in the batch, "
                        "cannot perform H2D copy."
                    )

                batch_len = len(memory_object_batch)
                batch_start_token = start_object_idx * lmcache_chunk_size
                batch_end_token = batch_start_token + batch_len * lmcache_chunk_size
                effective_start = max(batch_start_token, skip_first_n_tokens)
                if effective_start >= batch_end_token:
                    continue

                skip_tokens_in_chunk = effective_start - batch_start_token
                batch_key = (object_group_id, start_object_idx)
                object_group_buffers = [
                    cache_context.get_temp_object_group_buffer(
                        slot, object_group_id
                    )
                    for slot in range(batch_len)
                ]
                if batch_key not in staged_batches:
                    for chunk_idx, memory_obj in enumerate(memory_object_batch):
                        lmcache_memcpy_async_h2d(
                            memory_obj,
                            object_group_buffers[chunk_idx],
                        )
                    staged_batches.add(batch_key)

                kernel_group_id = launch.kernel_group_index
                if kernel_group_id not in kernel_group_ids:
                    continue

                blocks_per_chunk = cache_context.calculate_num_blocks(
                    lmcache_chunk_size, kernel_group_id
                )
                tokens_per_window = min(
                    lmcache_chunk_size,
                    kv_groups_manager.get_subchunk_sw_size_tokens(kernel_group_id),
                )
                blocks_per_window = cache_context.calculate_num_blocks(
                    tokens_per_window, kernel_group_id
                )
                start_block_pos = start_object_idx * blocks_per_window
                end_block_pos = (start_object_idx + batch_len) * blocks_per_window
                block_ids_curr_batch = block_ids_gpu[kernel_group_id][
                    start_block_pos:end_block_pos
                ]
                orig_skip_blocks = cache_context.calculate_num_blocks(
                    skip_tokens_in_chunk, kernel_group_id
                )
                recalculated_skip_blocks = _recalculate_blocks_to_skip(
                    blocks_per_chunk,
                    blocks_per_window,
                    orig_skip_blocks,
                )
                tmp_gpu_buffers_batched = [
                    cache_context.get_temp_kernel_group_buffer(
                        slot, kernel_group_id
                    ).data_ptr()
                    for slot in range(batch_len)
                ]
                _invoke_multi_layer_block_kv_transfer(
                    cache_context.get_kernel_group_kv_pointers(kernel_group_id),
                    tmp_gpu_buffers_batched,
                    block_ids_curr_batch,
                    cache_context.device,
                    direction_int,
                    cache_context.get_shape_desc(kernel_group_id),
                    cache_context.get_slots_per_chunk_in_sw(kernel_group_id),
                    cache_context.get_engine_kv_format(kernel_group_id),
                    recalculated_skip_blocks,
                    launch.position_in_group,
                    1,
                )

            event_pool.record_ordinal(ordinal, cache_context.stream)
            progress.report_launch_recorded(ordinal + 1)
    except Exception:
        progress.mark_retrieve_failed()
        raise


def launch_for_layer(
    schedule: LayerwiseSchedule, layer_id: int
) -> LayerLaunch:
    """Return the scheduled launch metadata for ``layer_id``.

    Args:
        schedule: Layerwise schedule for the registered layout.
        layer_id: Global layer index.

    Returns:
        The :class:`LayerLaunch` entry for that layer.

    Raises:
        KeyError: If ``layer_id`` is not scheduled.
    """
    return schedule.launch_for(layer_id)
