# SPDX-License-Identifier: Apache-2.0
"""CPU pinned-DRAM L1 memory manager."""

# Standard
from multiprocessing import shared_memory

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.api import L1BackendType, MemoryLayoutDesc
from lmcache.v1.distributed.config import L1MemoryManagerConfig
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.internal_api import (
    GENERAL_L1_POOL,
    L1MemoryDesc,
    L1Pool,
    MemoryGrowthPolicy,
)
from lmcache.v1.memory_allocators.lazy_memory_allocator import LazyMemoryAllocator
from lmcache.v1.memory_allocators.mixed_memory_allocator import MixedMemoryAllocator
from lmcache.v1.memory_allocators.range_memory_allocator import RangeMemoryAllocator
from lmcache.v1.memory_management import (
    MemoryAllocatorInterface,
    MemoryObj,
)

logger = init_logger(__name__)


# HELPER FUNCTIONS
def _unlink_stale_shm(shm_name: str) -> None:
    """Remove a stale LMCache shm segment if it exists."""
    normalized = shm_name.lstrip("/")
    if "/" in normalized or "\\" in normalized:
        logger.warning("Refusing to unlink invalid shm name %s", shm_name)
        return
    if not normalized.startswith("lmcache_l1_pool_"):
        return
    try:
        shm = shared_memory.SharedMemory(name=normalized, create=False)
        shm.close()
        shm.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning(
            "Failed to remove stale shm segment %s", normalized, exc_info=True
        )


def create_memory_allocator(config: L1MemoryManagerConfig) -> MemoryAllocatorInterface:
    """
    Create a memory allocator based on the provided configuration.

    Args:
        config (L1MemoryManagerConfig): Configuration for the memory manager.

    Returns:
        MemoryAllocatorInterface: An instance of a memory allocator. When
        ``config`` reserves RDMA windows, the allocator never hands out the
        first ``rdma_window_count * rdma_window_bytes`` bytes of its slab.

    Raises:
        ValueError: If RDMA windows are reserved with a lazy allocator, whose
            slab can grow after the windows are registered.
    """
    reserved_bytes = config.rdma_window_count * config.rdma_window_bytes
    if config.use_lazy and reserved_bytes > 0:
        raise ValueError(
            "RDMA windows need a fixed-size L1 slab; disable lazy allocation "
            "(--no-l1-use-lazy) or disable RDMA"
        )
    if config.use_lazy:
        logger.debug(
            "use lazy memory allocator, init size is %d bytes, "
            "final size is %d bytes, align bytes is %d bytes",
            config.init_size_in_bytes,
            config.size_in_bytes,
            config.align_bytes,
        )
        return LazyMemoryAllocator(
            config.init_size_in_bytes, config.size_in_bytes, config.align_bytes
        )
    else:
        logger.debug(
            "use mixed memory allocator, total size is %d bytes, "
            "align bytes is %d bytes",
            config.size_in_bytes,
            config.align_bytes,
        )
        shm_name = config.shm_name
        if shm_name:
            # Keep the lmcache_l1_pool_ prefix in normalized SHM names so
            # stale-segment cleanup can recognize and unlink user-provided names.
            bare = shm_name.lstrip("/")
            if not bare.startswith("lmcache_l1_pool_"):
                shm_name = f"lmcache_l1_pool_{bare}"
            _unlink_stale_shm(shm_name)
            return MixedMemoryAllocator(
                config.size_in_bytes,
                align_bytes=config.align_bytes,
                shm_name=shm_name,
                reserved_prefix_bytes=reserved_bytes,
            )
        return MixedMemoryAllocator(
            config.size_in_bytes,
            align_bytes=config.align_bytes,
            reserved_prefix_bytes=reserved_bytes,
        )


# MAIN CLASS
class L1MemoryManager:
    """
    L1MemoryManager manages the allocation and deallocation of L1 memory.

    When the config reserves RDMA windows, the first
    ``rdma_window_count * rdma_window_bytes`` bytes of the slab are split into
    that many windows, each with its own allocator. The general allocator never
    hands out memory there, so a remote writer holding one window's rkey cannot
    reach ordinary L1 objects. Memory usage reports the general pool only,
    since the windows are outside memory-pressure eviction.

    Observability metrics to emit:
    1. Memory usage
    2. Active allocations
    """

    # Class-level defaults so subclasses that build their own allocator
    # (Device-DAX) have no windows without calling this __init__.
    _windows: tuple[RangeMemoryAllocator, ...] = ()
    _window_bytes: int = 0
    _slab_ptr: int = 0

    def __init__(self, config: L1MemoryManagerConfig):
        """Create the manager and, if configured, its RDMA windows.

        Args:
            config: L1 memory configuration.

        Raises:
            ValueError: If RDMA windows are configured with a lazy allocator,
                are not aligned to ``align_bytes``, or leave no room for the
                general allocator.
        """
        self._allocator = create_memory_allocator(config)
        self._size_in_bytes = config.size_in_bytes
        self._align_bytes = config.align_bytes
        if config.rdma_window_count > 0 and isinstance(
            self._allocator, MixedMemoryAllocator
        ):
            slab = self._allocator.buffer
            self._window_bytes = config.rdma_window_bytes
            self._slab_ptr = slab.data_ptr()
            self._windows = tuple(
                RangeMemoryAllocator(
                    slab,
                    start=i * config.rdma_window_bytes,
                    size=config.rdma_window_bytes,
                    align_bytes=config.align_bytes,
                )
                for i in range(config.rdma_window_count)
            )

    def allocate(
        self,
        layout_desc: MemoryLayoutDesc,
        count: int,
        pool: L1Pool = GENERAL_L1_POOL,
    ) -> tuple[L1Error, list[MemoryObj]]:
        """
        Allocate memory objects based on the provided layout description and count.
        This function should be thread-safe

        Args:
            layout_desc (MemoryLayoutDesc): Description of the memory layout.
            count (int): Number of memory objects to allocate.
            pool (L1Pool): Where to allocate. General L1 by default; an RDMA
                window pool allocates inside that window only.

        Returns:
            tuple[L1Error, list[MemoryObj]]: Error code and list of
            allocated memory objects.
            Error code will be `L1Error.OUT_OF_MEMORY` if allocation
            fails; otherwise, it will be `L1Error.SUCCESS`.

        Raises:
            ValueError: If ``pool`` names a window this manager does not have.

        Note:
            If the allocation fails, the memory object list will be empty.
        """
        allocator = self._allocator_for(pool)
        objects = allocator.batched_allocate(
            layout_desc.shapes, layout_desc.dtypes, count
        )
        if objects is None:
            return L1Error.OUT_OF_MEMORY, []
        return L1Error.SUCCESS, objects

    def free(self, mem_objs: list[MemoryObj]) -> L1Error:
        """
        Free the provided memory objects.
        This function should be thread-safe.

        Objects may come from any pool; each returns to the allocator it came
        from.

        Args:
            mem_objs (list[MemoryObj]): List of memory objects to free.

        Returns:
            L1Error: Error code indicating the result of the operation.
            It will be `L1Error.SUCCESS` if the operation succeeds.
        """
        if not self._windows:
            self._allocator.batched_free(mem_objs)
            return L1Error.SUCCESS

        general: list[MemoryObj] = []
        per_window: dict[int, list[MemoryObj]] = {}
        for obj in mem_objs:
            pool = self.get_pool(obj)
            if pool.is_general():
                general.append(obj)
            else:
                per_window.setdefault(pool.window_index, []).append(obj)
        self._allocator.batched_free(general)
        for window_index, objs in per_window.items():
            self._windows[window_index].batched_free(objs)
        return L1Error.SUCCESS

    def get_pool(self, memory_obj: MemoryObj) -> L1Pool:
        """Return the pool ``memory_obj`` was allocated from.

        Args:
            memory_obj: An object allocated by this manager.

        Returns:
            The RDMA window pool if the object lies inside a reserved window,
            otherwise :data:`GENERAL_L1_POOL`.
        """
        if not self._windows:
            return GENERAL_L1_POOL
        offset = memory_obj.data_ptr - self._slab_ptr
        if 0 <= offset < self._window_bytes * len(self._windows):
            return L1Pool.rdma_window(offset // self._window_bytes)
        return GENERAL_L1_POOL

    def get_backend_type(self, memory_obj: MemoryObj) -> L1BackendType:
        """Return the storage medium backing ``memory_obj``.

        Args:
            memory_obj: An object allocated by this manager.

        Returns:
            ``L1BackendType.DRAM`` — the CPU tier is pinned DRAM only.
        """
        return L1BackendType.DRAM

    def get_memory_usage(self) -> tuple[int, int]:
        """
        Get the current memory usage. This function will mainly be used to support
        eviction decision.

        Returns:
            tuple[int, int]: A tuple containing used memory in bytes and total memory
            in bytes. Both cover the general pool only; reserved RDMA windows
            are excluded.

        Note:
            In the future, we may want to make a "callback" based mechanism to
            trigger eviction when the memory usage reaches a watermark.
        """

        if hasattr(self._allocator, "get_memory_usage"):
            return self._allocator.get_memory_usage()

        def get_address_manager(allocator: MemoryAllocatorInterface):
            if isinstance(allocator, MixedMemoryAllocator) and hasattr(
                allocator.pin_allocator, "address_manager"
            ):
                return allocator.pin_allocator.address_manager
            if isinstance(allocator, LazyMemoryAllocator):
                return allocator.get_address_manager()
            raise NotImplementedError(
                "get_memory_usage is not implemented for this allocator type."
            )

        address_manager = get_address_manager(self._allocator)
        free_size = address_manager.get_free_size()
        # The windows are one allocation in the general heap that is never
        # freed; leave them out of both numbers.
        total_size = address_manager.get_heap_size() - self._reserved_window_bytes()
        used_size = total_size - free_size
        return used_size, total_size

    def get_l1_memory_desc(self) -> L1MemoryDesc:
        """
        Return an L1MemoryDesc describing the underlying memory buffer.

        Returns:
            L1MemoryDesc: Pointer, size, and alignment of the L1 buffer. ``growth``
            is ``FIXED`` for :class:`MixedMemoryAllocator` and ``GROWABLE`` for
            :class:`LazyMemoryAllocator`, whose slab may still expand.

        Raises:
            NotImplementedError: If the allocator type does not support this operation.
        """
        if isinstance(self._allocator, MixedMemoryAllocator):
            buffer = self._allocator.buffer
            growth = MemoryGrowthPolicy.FIXED
        elif isinstance(self._allocator, LazyMemoryAllocator):
            # TODO(ApostaC): need to test if the RDMA registration works
            # before the lazy expansion is finished
            buffer = self._allocator.get_underlying_buffer()
            growth = MemoryGrowthPolicy.GROWABLE
        else:
            raise NotImplementedError(
                "get_l1_memory_desc is not implemented for this allocator type."
            )
        return L1MemoryDesc(
            ptr=buffer.data_ptr(),
            size=self._size_in_bytes,
            align_bytes=self._align_bytes,
            growth=growth,
        )

    def close(self) -> None:
        """
        Close the memory manager and release all resources.
        """
        self._allocator.close()

    # Debugging APIs
    def memcheck(self):
        windows_ok = all(window.memcheck() for window in self._windows)
        return self._allocator.memcheck() and windows_ok

    def _allocator_for(self, pool: L1Pool) -> MemoryAllocatorInterface:
        """Return the allocator that serves ``pool``.

        Raises:
            ValueError: If ``pool`` names a window this manager does not have.
        """
        if pool.is_general():
            return self._allocator
        if pool.window_index >= len(self._windows):
            raise ValueError(
                f"RDMA window {pool.window_index} does not exist; this L1 "
                f"reserves {len(self._windows)} windows"
            )
        return self._windows[pool.window_index]

    def _reserved_window_bytes(self) -> int:
        """Return the bytes reserved for RDMA windows at the start of the slab."""
        return self._window_bytes * len(self._windows)
