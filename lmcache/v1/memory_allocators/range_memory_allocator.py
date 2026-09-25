# SPDX-License-Identifier: Apache-2.0
"""Allocator confined to one fixed byte range of a slab it does not own."""

# Standard
from typing import List, Optional, Union

# Third Party
import torch

# First Party
from lmcache.utils import get_size_bytes
from lmcache.v1.memory_management import (
    AddressManager,
    MemoryAllocatorInterface,
    MemoryFormat,
    MemoryObj,
    MemoryObjMetadata,
    TensorMemoryObj,
)


class RangeMemoryAllocator(MemoryAllocatorInterface):
    """Allocates tensor objects from ``[start, start + size)`` of a slab.

    Object addresses (``meta.address``) are offsets from the start of the
    *slab*, not of the range, so an object from this allocator looks exactly
    like one from an allocator over the whole slab. Transports that turn
    ``meta.address`` into a slab offset keep working unchanged.

    The slab is borrowed: :meth:`close` does not free it, and the caller must
    keep it alive for as long as any object from this allocator is in use.
    Only tensor formats are supported; ``BINARY_BUFFER`` has no slab address.

    Thread-safe: the underlying :class:`AddressManager` serializes access.
    """

    def __init__(
        self,
        slab: torch.Tensor,
        start: int,
        size: int,
        align_bytes: int = AddressManager.ALIGN_BYTES,
    ) -> None:
        """Create an allocator over one range of ``slab``.

        Args:
            slab: The whole slab. It is viewed as flat bytes.
            start: Byte offset of the range from the start of the slab.
            size: Length of the range in bytes.
            align_bytes: Allocation alignment. ``start`` and ``size`` must be
                multiples of it.

        Raises:
            ValueError: If the range is empty, negative, unaligned, or does
                not fit inside the slab.
        """
        self._slab = slab.view(torch.uint8).flatten()
        if start < 0 or size <= 0:
            raise ValueError(
                f"range must have start >= 0 and size > 0, got start={start}, "
                f"size={size}"
            )
        if start + size > self._slab.numel():
            raise ValueError(
                f"range [{start}, {start + size}) does not fit in a slab of "
                f"{self._slab.numel()} bytes"
            )
        if start % align_bytes != 0 or size % align_bytes != 0:
            raise ValueError(
                f"range start ({start}) and size ({size}) must be multiples of "
                f"align_bytes ({align_bytes})"
            )
        self._start = start
        self._size = size
        self._address_manager = AddressManager(size, align_bytes)

    def allocate(
        self,
        shapes: Union[torch.Size, list[torch.Size]],
        dtypes: Union[torch.dtype, list[torch.dtype]],
        fmt: MemoryFormat = MemoryFormat.KV_2LTD,
        allocator_type: Optional[str] = None,
    ) -> Optional[MemoryObj]:
        """Allocate one object inside the range.

        Args:
            shapes: Logical tensor shape or shapes.
            dtypes: Logical tensor dtype or dtypes.
            fmt: Memory format recorded in the object's metadata.
            allocator_type: Unused; accepted for interface compatibility.

        Returns:
            The object, or ``None`` if the range has no room for it.

        Raises:
            ValueError: If ``fmt`` is ``BINARY_BUFFER``.
        """
        objs = self.batched_allocate(shapes, dtypes, 1, fmt, allocator_type)
        return objs[0] if objs is not None else None

    def batched_allocate(
        self,
        shapes: Union[torch.Size, list[torch.Size]],
        dtypes: Union[torch.dtype, list[torch.dtype]],
        batch_size: int,
        fmt: MemoryFormat = MemoryFormat.KV_2LTD,
        allocator_type: Optional[str] = None,
    ) -> Optional[List[MemoryObj]]:
        """Allocate ``batch_size`` equal-sized objects inside the range.

        All or nothing: either every object is allocated or none is.

        Args:
            shapes: Logical tensor shape or shapes of each object.
            dtypes: Logical tensor dtype or dtypes of each object.
            batch_size: Number of objects.
            fmt: Memory format recorded in each object's metadata.
            allocator_type: Unused; accepted for interface compatibility.

        Returns:
            The objects, or ``None`` if the range has no room for all of them.

        Raises:
            ValueError: If ``fmt`` is ``BINARY_BUFFER``.
        """
        if fmt == MemoryFormat.BINARY_BUFFER:
            raise ValueError("RangeMemoryAllocator does not allocate BINARY_BUFFER")
        shapes, dtypes = self._adapt_shapes_and_dtypes(shapes, dtypes)
        raw_size = get_size_bytes(shapes, dtypes)
        aligned_size = self._address_manager.compute_aligned_size(raw_size)
        try:
            blocks = self._address_manager.batched_allocate(aligned_size, batch_size)
        except RuntimeError:
            return None

        objs: List[MemoryObj] = []
        for local_address, _ in blocks:
            address = self._start + local_address
            objs.append(
                TensorMemoryObj(
                    raw_data=self._slab[address : address + aligned_size],
                    metadata=MemoryObjMetadata(
                        shapes[0],
                        dtypes[0],
                        address,
                        aligned_size,
                        1,
                        0,
                        fmt,
                        shapes=shapes,
                        dtypes=dtypes,
                    ),
                    parent_allocator=self,
                )
            )
        return objs

    def free(self, memory_obj: MemoryObj, allocator_type: Optional[str] = None) -> None:
        """Return one object's memory to the range.

        Freeing an already-invalidated object does nothing.

        Args:
            memory_obj: An object allocated by this allocator.
            allocator_type: Unused; accepted for interface compatibility.

        Raises:
            ValueError: If ``memory_obj`` does not lie inside this range.
        """
        if not memory_obj.is_valid():
            return
        if not self.contains(memory_obj):
            raise ValueError(
                f"object at slab offset {memory_obj.meta.address} is outside "
                f"[{self._start}, {self._start + self._size})"
            )
        self._address_manager.free(
            memory_obj.meta.address - self._start, memory_obj.meta.phy_size
        )
        memory_obj.invalidate()

    def batched_free(
        self,
        memory_objs: List[MemoryObj],
        allocator_type: Optional[str] = None,
        update_stats: bool = True,
    ) -> None:
        """Return several objects' memory to the range.

        Args:
            memory_objs: Objects allocated by this allocator.
            allocator_type: Unused; accepted for interface compatibility.
            update_stats: Unused; this allocator reports no global stats.

        Raises:
            ValueError: If any object does not lie inside this range. Objects
                before it in the list have already been freed.
        """
        for memory_obj in memory_objs:
            self.free(memory_obj)

    def contains(self, memory_obj: MemoryObj) -> bool:
        """Report whether ``memory_obj``'s memory lies inside this range.

        Decided by the object's data pointer, so it never mistakes an object
        from another allocator (including ``BINARY_BUFFER`` objects, whose
        ``meta.address`` is not a slab offset) for one of its own.

        Args:
            memory_obj: Any memory object.

        Returns:
            ``True`` if the object's first byte is inside the range.
        """
        base = self._slab.data_ptr() + self._start
        return base <= memory_obj.data_ptr < base + self._size

    def get_used_bytes(self) -> int:
        """Return the bytes currently allocated inside the range.

        Returns:
            Allocated bytes, including alignment padding.
        """
        return self._size - self._address_manager.get_free_size()

    def get_size(self) -> int:
        """Return the length of the range.

        Returns:
            Size in bytes.
        """
        return self._size

    def memcheck(self) -> bool:
        """Check the range's free-list bookkeeping.

        Returns:
            ``True`` if the bookkeeping is consistent.
        """
        return self._address_manager.check_consistency()

    def __str__(self) -> str:
        """Return the allocator name."""
        return "RangeMemoryAllocator"
