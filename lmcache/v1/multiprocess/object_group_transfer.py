# SPDX-License-Identifier: Apache-2.0
"""Object-group KV transfer for the multiprocess server.

Also provides layerwise H2D retrieve.

Per-kernel-group gather and scatter between the engine's paged KV cache and
LMCache memory objects, driven by the cache context's ``KVLayerGroupsManager``:
block-id downsampling for sub-chunk sliding windows, skip recalculation, and
the per-object-group transfer plan the copy kernels run.
"""

# Standard
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from typing import Any, Generator, Protocol, Sequence

# Third Party
import torch

# First Party
from lmcache import device_ops
from lmcache.logging import init_logger
from lmcache.v1.gpu_connector.gpu_ops import (
    build_h2d_range_staging_copies,
    build_staging_copies,
    lmcache_memcpy_async_d2h,
    lmcache_memcpy_async_h2d,
    lmcache_memcpy_async_h2d_range,
)
from lmcache.v1.memory_allocators.lazy_memory_allocator import LazyMemoryAllocator
from lmcache.v1.memory_management import GDSMemoryObject, MemoryObj
from lmcache.v1.mp_observability.event import EventType
from lmcache.v1.mp_observability.event_bus import (
    get_event_bus,
    is_observability_enabled,
)
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerLaunch, LayerwiseSchedule
from lmcache.v1.platform.base.cache_context import BaseCacheContext
from lmcache.v1.platform.ops_types import (
    BatchStep,
    KernelGroupSpec,
    PageBufferShapeDesc,
    StagingCopy,
)
import lmcache.lmcache_native as lmcache_native

logger = init_logger(__name__)
_HAS_NATIVE_OBJECT_GROUP_TRANSFER: bool = hasattr(
    device_ops, "execute_object_group_transfer"
)
_HAS_TRANSFER_PHASE_TIMING: bool = hasattr(device_ops, "pop_completed_phase_timings")


def _native_runs_layer_subranges() -> bool:
    """Whether the native plan executor can run a launch over a layer subrange.

    Older builds have ``execute_object_group_transfer`` but a ``LaunchVar``
    without ``layer_offset`` / ``n_layers``; constructing one with them is
    the only way to tell.
    """
    if not _HAS_NATIVE_OBJECT_GROUP_TRANSFER:
        return False
    try:
        device_ops.LaunchVar(0, 0, 0, 1, 0, layer_offset=0, n_layers=1)
    except (TypeError, NotImplementedError):
        return False
    return True


_HAS_NATIVE_LAYER_LAUNCHES: bool = _native_runs_layer_subranges()


def batched_iteration_with_skip(
    lst: Sequence,
    batch_size: int,
    skip_count: int,
) -> Generator[tuple[int, tuple], None, None]:
    """Utility function to iterate over a list in batches with an initial skip.

    Args:
        lst: The list to iterate over.
        batch_size: The size of each batch.
        skip_count: The number of items to skip at the start of the list.

    Yields:
        Tuples of (batch_start_idx, batch) where batch is a tuple of items
        from the list, and batch_start_idx is the "original" index of the first
        item in the batch.

    Raises:
        ValueError: If batch_size is less than 1 or skip_count is negative.

    Note:
        Batch_idx is the index of the batch in the original list, accounting
        for the skipped items. For example, if skip_count is 10 and batch_size
        is 5, the first yielded batch will have batch_start_idx=10.
    """
    if batch_size < 1:
        raise ValueError("batch size must be at least one")
    if skip_count < 0:
        raise ValueError("skip_count must be non-negative")

    it = iter(lst)
    # Skip the initial items
    for _ in range(skip_count):
        next(it, None)
    batch_start_idx = skip_count
    while batch := tuple(islice(it, batch_size)):
        yield batch_start_idx, batch
        batch_start_idx += len(batch)


def downsample_and_stage_block_ids(
    cache_context: BaseCacheContext,
    block_ids: list[list[int]],
) -> list[torch.Tensor]:
    """Cut the block id lists to skip the unneeded blocks in a chunk and
    stage it into GPU tensors for later use.

    This mainly targets the case where a portion of the blocks are not
    needed for every chunk, such as deepseek v4's swa cache.

    Note that the we do NOT do any object-level skipping here.

    Args:
        cache_context: The cache context containing the KV cache information.
        block_ids: The original block id lists, indexed by LMCache KV group index.

    Returns:
        The cut block id lists, indexed by LMCache KV group index.

    Raises:
        ValueError: If a kernel group's block id list is not a whole number of
            chunks.

    Note:
        This function has some coupled logic with transfer_kv_per_object_group below.
        The caller need to make sure that the block ids seen by
        transfer_kv_per_object_group are produced by this function.

    Example:
        If a model have 2 kernel groups, one is full attention with block size 32,
        one is swa attention with block size 32 and sliding window size 64, and
        LMCache has a chunk size of 128. And there are 2 chunks in total (256 tokens).

        The input will be:
        [
          [1, 2, 3, 4, 5, 6, 7, 8],  # block ids for the full attention group
          [11, 12, 13, 14, 15, 16, 17, 18], # block ids for the swa attention group
        ]

        The output will be
        [
          [1, 2, 3, 4, 5, 6, 7, 8],  # full attention group still needs all block ids
          [13, 14, 17, 18], # swa attention group only needs the last 2 block per chunk
        ]
    """
    num_kernel_groups = cache_context.kv_layer_groups_manager.num_kernel_groups
    for kernel_group_id in range(num_kernel_groups):
        subchunk_sw_size_tokens = (
            cache_context.kv_layer_groups_manager.get_subchunk_sw_size_tokens(
                kernel_group_id
            )
        )
        tokens_per_chunk = min(
            cache_context.lmcache_tokens_per_chunk, subchunk_sw_size_tokens
        )
        keep_blocks_per_chunk = cache_context.calculate_num_blocks(
            tokens_per_chunk, kernel_group_id
        )
        total_blocks_per_chunk = cache_context.calculate_num_blocks(
            cache_context.lmcache_tokens_per_chunk, kernel_group_id
        )

        new_block_ids = []
        old_block_ids = block_ids[kernel_group_id]
        if len(old_block_ids) % total_blocks_per_chunk != 0:
            raise ValueError(
                f"len(block_ids[{kernel_group_id}]) should be a multiple "
                f"of total_blocks_per_chunk ({total_blocks_per_chunk}), but got "
                f"{len(old_block_ids)}"
            )

        for i in range(0, len(old_block_ids), total_blocks_per_chunk):
            chunk_block_ids = old_block_ids[i : i + total_blocks_per_chunk]
            new_block_ids.extend(chunk_block_ids[-keep_blocks_per_chunk:])

        block_ids[kernel_group_id] = new_block_ids

    # Stage the cut block ids into GPU tensors
    block_ids_gpu = cache_context.stage_block_ids(block_ids)
    return block_ids_gpu


def recalculate_blocks_to_skip(
    blocks_per_chunk: int,
    blocks_per_window: int,
    blocks_to_skip: int,
) -> int:
    """Re-calculate the number of blocks to skip for a batch of chunks based
    on the blocks per chunk and blocks per sliding window WHEN the window
    size is smaller than the lmcache chunk size.

    Args:
        blocks_per_chunk: The total number of blocks in one chunk for the
            current group.
        blocks_per_window: The number of blocks in the sliding window
            for the current group. Should be less than or equal to
            blocks_per_chunk.
        blocks_to_skip: The number of blocks to skip.

    Returns:
        The re-calculated number of blocks to skip for the current batch of
        chunks.
    """
    if blocks_per_chunk == blocks_per_window:
        return blocks_to_skip

    full_windows_to_skip = blocks_to_skip // blocks_per_chunk
    tail_blocks = blocks_to_skip % blocks_per_chunk
    tail_blocks_to_skip = tail_blocks - (blocks_per_chunk - blocks_per_window)
    return full_windows_to_skip * blocks_per_window + max(0, tail_blocks_to_skip)


def _run_object_group_transfer_plan(
    cache_context: BaseCacheContext,
    block_ids_gpu: list[torch.Tensor],
    memory_objs: Sequence[MemoryObj | None],
    object_group_id: int,
    batch_size: int,
    skip_first_n_tokens: int,
    direction: "lmcache_native.TransferDirection",
    *,
    transfer_key: str,
) -> None:
    """Plan and execute one object group's transfer in a single native call.

    This is the fast path of :func:`transfer_kv_per_object_group`: it runs the
    same batched-iteration / skip logic, but instead of issuing each staging
    copy and kernel launch immediately (each a GIL release/re-acquire), it
    resolves every argument to plain pointers/scalars (the "planner", GIL held
    throughout) and hands the whole plan to ``execute_object_group_transfer``,
    which issues all of it on the stream within a single GIL release.

    Requires every object to be non-GDS (staged through the lazy-allocator
    path); the caller skips groups that contain any GDS-backed object.

    Args:
        cache_context: The GPU cache context containing the KV cache information.
        block_ids_gpu: GPU block IDs, indexed by LMCache KV group index.
        memory_objs: The MemoryObj instances to copy. None entries are only
            valid for D2H (the batch is skipped); H2D raises.
        object_group_id: Index of the object group being copied.
        batch_size: Number of memory objects per batched copy.
        skip_first_n_tokens: Tokens to skip writing at the start of the range.
        direction: H2D (retrieve) or D2H (store).
        transfer_key: Identity of this store/retrieve operation, echoed back
            on every phase-timing sample (a request issues several transfers,
            so the request id cannot identify one).

    Raises:
        ValueError: If a None entry is found in memory_objs when direction is
            H2D, or if an object's size does not match its GPU staging buffer.
    """
    lmcache_chunk_size = cache_context.lmcache_tokens_per_chunk
    kv_groups_manager = cache_context.kv_layer_groups_manager
    object_group = kv_groups_manager.object_groups[object_group_id]
    kernel_group_ids = object_group.kernel_group_indices
    is_h2d = direction == lmcache_native.TransferDirection.H2D
    max_batch_size = cache_context.max_batch_size

    # --- Per-kernel-group invariants, resolved once (vs. every batch before) ---
    kernel_group_specs: list[Any] = []
    spec_index_by_kg: dict[int, int] = {}
    blocks_per_chunk_by_kg: dict[int, int] = {}
    blocks_per_window_by_kg: dict[int, int] = {}
    for kernel_group_id in kernel_group_ids:
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
        blocks_per_chunk_by_kg[kernel_group_id] = blocks_per_chunk
        blocks_per_window_by_kg[kernel_group_id] = blocks_per_window

        paged_ptrs = cache_context.get_kernel_group_kv_pointers(kernel_group_id)
        block_ids_tensor = block_ids_gpu[kernel_group_id]
        temp_buffers = [
            cache_context.get_temp_kernel_group_buffer(slot, kernel_group_id)
            for slot in range(max_batch_size)
        ]

        spec_index_by_kg[kernel_group_id] = len(kernel_group_specs)
        kernel_group_specs.append(
            device_ops.KernelGroupSpec(
                paged_ptrs.data_ptr(),
                [buffer.data_ptr() for buffer in temp_buffers],
                cache_context.get_shape_desc(kernel_group_id),
                cache_context.get_slots_per_chunk_in_sw(kernel_group_id),
                cache_context.get_engine_kv_format(kernel_group_id),
                block_ids_tensor.data_ptr(),
                block_ids_tensor.numel(),
            )
        )

    # Temp object-group staging buffers (reused per batch slot, like above).
    object_group_buffers = [
        cache_context.get_temp_object_group_buffer(slot, object_group_id)
        for slot in range(max_batch_size)
    ]

    attn_desc = kv_groups_manager.get_attn_desc()
    num_objects_to_skip = 0
    if not attn_desc.is_full_attention(object_group_id) and is_h2d:
        sw_size_chunks = attn_desc.num_chunks_in_sw[object_group_id]
        num_objects_to_skip = max(0, len(memory_objs) - sw_size_chunks)
        logger.debug(
            "Detected sliding window for object group %d: "
            "skipping the first %d objects in the batch",
            object_group_id,
            num_objects_to_skip,
        )

    # --- Walk the batches in order, emitting staging + launch work per step ---
    batch_steps: list[Any] = []
    for start_object_idx, memory_object_batch in batched_iteration_with_skip(
        memory_objs, batch_size, skip_count=num_objects_to_skip
    ):
        if any(mo is None for mo in memory_object_batch):
            if is_h2d:
                raise ValueError(
                    "MemoryObj is None for some objects in the batch, cannot "
                    "perform H2D copy. memory_object_batch: "
                    f"{memory_object_batch}"
                )
            else:
                continue

        batch_len = len(memory_object_batch)
        batch_start_token = start_object_idx * lmcache_chunk_size
        batch_end_token = batch_start_token + batch_len * lmcache_chunk_size

        effective_start = max(batch_start_token, skip_first_n_tokens)
        if effective_start >= batch_end_token:
            continue

        skip_tokens_in_chunk = effective_start - batch_start_token

        staging = build_staging_copies(
            memory_object_batch,
            object_group_buffers[:batch_len],
            is_h2d,
        )

        launches: list[Any] = []
        for kernel_group_id in kernel_group_ids:
            blocks_per_chunk = blocks_per_chunk_by_kg[kernel_group_id]
            blocks_per_window = blocks_per_window_by_kg[kernel_group_id]

            start_block_pos = start_object_idx * blocks_per_window
            end_block_pos = (start_object_idx + batch_len) * blocks_per_window

            orig_skip_blocks = cache_context.calculate_num_blocks(
                skip_tokens_in_chunk, kernel_group_id
            )
            recalculated_skip_blocks = recalculate_blocks_to_skip(
                blocks_per_chunk,
                blocks_per_window,
                orig_skip_blocks,
            )

            launches.append(
                device_ops.LaunchVar(
                    spec_index_by_kg[kernel_group_id],
                    start_block_pos,
                    end_block_pos - start_block_pos,
                    batch_len,
                    recalculated_skip_blocks,
                )
            )

        batch_steps.append(device_ops.BatchStep(staging, launches))

    if not batch_steps:
        return

    # Time the phases only when a subscriber consumes the samples. An older
    # compiled extension has neither the keywords nor anything to consume
    # them, so fall back to the untimed legacy signature.
    timing_kwargs = (
        {
            "phase_timing_enabled": is_observability_enabled()
            and get_event_bus().has_subscribers(EventType.MP_TRANSFER_PHASE_SAMPLES),
            # Echoed back verbatim on each sample; the transfer's identity.
            "session_id": transfer_key,
        }
        if _HAS_TRANSFER_PHASE_TIMING
        else {}
    )
    device_ops.execute_object_group_transfer(
        direction,
        cache_context.device,
        LazyMemoryAllocator.PIN_CHUNK_SIZE,
        kernel_group_specs,
        batch_steps,
        **timing_kwargs,
    )


def transfer_kv_per_object_group(
    cache_context: BaseCacheContext,
    block_ids_gpu: list[torch.Tensor],
    memory_objs: Sequence[MemoryObj | None],
    object_group_id: int,
    batch_size: int,
    skip_first_n_tokens: int,
    direction: "lmcache_native.TransferDirection",
    *,
    transfer_key: str,
) -> None:
    """Helper function to transfer memory objects of a single object group
    to/from GPU, with batching support.

    Args:
        cache_context: The GPU cache context containing the KV cache information.
        block_ids_gpu: GPU block IDs to retrieve into, indexed by LMCache KV group
            index. It should satisfy `len(block_ids_gpu[i]) == len(memory_objs) *
            blocks_per_chunk[i]` for each group `i`.
            Note that the block IDs list are already on GPU.
        memory_objs: The list of MemoryObj instances to copy from. It could be
            None when allocation or retrieval fails. For store (D2H), it should
            ignore the None entry and continue copying the rest. For retrieve
            (H2D), it should raise the error and stop copying.
        object_group_id: Index of the object group being copied.
        batch_size: The number of memory objects to perform batched copy
        skip_first_n_tokens: Number of tokens to skip writing at the start of
            the retrieve range. This avoids overwriting APC-shared GPU blocks that
            may be read concurrently by other requests.
        direction: The transfer direction, H2D (retrieve) or D2H (store).
        transfer_key: Identity of this store/retrieve operation, echoed back on
            every phase-timing sample; see _run_object_group_transfer_plan.

    Raises:
        ValueError: If it founds None entry in memory_objs when direction is H2D.
    Note:
        This function expects the caller to stage the block ids (list[list[int]])
        into GPU tensors and pass them in as `block_ids_gpu`.
    """
    if _HAS_NATIVE_OBJECT_GROUP_TRANSFER and not any(
        isinstance(mo, GDSMemoryObject) for mo in memory_objs
    ):
        _run_object_group_transfer_plan(
            cache_context,
            block_ids_gpu,
            memory_objs,
            object_group_id,
            batch_size,
            skip_first_n_tokens,
            direction,
            transfer_key=transfer_key,
        )
        return

    lmcache_chunk_size = cache_context.lmcache_tokens_per_chunk
    kv_groups_manager = cache_context.kv_layer_groups_manager
    object_group = kv_groups_manager.object_groups[object_group_id]
    kernel_group_ids = object_group.kernel_group_indices
    is_h2d = direction == lmcache_native.TransferDirection.H2D

    attn_desc = kv_groups_manager.get_attn_desc()
    num_objects_to_skip = 0
    if not attn_desc.is_full_attention(object_group_id) and is_h2d:
        sw_size_chunks = attn_desc.num_chunks_in_sw[object_group_id]
        num_objects_to_skip = max(0, len(memory_objs) - sw_size_chunks)
        logger.debug(
            "Detected sliding window for object group %d: "
            "skipping the first %d objects in the batch",
            object_group_id,
            num_objects_to_skip,
        )

    for start_object_idx, memory_object_batch in batched_iteration_with_skip(
        memory_objs, batch_size, skip_count=num_objects_to_skip
    ):
        if any(mo is None for mo in memory_object_batch):
            if is_h2d:
                raise ValueError(
                    "MemoryObj is None for some objects in the batch, cannot "
                    "perform H2D copy. memory_object_batch: "
                    f"{memory_object_batch}"
                )
            else:
                continue

        batch_len = len(memory_object_batch)
        batch_start_token = start_object_idx * lmcache_chunk_size
        batch_end_token = batch_start_token + batch_len * lmcache_chunk_size

        effective_start = max(batch_start_token, skip_first_n_tokens)
        if effective_start >= batch_end_token:
            continue

        skip_tokens_in_chunk = effective_start - batch_start_token

        # For H2D, copy from CPU to GPU tmp buffers before the kernel launch
        if is_h2d:
            for chunk_idx, memory_obj in enumerate(memory_object_batch):
                lmcache_memcpy_async_h2d(
                    memory_obj,
                    cache_context.get_temp_object_group_buffer(
                        chunk_idx, object_group_id
                    ),
                )

        # Do paged KV copy
        for kernel_group_id in kernel_group_ids:
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

            # Get the block ids for this chunk
            start_block_pos = start_object_idx * blocks_per_window
            end_block_pos = (start_object_idx + batch_len) * blocks_per_window

            block_ids_curr_batch = block_ids_gpu[kernel_group_id][
                start_block_pos:end_block_pos
            ]

            # Re-calculate the skip blocks for this kernel group
            orig_skip_blocks = cache_context.calculate_num_blocks(
                skip_tokens_in_chunk, kernel_group_id
            )
            recalculated_skip_blocks = recalculate_blocks_to_skip(
                blocks_per_chunk,
                blocks_per_window,
                orig_skip_blocks,
            )

            # Launch kernel
            group_kv_pointers = cache_context.get_kernel_group_kv_pointers(
                kernel_group_id
            )
            group_lmcache_chunk_size = cache_context.get_slots_per_chunk_in_sw(
                kernel_group_id
            )
            tmp_gpu_buffers_batched = [
                cache_context.get_temp_kernel_group_buffer(
                    i, kernel_group_id
                ).data_ptr()
                for i in range(batch_len)
            ]
            device_ops.multi_layer_block_kv_transfer(
                group_kv_pointers,
                tmp_gpu_buffers_batched,
                block_ids_curr_batch,
                cache_context.device,
                direction,
                cache_context.get_shape_desc(kernel_group_id),
                group_lmcache_chunk_size,
                cache_context.get_engine_kv_format(kernel_group_id),
                recalculated_skip_blocks,
            )

        # For D2H, copy from GPU tmp buffers to CPU after the kernel launch
        if not is_h2d:
            for chunk_idx, memory_obj in enumerate(memory_object_batch):
                lmcache_memcpy_async_d2h(
                    cache_context.get_temp_object_group_buffer(
                        chunk_idx, object_group_id
                    ),
                    memory_obj,
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


class MemoryObjectLookup(Protocol):
    """Where a layerwise retrieve reads its memory objects from.

    Positions are fixed for the retrieve's lifetime; the object at a position
    may be replaced between layers (a fallback swapping in whole objects), so
    :class:`LayerwiseH2DRetrieve` reads it again at each layer's launch.
    ``lmcache.v1.multiprocess.pipelined_loading.ObjectTable`` implements it.
    """

    def get(self, object_group_id: int, chunk_id: int) -> MemoryObj | None:
        """Return the object at one position, or ``None`` if there is none.

        Args:
            object_group_id: The object group.
            chunk_id: The chunk's index in the retrieve, counting skipped
                prefix chunks.
        """
        ...

    def by_group(self) -> list[list[MemoryObj | None]]:
        """Return every position's current object, one list per object group."""
        ...


class FixedMemoryObjects:
    """A :class:`MemoryObjectLookup` over objects that never change."""

    def __init__(self, memory_objs_by_group: Sequence[Sequence[MemoryObj | None]]):
        """Wrap the objects.

        Args:
            memory_objs_by_group: Memory objects per object group, with
                ``None`` padding for skipped prefix chunks.
        """
        self._objects = [list(group) for group in memory_objs_by_group]

    def get(self, object_group_id: int, chunk_id: int) -> MemoryObj | None:
        """Return the object at one position; see :class:`MemoryObjectLookup`."""
        return self._objects[object_group_id][chunk_id]

    def by_group(self) -> list[list[MemoryObj | None]]:
        """Return a copy of every group's objects."""
        return [list(group) for group in self._objects]


@dataclass(frozen=True)
class _LayerwiseKernelLaunchParams:
    """Per-kernel-group launch inputs for one staged batch."""

    recalculated_skip_blocks: int
    block_ids_curr_batch: torch.Tensor
    #: Where ``block_ids_curr_batch`` starts in the kernel group's block ids.
    block_ids_offset: int
    tmp_gpu_buffer_data_ptrs: tuple[int, ...]
    #: Per slot, where this kernel group's staging region starts inside the
    #: slot's object group buffer, in bytes. Per-layer staging adds a layer's
    #: plane offsets to it.
    staging_region_offsets: tuple[int, ...]


@dataclass(frozen=True)
class _LayerwiseBatchDescriptor:
    """Layer-independent H2D batch state reused for every scheduled layer.

    Holds positions, not objects: the batch's objects are the chunks
    ``start_object_idx`` onwards, one per staging buffer, read from the
    retrieve's :class:`MemoryObjectLookup` when a layer is staged.
    """

    start_object_idx: int
    object_group_buffers: tuple[torch.Tensor, ...]
    launch_params_by_kernel_group: dict[int, _LayerwiseKernelLaunchParams]


def _build_layerwise_batch_descriptors(
    cache_context: BaseCacheContext,
    block_ids_gpu: list[torch.Tensor],
    memory_objs_by_group: Sequence[Sequence[MemoryObj | None]],
    skip_first_n_tokens: int,
) -> dict[int, tuple[_LayerwiseBatchDescriptor, ...]]:
    """Precompute per-batch staging and launch inputs once per object group."""
    lmcache_chunk_size = cache_context.lmcache_tokens_per_chunk
    kv_groups_manager = cache_context.kv_layer_groups_manager
    attn_desc = kv_groups_manager.get_attn_desc()
    descriptors_by_object_group: dict[int, tuple[_LayerwiseBatchDescriptor, ...]] = {}

    for object_group_id, memory_objs in enumerate(memory_objs_by_group):
        object_group = kv_groups_manager.object_groups[object_group_id]
        kernel_group_ids = object_group.kernel_group_indices

        num_objects_to_skip = 0
        if not attn_desc.is_full_attention(object_group_id):
            sw_size_chunks = attn_desc.num_chunks_in_sw[object_group_id]
            num_objects_to_skip = max(0, len(memory_objs) - sw_size_chunks)

        batch_descriptors: list[_LayerwiseBatchDescriptor] = []
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
            object_group_buffers = tuple(
                cache_context.get_temp_object_group_buffer(slot, object_group_id)
                for slot in range(batch_len)
            )

            launch_params_by_kernel_group: dict[int, _LayerwiseKernelLaunchParams] = {}
            for kernel_group_id in kernel_group_ids:
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
                recalculated_skip_blocks = recalculate_blocks_to_skip(
                    blocks_per_chunk,
                    blocks_per_window,
                    orig_skip_blocks,
                )
                tmp_gpu_buffer_data_ptrs = tuple(
                    cache_context.get_temp_kernel_group_buffer(
                        slot, kernel_group_id
                    ).data_ptr()
                    for slot in range(batch_len)
                )
                launch_params_by_kernel_group[kernel_group_id] = (
                    _LayerwiseKernelLaunchParams(
                        recalculated_skip_blocks=recalculated_skip_blocks,
                        block_ids_curr_batch=block_ids_curr_batch,
                        block_ids_offset=start_block_pos,
                        tmp_gpu_buffer_data_ptrs=tmp_gpu_buffer_data_ptrs,
                        staging_region_offsets=tuple(
                            _staging_region_offset(data_ptr, buffer)
                            for data_ptr, buffer in zip(
                                tmp_gpu_buffer_data_ptrs,
                                object_group_buffers,
                                strict=True,
                            )
                        ),
                    )
                )

            batch_descriptors.append(
                _LayerwiseBatchDescriptor(
                    start_object_idx=start_object_idx,
                    object_group_buffers=object_group_buffers,
                    launch_params_by_kernel_group=launch_params_by_kernel_group,
                )
            )

        descriptors_by_object_group[object_group_id] = tuple(batch_descriptors)

    return descriptors_by_object_group


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

    Each layer stages only its own bytes (per-layer staging), so the first
    layer's wait covers one layer's copy rather than every layer's. GDS
    objects only transfer whole, so a retrieve holding any uses whole-object
    staging. Overlap is between attention on layer *L* and the transfer
    stream work for layer *L+1*.

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
        transfer_key: Same retrieve identity as the bulk path; reserved for
            future phase-timing on this path (not emitted today).

    Raises:
        ValueError: If a batch contains null memory objects on H2D.
        RuntimeError: If the native extension lacks layer-range support.
    """
    del transfer_key

    has_gds_objects = any(
        isinstance(mo, GDSMemoryObject)
        for group in memory_objs_by_group
        for mo in group
    )
    staging = LayerStaging.WHOLE_OBJECT if has_gds_objects else LayerStaging.PER_LAYER
    retrieve = LayerwiseH2DRetrieve(
        cache_context,
        block_ids_gpu,
        FixedMemoryObjects(memory_objs_by_group),
        skip_first_n_tokens,
        schedule,
        progress,
        event_pool,
        retrieve_generation,
        staging=staging,
    )
    retrieve.begin()
    for launch in schedule.launches:
        retrieve.launch_layer(launch.layer_id)


class LayerStaging(Enum):
    """How :class:`LayerwiseH2DRetrieve` copies host bytes to GPU staging.

    The choice is a correctness question, not only a performance one: it
    depends on whether the host memory objects are complete when the retrieve
    starts.
    """

    #: Copy only the launched layer's bytes, at its launch. Required whenever
    #: later layers may still be arriving in host memory (arrival-driven
    #: loading), because a whole-object copy would snapshot them early.
    PER_LAYER = "per_layer"
    #: Copy each object in full when a layer launches and its batch is not the
    #: one in the staging slots, then reuse it. Only correct when every object
    #: is complete before the retrieve begins, and the only mode GDS objects
    #: support. Cheap only when each object group fits in one batch: batches of
    #: a group share the staging slots, so each layer restages every batch.
    WHOLE_OBJECT = "whole_object"


@dataclass(frozen=True)
class _LayerPlaneGeometry:
    """Where one layer's bytes sit inside a kernel group's staging region.

    A kernel group's staging view is either ``(kv_size, num_layers, slots,
    hidden)``, where one layer is ``kv_size`` disjoint planes, or
    ``(num_layers, slots, hidden)``, where one layer is a single contiguous
    block.
    """

    num_planes: int
    """Byte ranges per layer: ``kv_size``, or 1 for the layer-major layout."""

    plane_stride_bytes: int
    """Distance between consecutive planes of one layer; 0 if single-plane."""

    layer_stride_bytes: int
    """Distance between the same plane of consecutive layers."""

    plane_bytes: int
    """Length of one plane."""

    def byte_ranges(self, position_in_group: int) -> tuple[tuple[int, int], ...]:
        """Return ``(offset, length)`` pairs holding one layer's bytes.

        Args:
            position_in_group: The layer's index along its group's layer axis.

        Returns:
            One pair per plane, offsets relative to the kernel group's region.
        """
        start = position_in_group * self.layer_stride_bytes
        return tuple(
            (start + plane * self.plane_stride_bytes, self.plane_bytes)
            for plane in range(self.num_planes)
        )


def _layer_plane_geometry(kernel_group_view: torch.Tensor) -> _LayerPlaneGeometry:
    """Derive per-layer byte ranges from a kernel group's staging view.

    Args:
        kernel_group_view: A shaped staging view, as returned by
            ``get_temp_kernel_group_buffer``.

    Returns:
        The geometry locating any single layer inside that view.

    Raises:
        ValueError: If the view is not contiguous or is neither 3- nor
            4-dimensional, since the layer axis could then not be located.
    """
    if not kernel_group_view.is_contiguous():
        raise ValueError("kernel group staging view must be contiguous")
    itemsize = kernel_group_view.element_size()
    if kernel_group_view.dim() == 4:
        layer_stride_bytes = kernel_group_view.stride(1) * itemsize
        return _LayerPlaneGeometry(
            num_planes=kernel_group_view.shape[0],
            plane_stride_bytes=kernel_group_view.stride(0) * itemsize,
            layer_stride_bytes=layer_stride_bytes,
            plane_bytes=layer_stride_bytes,
        )
    if kernel_group_view.dim() == 3:
        layer_stride_bytes = kernel_group_view.stride(0) * itemsize
        return _LayerPlaneGeometry(
            num_planes=1,
            plane_stride_bytes=0,
            layer_stride_bytes=layer_stride_bytes,
            plane_bytes=layer_stride_bytes,
        )
    raise ValueError(
        f"unsupported kernel group staging shape {tuple(kernel_group_view.shape)}"
    )


def _staging_region_offset(
    kernel_group_data_ptr: int, object_group_buffer: torch.Tensor
) -> int:
    """Where a kernel group's staging view starts inside its object group's
    staging buffer, in bytes. A memory object is laid out byte for byte like
    that buffer, so the same offset addresses the object."""
    return kernel_group_data_ptr - object_group_buffer.data_ptr()


def per_layer_staging_ranges(
    cache_context: BaseCacheContext, schedule: LayerwiseSchedule
) -> dict[int, tuple[tuple[int, int], ...]]:
    """Return the bytes per-layer staging copies for each scheduled layer.

    Computed exactly as :class:`LayerwiseH2DRetrieve` with
    :attr:`LayerStaging.PER_LAYER` computes them at each launch, from the
    staging views of batch slot 0 (every slot has the same layout). A
    pipelined fetch must land each layer at exactly these bytes, so a caller
    can check them against what its planner asks the transport to write.

    Args:
        cache_context: The registered worker cache context.
        schedule: The launch order built from the same kernel groups.

    Returns:
        ``{layer_id: ((offset, length), ...)}``, one pair per plane, offsets
        relative to the start of the layer's object group payload.

    Raises:
        ValueError: If a staging view is not contiguous or has an
            unsupported rank.
    """
    kernel_to_object_group = _kernel_group_to_object_group(cache_context)
    region_offsets: dict[int, int] = {}
    geometries: dict[int, _LayerPlaneGeometry] = {}
    ranges: dict[int, tuple[tuple[int, int], ...]] = {}
    for launch in schedule.launches:
        kernel_group_id = launch.kernel_group_index
        if kernel_group_id not in geometries:
            view = cache_context.get_temp_kernel_group_buffer(0, kernel_group_id)
            object_group_buffer = cache_context.get_temp_object_group_buffer(
                0, kernel_to_object_group[kernel_group_id]
            )
            region_offsets[kernel_group_id] = _staging_region_offset(
                view.data_ptr(), object_group_buffer
            )
            geometries[kernel_group_id] = _layer_plane_geometry(view)
        base = region_offsets[kernel_group_id]
        ranges[launch.layer_id] = tuple(
            (base + offset, length)
            for offset, length in geometries[kernel_group_id].byte_ranges(
                launch.position_in_group
            )
        )
    return ranges


@dataclass(frozen=True)
class _KernelGroupLaunchConstants:
    """Kernel arguments that depend only on the kernel group.

    Looked up once per retrieve instead of once per layer per batch.
    """

    kv_pointers: torch.Tensor
    shape_desc: PageBufferShapeDesc
    slots_per_chunk: int
    engine_kv_format: "lmcache_native.EngineKVFormat"


class _RetrieveState(Enum):
    """Lifecycle of one :class:`LayerwiseH2DRetrieve`."""

    #: Constructed; nothing published to the progress record yet.
    NOT_BEGUN = "not_begun"
    #: Generation published; some scheduled layers may still be unlaunched.
    IN_PROGRESS = "in_progress"
    #: Every scheduled layer has been launched and published.
    COMPLETE = "complete"
    #: Failure published; no further layer may be launched.
    FAILED = "failed"


class LayerwiseH2DRetrieve:
    """One layerwise H2D retrieve, launched one scheduled layer at a time.

    Splits the whole-retrieve loop into three phases so a caller can copy each
    layer only once it has actually arrived in host memory:

    1. :meth:`begin` does the per-batch setup once and publishes the retrieve
       generation to the shared progress record.
    2. :meth:`launch_layer` copies exactly one layer -- the next one in
       schedule order -- then records its completion event and advances the
       watermark, in that order.
    3. :meth:`mark_failed` publishes a failure so every worker waiting on this
       retrieve wakes with an error instead of hanging.

    Layers must be launched in :class:`LayerwiseSchedule` order. Launches share
    ``cache_context.stream`` and the worker's wait is a watermark over schedule
    ordinals, so launching out of order would make an earlier layer look ready
    before its copy was queued.

    With :attr:`LayerStaging.PER_LAYER` (the default), ``launch_layer(N)``
    copies only layer ``N``'s bytes from the host objects, at that moment, so
    the caller may launch layer ``N`` as soon as it has landed even while later
    layers of the same objects are still being written. The objects are read
    from ``objects`` at each launch, so one swapped in between layers is the
    one the next layer copies from.

    On a CUDA (or ROCm) cache context whose native extension supports layer
    subranges, a per-layer launch is one native call: every batch's range
    copies and layer kernel go to ``execute_object_group_transfer`` as one
    plan, in the same stream order as the per-call path. Otherwise each copy
    and kernel is its own call.

    Not thread-safe: all calls must come from one thread.
    """

    def __init__(
        self,
        cache_context: BaseCacheContext,
        block_ids_gpu: list[torch.Tensor],
        objects: MemoryObjectLookup,
        skip_first_n_tokens: int,
        schedule: LayerwiseSchedule,
        progress: LayerProgressRecord,
        event_pool: DaemonLayerLaunchEventPool,
        retrieve_generation: int,
        *,
        staging: LayerStaging = LayerStaging.PER_LAYER,
    ) -> None:
        """Bind one retrieve's state without touching the device or the record.

        Args:
            cache_context: Registered worker cache context on the daemon.
            block_ids_gpu: Staged GPU block-id tensors per kernel group.
            objects: The retrieve's memory objects by object group and chunk,
                ``None`` for skipped prefix chunks. Every other position must
                hold an object by :meth:`begin`, and at every launch.
            skip_first_n_tokens: Tokens to skip at the start of the retrieve
                range.
            schedule: Global per-layer launch order for this layout.
            progress: Shared progress record for this worker instance.
            event_pool: IPC events recorded after each ordinal on
                ``cache_context.stream``.
            retrieve_generation: Generation the worker waits on for this
                retrieve. Distinct from any transport fetch generation.
            staging: How host bytes reach GPU staging. Defaults to
                :attr:`LayerStaging.PER_LAYER`, the only mode that is correct
                while later layers may still be arriving.

        Raises:
            ValueError: If ``retrieve_generation`` is not positive, since ``0``
                means "no retrieve" to the worker waiter.
        """
        if retrieve_generation <= 0:
            raise ValueError(
                f"retrieve_generation must be positive, got {retrieve_generation}"
            )
        self._cache_context = cache_context
        self._block_ids_gpu = block_ids_gpu
        self._objects = objects
        self._skip_first_n_tokens = skip_first_n_tokens
        self._schedule = schedule
        self._progress = progress
        self._event_pool = event_pool
        self._retrieve_generation = retrieve_generation
        self._staging = staging
        self._state = _RetrieveState.NOT_BEGUN
        #: Index into ``schedule.launches`` of the next layer to launch.
        self._next_ordinal = 0
        #: Per-object-group batch setup; filled once by :meth:`begin`.
        self._batch_descriptors_by_object_group: dict[
            int, tuple[_LayerwiseBatchDescriptor, ...]
        ] = {}
        #: Kernel group index -> owning object group index; filled by begin.
        self._kernel_to_object_group: dict[int, int] = {}
        #: Kernel-group-invariant kernel arguments; filled by begin.
        self._launch_constants: dict[int, _KernelGroupLaunchConstants] = {}
        #: Per-layer byte ranges per kernel group; filled by begin, and only
        #: for :attr:`LayerStaging.PER_LAYER`.
        self._plane_geometry: dict[int, _LayerPlaneGeometry] = {}
        #: Kernel group index -> native plan spec; filled by begin only when
        #: a per-layer launch runs as one native plan. Empty means per-call.
        self._native_kernel_group_specs: dict[int, KernelGroupSpec] = {}
        #: (object group, batch start) pairs staged whole; only used for
        #: :attr:`LayerStaging.WHOLE_OBJECT`: per object group, the start index
        #: of the batch whose objects its staging slots hold now.
        self._resident_batch: dict[int, int] = {}

    def begin(self) -> None:
        """Do the once-per-retrieve setup and publish the retrieve generation.

        Batch descriptors, per-kernel-group kernel arguments and (for per-layer
        staging) per-layer byte ranges are all computed here, before the
        generation is published, so a setup failure leaves the progress record
        untouched -- the caller decides how to report it, matching the
        pre-split behaviour.

        Raises:
            RuntimeError: If :meth:`begin` was already called.
            ValueError: If a batch contains null memory objects on H2D; or,
                with per-layer staging, if any memory object is a GDS object
                or a staging view has an unsupported layout.
        """
        if self._state is not _RetrieveState.NOT_BEGUN:
            raise RuntimeError(
                f"retrieve generation {self._retrieve_generation} already began"
            )
        cache_context = self._cache_context
        memory_objs_by_group = self._objects.by_group()
        self._kernel_to_object_group = _kernel_group_to_object_group(cache_context)
        self._batch_descriptors_by_object_group = _build_layerwise_batch_descriptors(
            cache_context,
            self._block_ids_gpu,
            memory_objs_by_group,
            self._skip_first_n_tokens,
        )
        kernel_groups = {
            launch.kernel_group_index for launch in self._schedule.launches
        }
        self._launch_constants = {
            kernel_group_id: _KernelGroupLaunchConstants(
                kv_pointers=cache_context.get_kernel_group_kv_pointers(kernel_group_id),
                shape_desc=cache_context.get_shape_desc(kernel_group_id),
                slots_per_chunk=cache_context.get_slots_per_chunk_in_sw(
                    kernel_group_id
                ),
                engine_kv_format=cache_context.get_engine_kv_format(kernel_group_id),
            )
            for kernel_group_id in kernel_groups
        }
        if self._staging is LayerStaging.PER_LAYER:
            if any(
                isinstance(
                    memory_objs_by_group[object_group_id][
                        descriptor.start_object_idx + chunk_idx
                    ],
                    GDSMemoryObject,
                )
                for object_group_id, descriptors in (
                    self._batch_descriptors_by_object_group.items()
                )
                for descriptor in descriptors
                for chunk_idx in range(len(descriptor.object_group_buffers))
            ):
                raise ValueError(
                    "per-layer staging cannot read GDS memory objects; "
                    "use LayerStaging.WHOLE_OBJECT for complete objects"
                )
            self._plane_geometry = {
                kernel_group_id: _layer_plane_geometry(
                    cache_context.get_temp_kernel_group_buffer(0, kernel_group_id)
                )
                for kernel_group_id in kernel_groups
            }
            if _HAS_NATIVE_LAYER_LAUNCHES and cache_context.device.type == "cuda":
                self._native_kernel_group_specs = {
                    kernel_group_id: self._kernel_group_spec(kernel_group_id)
                    for kernel_group_id in kernel_groups
                }
        self._progress.begin_retrieve(self._retrieve_generation)
        self._state = _RetrieveState.IN_PROGRESS

    def launch_layer(self, layer_id: int) -> None:
        """Copy one layer to the GPU and publish that it has landed.

        Records the layer's completion event on the transfer stream *before*
        advancing the watermark, so a worker that observes the watermark never
        waits on an event that was not yet recorded. On any failure the
        retrieve is marked failed before the exception propagates.

        Args:
            layer_id: Global layer index. Must be the next layer in schedule
                order.

        Raises:
            RuntimeError: If :meth:`begin` has not run, the retrieve already
                failed, or every scheduled layer was already launched.
            ValueError: If ``layer_id`` is not the next layer in schedule order.
                The retrieve is left unchanged so the caller can report it.
        """
        if self._state is not _RetrieveState.IN_PROGRESS:
            raise RuntimeError(
                f"cannot launch layer {layer_id}: retrieve generation "
                f"{self._retrieve_generation} is {self._state.value}"
            )
        expected = self._schedule.launches[self._next_ordinal]
        if layer_id != expected.layer_id:
            raise ValueError(
                f"expected layer {expected.layer_id} next in schedule order, "
                f"got {layer_id}"
            )
        ordinal = self._next_ordinal
        try:
            self._launch_scheduled(expected)
            self._event_pool.record_ordinal(ordinal, self._cache_context.stream)
            self._progress.report_launch_recorded(ordinal + 1)
        except Exception:
            self.mark_failed()
            raise
        self._next_ordinal += 1
        if self._next_ordinal == self._schedule.launch_count():
            self._state = _RetrieveState.COMPLETE

    def mark_failed(self) -> None:
        """Publish that this retrieve failed, waking every worker waiter.

        Safe to call in any state and more than once. The record's current
        generation decides what happens:

        - older than this retrieve's (never published): this generation is
          published first, so the failure is attributed to this retrieve
          rather than to whatever the record last held;
        - equal: the failure flag is set;
        - newer: nothing is written. A newer retrieve owns the record, and a
          late failure from this one must neither fail nor rewind it.

        Marking a completed retrieve failed is allowed and conservative: a
        waiter that has not yet returned falls back to a full load instead of
        trusting it.

        Before setting the flag, waits for every copy this retrieve already
        queued on the transfer stream. The worker reports a flagged retrieve's
        blocks to vLLM for recompute, so no copy may still be landing in them.

        Assumes this process is the record's only writer, which holds because
        the MP server serialises retrieves per worker.
        """
        copies_queued = self._state is not _RetrieveState.NOT_BEGUN
        self._state = _RetrieveState.FAILED
        if self._progress.read().generation > self._retrieve_generation:
            return
        if copies_queued:
            self._cache_context.stream.synchronize()
        self._progress.fail_retrieve(self._retrieve_generation)

    def wait_for_copies(self) -> None:
        """Block until every copy this retrieve has queued has finished.

        After it returns, no copy of this retrieve still reads its host
        memory objects, so the caller may release them. Safe in any state;
        a retrieve that never began has nothing to wait for.
        """
        if self._state is not _RetrieveState.NOT_BEGUN:
            self._cache_context.stream.synchronize()

    def _launch_scheduled(self, launch: LayerLaunch) -> None:
        """Stage ``launch``'s bytes to GPU staging and run its layer kernel.

        Args:
            launch: The scheduled layer to transfer.
        """
        kernel_group_id = launch.kernel_group_index
        object_group_id = self._kernel_to_object_group[kernel_group_id]
        if kernel_group_id in self._native_kernel_group_specs:
            self._run_layer_plan(launch, object_group_id)
            return
        constants = self._launch_constants[kernel_group_id]
        layer_ranges: tuple[tuple[int, int], ...] = ()
        if self._staging is LayerStaging.PER_LAYER:
            layer_ranges = self._plane_geometry[kernel_group_id].byte_ranges(
                launch.position_in_group
            )
        for batch_descriptor in self._batch_descriptors_by_object_group[
            object_group_id
        ]:
            launch_params = batch_descriptor.launch_params_by_kernel_group[
                kernel_group_id
            ]
            if self._staging is LayerStaging.PER_LAYER:
                self._stage_layer(
                    object_group_id, batch_descriptor, launch_params, layer_ranges
                )
            else:
                self._stage_whole_batch(object_group_id, batch_descriptor)
            device_ops.multi_layer_block_kv_transfer(
                constants.kv_pointers,
                list(launch_params.tmp_gpu_buffer_data_ptrs),
                launch_params.block_ids_curr_batch,
                self._cache_context.device,
                lmcache_native.TransferDirection.H2D,
                constants.shape_desc,
                constants.slots_per_chunk,
                constants.engine_kv_format,
                launch_params.recalculated_skip_blocks,
                launch.position_in_group,
                1,
            )

    def _run_layer_plan(self, launch: LayerLaunch, object_group_id: int) -> None:
        """Stage one layer of every batch and run its kernels in one native call.

        Builds, per batch, the layer's range copies and its one-layer kernel
        launch, then hands the whole layer to ``execute_object_group_transfer``,
        which issues it on the current stream with the GIL released once. The
        executor stages a batch before its kernel and finishes that batch
        before the next, so batches can share the staging slots. Every object
        is read and checked before anything is queued.

        Args:
            launch: The scheduled layer to transfer.
            object_group_id: The object group owning ``launch``'s kernel group.

        Raises:
            ValueError: If a position has no object, or an object cannot be
                staged by byte range.
        """
        kernel_group_id = launch.kernel_group_index
        layer_ranges = self._plane_geometry[kernel_group_id].byte_ranges(
            launch.position_in_group
        )
        batch_steps: list[BatchStep] = []
        for batch_descriptor in self._batch_descriptors_by_object_group[
            object_group_id
        ]:
            launch_params = batch_descriptor.launch_params_by_kernel_group[
                kernel_group_id
            ]
            memory_objs = self._batch_objects(object_group_id, batch_descriptor)
            staging: list[StagingCopy] = []
            for chunk_idx, memory_obj in enumerate(memory_objs):
                region_offset = launch_params.staging_region_offsets[chunk_idx]
                staging.extend(
                    build_h2d_range_staging_copies(
                        memory_obj,
                        batch_descriptor.object_group_buffers[chunk_idx],
                        [(region_offset + offset, n) for offset, n in layer_ranges],
                    )
                )
            layer_launch = device_ops.LaunchVar(
                0,
                launch_params.block_ids_offset,
                launch_params.block_ids_curr_batch.shape[0],
                len(memory_objs),
                launch_params.recalculated_skip_blocks,
                layer_offset=launch.position_in_group,
                n_layers=1,
            )
            batch_steps.append(device_ops.BatchStep(staging, [layer_launch]))
        if not batch_steps:
            return
        device_ops.execute_object_group_transfer(
            lmcache_native.TransferDirection.H2D,
            self._cache_context.device,
            LazyMemoryAllocator.PIN_CHUNK_SIZE,
            [self._native_kernel_group_specs[kernel_group_id]],
            batch_steps,
        )

    def _kernel_group_spec(self, kernel_group_id: int) -> KernelGroupSpec:
        """Build the native plan's spec for one kernel group, once per retrieve.

        Args:
            kernel_group_id: The kernel group to describe.

        Returns:
            The kernel arguments that do not vary by batch or layer, with every
            staging slot's kernel-group view and the group's block ids.
        """
        cache_context = self._cache_context
        block_ids = self._block_ids_gpu[kernel_group_id]
        return device_ops.KernelGroupSpec(
            cache_context.get_kernel_group_kv_pointers(kernel_group_id).data_ptr(),
            [
                cache_context.get_temp_kernel_group_buffer(
                    slot, kernel_group_id
                ).data_ptr()
                for slot in range(cache_context.max_batch_size)
            ],
            cache_context.get_shape_desc(kernel_group_id),
            cache_context.get_slots_per_chunk_in_sw(kernel_group_id),
            cache_context.get_engine_kv_format(kernel_group_id),
            block_ids.data_ptr(),
            block_ids.numel(),
        )

    def _stage_layer(
        self,
        object_group_id: int,
        batch_descriptor: _LayerwiseBatchDescriptor,
        launch_params: _LayerwiseKernelLaunchParams,
        layer_ranges: tuple[tuple[int, int], ...],
    ) -> None:
        """Copy one layer's planes of every object in a batch to GPU staging.

        Each kernel group's staging view sits at a fixed offset inside its
        object group's staging buffer, and a memory object is laid out
        byte-for-byte like that buffer, so the same offsets address both.

        Args:
            object_group_id: The object group the batch belongs to.
            batch_descriptor: The batch whose objects are staged.
            launch_params: The kernel group's launch inputs for this batch.
            layer_ranges: The layer's ``(offset, length)`` byte ranges inside
                the kernel group's staging region.
        """
        memory_objs = self._batch_objects(object_group_id, batch_descriptor)
        for chunk_idx, memory_obj in enumerate(memory_objs):
            object_group_buffer = batch_descriptor.object_group_buffers[chunk_idx]
            region_offset = launch_params.staging_region_offsets[chunk_idx]
            for offset, length in layer_ranges:
                lmcache_memcpy_async_h2d_range(
                    memory_obj, object_group_buffer, region_offset + offset, length
                )

    def _stage_whole_batch(
        self,
        object_group_id: int,
        batch_descriptor: _LayerwiseBatchDescriptor,
    ) -> None:
        """Copy every object of a batch to GPU staging unless already there.

        All batches of an object group share its staging slots, so a batch
        staged for an earlier layer is overwritten once a later batch of the
        same group is staged, and must be staged again.

        Args:
            object_group_id: The object group the batch belongs to.
            batch_descriptor: The batch whose objects are staged.
        """
        start = batch_descriptor.start_object_idx
        if self._resident_batch.get(object_group_id) == start:
            return
        memory_objs = self._batch_objects(object_group_id, batch_descriptor)
        for chunk_idx, memory_obj in enumerate(memory_objs):
            lmcache_memcpy_async_h2d(
                memory_obj, batch_descriptor.object_group_buffers[chunk_idx]
            )
        self._resident_batch[object_group_id] = start

    def _batch_objects(
        self, object_group_id: int, batch_descriptor: _LayerwiseBatchDescriptor
    ) -> list[MemoryObj]:
        """Read a batch's objects from the lookup, as they are now.

        Args:
            object_group_id: The object group the batch belongs to.
            batch_descriptor: The batch whose objects to read.

        Returns:
            One object per staging buffer, in chunk order.

        Raises:
            ValueError: If a position the batch stages has no object.
        """
        start = batch_descriptor.start_object_idx
        memory_objs: list[MemoryObj] = []
        for chunk_id in range(
            start, start + len(batch_descriptor.object_group_buffers)
        ):
            memory_obj = self._objects.get(object_group_id, chunk_id)
            if memory_obj is None:
                raise ValueError(
                    f"no memory object for chunk {chunk_id} of object group "
                    f"{object_group_id} at launch; cannot perform H2D copy"
                )
            memory_objs.append(memory_obj)
        return memory_objs
