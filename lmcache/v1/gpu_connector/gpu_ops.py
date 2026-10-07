# SPDX-License-Identifier: Apache-2.0
# Standard
from typing import Sequence

# Third Party
import torch

# First Party
from lmcache import device_ops
from lmcache.v1.gpu_connector.gds_context import SlabDirection, get_gds_context
from lmcache.v1.memory_allocators.lazy_memory_allocator import LazyMemoryAllocator
from lmcache.v1.memory_management import GDSMemoryObject, MemoryObj
from lmcache.v1.platform.ops_types import StagingCopy
import lmcache.lmcache_native as lmcache_native

#: Alignment that keeps ``device_ops.lmcache_memcpy_async`` from splitting a
#: copy: its pieces end at multiples of the alignment past the host offset.
_SINGLE_COPY_ALIGNMENT = 1 << 62


def _range_source_tensor(memory_obj: MemoryObj) -> torch.Tensor:
    """Return the host tensor a byte-range staging copy reads.

    Raises:
        ValueError: If ``memory_obj`` is a GDS object (those transfer whole
            objects only) or has no backing tensor.
    """
    if isinstance(memory_obj, GDSMemoryObject):
        raise ValueError("GDS memory objects cannot be staged by byte range")
    src_tensor = memory_obj.raw_tensor
    if src_tensor is None:
        raise ValueError(
            "memory_obj.raw_tensor is None; ensure the MemoryObj has been allocated."
        )
    return src_tensor


def _check_byte_range(
    memory_obj: MemoryObj, gpu_buffer: torch.Tensor, byte_offset: int, nbytes: int
) -> None:
    """Reject a byte range that is empty, negative, or past either buffer.

    Raises:
        ValueError: If the range is invalid for ``memory_obj`` or ``gpu_buffer``.
    """
    if byte_offset < 0 or nbytes <= 0:
        raise ValueError(f"invalid byte range: offset={byte_offset}, nbytes={nbytes}")
    end = byte_offset + nbytes
    if end > memory_obj.get_size() or end > gpu_buffer.nbytes:
        raise ValueError(
            f"byte range [{byte_offset}, {end}) exceeds memory_obj nbytes="
            f"{memory_obj.get_size()} or gpu_buffer nbytes={gpu_buffer.nbytes}"
        )


# Helper functions
def lmcache_memcpy_async_h2d(
    memory_obj: MemoryObj,
    gpu_buffer: torch.Tensor,
):
    """Helper function to copy memory object allocated by different
    allocators to GPU buffer.

    This function is non-blocking and won't do stream synchronization.

    :param MemoryObj memory_obj: The memory object to be copied.
    :param torch.Tensor gpu_buffer: The GPU buffer to copy the data to.
    """
    if isinstance(memory_obj, GDSMemoryObject):
        get_gds_context().transfer_async(memory_obj, gpu_buffer, SlabDirection.READ)
        return
    src_tensor = memory_obj.raw_tensor
    if src_tensor is None:
        raise ValueError(
            "memory_obj.raw_tensor is None; ensure the MemoryObj has been allocated."
        )
    mem_obj_size = memory_obj.get_size()
    if mem_obj_size != gpu_buffer.nbytes:
        raise ValueError(
            f"Size mismatch: memory_obj nbytes={mem_obj_size}, "
            f"gpu_buffer nbytes={gpu_buffer.nbytes}"
        )
    if isinstance(memory_obj.parent(), LazyMemoryAllocator):
        device_ops.lmcache_memcpy_async(
            gpu_buffer.data_ptr(),
            memory_obj.data_ptr,
            mem_obj_size,
            lmcache_native.TransferDirection.H2D,
            memory_obj.meta.address,
            LazyMemoryAllocator.PIN_CHUNK_SIZE,
        )
    else:
        gpu_buffer.view(torch.uint8).copy_(
            src_tensor.view(torch.uint8)[:mem_obj_size], non_blocking=True
        )


def lmcache_memcpy_async_h2d_range(
    memory_obj: MemoryObj,
    gpu_buffer: torch.Tensor,
    byte_offset: int,
    nbytes: int,
) -> None:
    """Copy one byte range of a memory object into the same range of a GPU buffer.

    The partial counterpart of :func:`lmcache_memcpy_async_h2d`: bytes
    ``[byte_offset, byte_offset + nbytes)`` of the memory object land at the
    same offsets of ``gpu_buffer``. Used to stage a single layer of an object
    whose other layers may still be arriving. Non-blocking; runs on the
    current stream and does not synchronize. For a CUDA ``gpu_buffer``, its
    device must be the current device.

    Args:
        memory_obj: Host memory object to read from.
        gpu_buffer: Contiguous device buffer laid out byte-for-byte like the
            memory object (for example an object-group staging buffer).
        byte_offset: Start of the range, in bytes, in both buffers.
        nbytes: Length of the range in bytes.

    Raises:
        ValueError: If ``memory_obj`` is a GDS object (those transfer whole
            objects only), has no backing tensor, or if the range is empty,
            negative, or runs past the end of either buffer.
    """
    src_tensor = _range_source_tensor(memory_obj)
    _check_byte_range(memory_obj, gpu_buffer, byte_offset, nbytes)
    end = byte_offset + nbytes
    if isinstance(memory_obj.parent(), LazyMemoryAllocator):
        # The host offset is the allocator's virtual offset of the range start;
        # the native copy splits at pin-chunk boundaries relative to it.
        device_ops.lmcache_memcpy_async(
            gpu_buffer.data_ptr() + byte_offset,
            memory_obj.data_ptr + byte_offset,
            nbytes,
            lmcache_native.TransferDirection.H2D,
            memory_obj.meta.address + byte_offset,
            LazyMemoryAllocator.PIN_CHUNK_SIZE,
        )
    elif gpu_buffer.is_cuda:
        # One raw cudaMemcpyAsync, issued without the GIL: per-layer staging
        # issues one call per plane per chunk, and slicing two tensors for
        # copy_ costs about 2.5x the CPU time. The native call copies on the
        # current device's stream, so the caller must have made gpu_buffer's
        # device current (retrieve runs under torch_dev.device(...)).
        device_ops.lmcache_memcpy_async(
            gpu_buffer.data_ptr() + byte_offset,
            src_tensor.data_ptr() + byte_offset,
            nbytes,
            lmcache_native.TransferDirection.H2D,
            0,
            _SINGLE_COPY_ALIGNMENT,
        )
    else:
        gpu_buffer.view(torch.uint8)[byte_offset:end].copy_(
            src_tensor.view(torch.uint8)[byte_offset:end], non_blocking=True
        )


def lmcache_memcpy_async_d2h(
    gpu_buffer: torch.Tensor,
    memory_obj: MemoryObj,
):
    """Helper function to copy memory object allocated by different
    allocators from GPU buffer.

    This function is non-blocking and won't do stream synchronization.

    :param torch.Tensor gpu_buffer: The GPU buffer to copy the data from.
    :param MemoryObj memory_obj: The memory object to be copied to.
    """
    if isinstance(memory_obj, GDSMemoryObject):
        get_gds_context().transfer_async(memory_obj, gpu_buffer, SlabDirection.WRITE)
        return
    dst_tensor = memory_obj.raw_tensor
    if dst_tensor is None:
        raise ValueError(
            "memory_obj.raw_tensor is None; ensure the MemoryObj has been allocated."
        )
    mem_obj_size = memory_obj.get_size()
    if mem_obj_size != gpu_buffer.nbytes:
        raise ValueError(
            f"Size mismatch: memory_obj nbytes={mem_obj_size}, "
            f"gpu_buffer nbytes={gpu_buffer.nbytes}"
        )
    if isinstance(memory_obj.parent(), LazyMemoryAllocator):
        device_ops.lmcache_memcpy_async(
            memory_obj.data_ptr,
            gpu_buffer.data_ptr(),
            mem_obj_size,
            lmcache_native.TransferDirection.D2H,
            memory_obj.meta.address,
            LazyMemoryAllocator.PIN_CHUNK_SIZE,
        )
    else:
        dst_tensor.view(torch.uint8)[:mem_obj_size].copy_(
            gpu_buffer.view(torch.uint8), non_blocking=True
        )


def build_staging_copies(
    memory_objs: Sequence[MemoryObj],
    gpu_buffers: Sequence[torch.Tensor],
    is_h2d: bool,
) -> list[StagingCopy]:
    """Build native ``StagingCopy`` descriptors for one batch of lazy objects.

    The H2D/D2H direction decides which side is source vs. destination; the host
    side is always the lazy memory object. Callers must ensure every object is
    lazy-allocator-backed.

    Args:
        memory_objs: Lazy-allocator memory objects, one per chunk in the batch.
        gpu_buffers: GPU staging buffers, aligned element-wise with
            ``memory_objs``.
        is_h2d: True for retrieve (CPU->GPU), False for store (GPU->CPU).

    Returns:
        One ``device_ops.StagingCopy`` per object, in input order.

    Raises:
        ValueError: If an object has not been allocated (``raw_tensor`` is None)
            or its size does not match its GPU buffer.
    """
    copies: list[StagingCopy] = []
    for memory_obj, gpu_buffer in zip(memory_objs, gpu_buffers, strict=True):
        if memory_obj.raw_tensor is None:
            raise ValueError(
                "memory_obj.raw_tensor is None; ensure the MemoryObj has been "
                "allocated."
            )
        mem_obj_size = memory_obj.get_size()
        if mem_obj_size != gpu_buffer.nbytes:
            raise ValueError(
                f"Size mismatch: memory_obj nbytes={mem_obj_size}, "
                f"gpu_buffer nbytes={gpu_buffer.nbytes}"
            )
        host_ptr = memory_obj.data_ptr
        gpu_ptr = gpu_buffer.data_ptr()
        host_offset = memory_obj.meta.address
        if is_h2d:
            copies.append(
                device_ops.StagingCopy(gpu_ptr, host_ptr, mem_obj_size, host_offset)
            )
        else:
            copies.append(
                device_ops.StagingCopy(host_ptr, gpu_ptr, mem_obj_size, host_offset)
            )
    return copies


def build_h2d_range_staging_copies(
    memory_obj: MemoryObj,
    gpu_buffer: torch.Tensor,
    byte_ranges: Sequence[tuple[int, int]],
) -> list[StagingCopy]:
    """Build native ``StagingCopy`` descriptors for byte ranges of one object.

    The plan counterpart of :func:`lmcache_memcpy_async_h2d_range`: each
    ``(offset, length)`` range of ``memory_obj`` lands at the same offsets of
    ``gpu_buffer``. The descriptors are meant for
    ``device_ops.execute_object_group_transfer`` with
    ``LazyMemoryAllocator.PIN_CHUNK_SIZE`` as its host alignment, so a lazy
    object's copy splits at pin-chunk boundaries; any other host object
    carries host offset 0, which splits only a range longer than a pin chunk.

    Args:
        memory_obj: Host memory object to read from.
        gpu_buffer: Contiguous device buffer laid out byte-for-byte like the
            memory object (for example an object-group staging buffer).
        byte_ranges: ``(offset, length)`` pairs, in bytes, in both buffers.

    Returns:
        One ``device_ops.StagingCopy`` per range, in input order.

    Raises:
        ValueError: If ``memory_obj`` is a GDS object or has no backing
            tensor, or if a range is empty, negative, or runs past the end of
            either buffer.
    """
    src_tensor = _range_source_tensor(memory_obj)
    is_lazy = isinstance(memory_obj.parent(), LazyMemoryAllocator)
    host_ptr = memory_obj.data_ptr if is_lazy else src_tensor.data_ptr()
    host_base = memory_obj.meta.address if is_lazy else 0
    gpu_ptr = gpu_buffer.data_ptr()
    copies: list[StagingCopy] = []
    for offset, length in byte_ranges:
        _check_byte_range(memory_obj, gpu_buffer, offset, length)
        copies.append(
            device_ops.StagingCopy(
                gpu_ptr + offset,
                host_ptr + offset,
                length,
                host_base + offset if is_lazy else 0,
            )
        )
    return copies
