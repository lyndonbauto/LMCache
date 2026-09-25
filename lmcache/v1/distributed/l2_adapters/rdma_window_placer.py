# SPDX-License-Identifier: Apache-2.0
"""Places a pipelined retrieve's L1 objects in one leased RDMA window.

This is the destination half of the layerwise ``ChunkPlacer``: it decides
where each object lands, not which node serves it. Nodes are per record and
come from the planner (see N1 in
``docs/design/v1/layerwise/track-a-questions-for-track-c.md``).

Every window is published to the nodes as one registration covering the
whole window range, which starts at slab offset 0. So an object's destination
offset is its slab offset, ``memory_obj.meta.address``.
"""

# Standard
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import enum

# First Party
from lmcache.utils import get_size_bytes
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.rdma_window_leaser import (
    FetchOutcome,
    RdmaWindowLeaser,
    WindowLease,
)
from lmcache.v1.layerwise.contract import LayerwiseContractError, PlanTooLargeError
from lmcache.v1.memory_management import MemoryObj


def _align_up(size: int, align_bytes: int) -> int:
    """Round ``size`` up to a multiple of ``align_bytes`` (0 means no rounding)."""
    if align_bytes <= 0:
        return size
    return -(-size // align_bytes) * align_bytes


@dataclass(frozen=True)
class ObjectToPlace:
    """One object a pipelined retrieve reads.

    Attributes:
        chunk_id: Index of the chunk within the request.
        object_group_id: Object group the object belongs to.
        key: The key the object is cached under in L1.
    """

    chunk_id: int
    object_group_id: int
    key: ObjectKey


class _PlacementState(enum.Enum):
    """Where a placement is in its life."""

    PLACED = enum.auto()
    COMPLETED = enum.auto()
    ABANDONED = enum.auto()


class WindowPlacement:
    """A retrieve's objects, reserved for write inside one leased window.

    Created by :meth:`RdmaWindowPlacer.place`. The objects stay write-locked
    until the placement ends with exactly one of :meth:`complete` or
    :meth:`abandon`.

    Not thread-safe: one retrieve drives it.
    """

    def __init__(
        self,
        l1_manager: L1Manager,
        leaser: RdmaWindowLeaser,
        lease: WindowLease,
        objects: Mapping[tuple[int, int], tuple[ObjectKey, MemoryObj]],
    ) -> None:
        """Wrap reserved objects; use :meth:`RdmaWindowPlacer.place` instead.

        Args:
            l1_manager: The L1 holding the reservations.
            leaser: The leaser ``lease`` came from.
            lease: The window the objects are in.
            objects: ``{(chunk_id, object_group_id): (key, memory_obj)}``.
        """
        self._l1_manager = l1_manager
        self._leaser = leaser
        self._lease = lease
        self._objects = dict(objects)
        self._state = _PlacementState.PLACED

    @property
    def lease(self) -> WindowLease:
        """The window this placement's objects are in."""
        return self._lease

    def dest_offset(self, chunk_id: int, object_group_id: int) -> int:
        """Return where one object lands, as a slab offset.

        Args:
            chunk_id: Index of the chunk within the request.
            object_group_id: Object group the object belongs to.

        Returns:
            The object's byte offset from the start of the L1 slab, which is
            the offset within the published window registration.

        Raises:
            KeyError: If the object was not placed.
        """
        return self._memory_obj(chunk_id, object_group_id).meta.address

    def memory_obj(self, chunk_id: int, object_group_id: int) -> MemoryObj:
        """Return one object's L1 memory, e.g. for the layer loader.

        Args:
            chunk_id: Index of the chunk within the request.
            object_group_id: Object group the object belongs to.

        Returns:
            The write-reserved memory object.

        Raises:
            KeyError: If the object was not placed.
        """
        return self._memory_obj(chunk_id, object_group_id)

    def keys(self) -> list[ObjectKey]:
        """Return the keys of every placed object, ordered by chunk, then group.

        Returns:
            The L1 keys.
        """
        return [self._objects[pair][0] for pair in sorted(self._objects)]

    def complete(self) -> None:
        """End a fetch in which every layer became resident.

        Finishes the writes, so the objects become readable L1 cache entries,
        and releases the window as :attr:`FetchOutcome.FINISHED`, leaving it
        reclaimable at once.

        Raises:
            ValueError: If the placement already ended.
            RuntimeError: If L1 refuses to finish a write, e.g. a write lock
                expired; the window is still released.
        """
        self._end(_PlacementState.COMPLETED)
        try:
            results = self._l1_manager.finish_write(self.keys())
        finally:
            self._leaser.release(self._lease, FetchOutcome.FINISHED)
        failed = {k: e for k, e in results.items() if e != L1Error.SUCCESS}
        if failed:
            raise RuntimeError(f"L1 refused to finish writes: {failed}")

    def abandon(self) -> None:
        """End a fetch that did not finish, for any reason.

        Aborts the write reservations, so no half-written object becomes
        readable, and releases the window as :attr:`FetchOutcome.ABANDONED`,
        which quarantines it. A fallback load must then reserve fresh objects
        in general L1 (W4).

        Raises:
            ValueError: If the placement already ended.
        """
        self._end(_PlacementState.ABANDONED)
        try:
            self._l1_manager.abort_write(self.keys())
        finally:
            self._leaser.release(self._lease, FetchOutcome.ABANDONED)

    def _memory_obj(self, chunk_id: int, object_group_id: int) -> MemoryObj:
        """Return one placed object's memory. Raises ``KeyError`` if absent."""
        pair = (chunk_id, object_group_id)
        if pair not in self._objects:
            raise KeyError(
                f"chunk {chunk_id} of object group {object_group_id} was not placed"
            )
        return self._objects[pair][1]

    def _end(self, state: _PlacementState) -> None:
        """Move from PLACED to ``state``, or raise if already ended."""
        if self._state is not _PlacementState.PLACED:
            raise ValueError(f"placement already ended as {self._state.name}")
        self._state = state


class RdmaWindowPlacer:
    """Reserves every object of a pipelined retrieve inside one leased window.

    Thread-safe to the extent its collaborators are: each call to
    :meth:`place` works on its own lease, and the leaser admits one at a time.
    """

    def __init__(self, l1_manager: L1Manager, leaser: RdmaWindowLeaser) -> None:
        """Create a placer.

        Args:
            l1_manager: The L1 whose windows ``leaser`` hands out.
            leaser: Hands out the windows.
        """
        self._l1_manager = l1_manager
        self._leaser = leaser
        self._align_bytes = l1_manager.get_l1_memory_desc().align_bytes

    def place(
        self,
        objects: Sequence[ObjectToPlace],
        layouts: Mapping[int, MemoryLayoutDesc],
    ) -> WindowPlacement:
        """Lease a window and reserve every object in it, all or nothing.

        Args:
            objects: The objects the retrieve reads, each ``(chunk_id,
                object_group_id)`` at most once.
            layouts: The L1 memory layout of each object group in
                ``objects``, as the retrieve would reserve it in general L1.

        Returns:
            The placement. End it with :meth:`WindowPlacement.complete` or
            :meth:`WindowPlacement.abandon`.

        Raises:
            ValueError: If ``objects`` is empty, repeats a pair, or names an
                object group missing from ``layouts``.
            PlanTooLargeError: If the objects, each rounded up to the L1
                alignment, do not fit in one window. The caller can split
                the request.
            LayerwiseContractError: If no window can be leased, or L1 refuses
                a reservation, e.g. because a key is already cached. Nothing
                is left reserved or leased. The caller falls back.
        """
        request_bytes = self._request_bytes(objects, layouts)
        lease = self._leaser.lease(request_bytes)
        try:
            reserved = self._reserve_all(objects, layouts, lease)
        except BaseException:
            self._leaser.release(lease, FetchOutcome.FINISHED)
            raise
        return WindowPlacement(self._l1_manager, self._leaser, lease, reserved)

    def _request_bytes(
        self,
        objects: Sequence[ObjectToPlace],
        layouts: Mapping[int, MemoryLayoutDesc],
    ) -> int:
        """Return the window bytes ``objects`` need, validating the input."""
        if not objects:
            raise ValueError("a placement needs at least one object")
        pairs = [(o.chunk_id, o.object_group_id) for o in objects]
        if len(set(pairs)) != len(pairs):
            raise ValueError(f"objects repeat a (chunk, object group) pair: {pairs}")
        total = 0
        for obj in objects:
            layout = layouts.get(obj.object_group_id)
            if layout is None:
                raise ValueError(f"no L1 layout for object group {obj.object_group_id}")
            raw = get_size_bytes(layout.shapes, layout.dtypes)
            total += _align_up(raw, self._align_bytes)
        return total

    def _reserve_all(
        self,
        objects: Sequence[ObjectToPlace],
        layouts: Mapping[int, MemoryLayoutDesc],
        lease: WindowLease,
    ) -> dict[tuple[int, int], tuple[ObjectKey, MemoryObj]]:
        """Reserve ``objects`` in the leased window, undoing all on failure.

        Raises:
            PlanTooLargeError: If the window ran out of room despite the size
                check, e.g. through fragmentation.
            LayerwiseContractError: If L1 refused any key.
        """
        by_group: dict[int, list[ObjectToPlace]] = {}
        for obj in objects:
            by_group.setdefault(obj.object_group_id, []).append(obj)

        reserved: dict[tuple[int, int], tuple[ObjectKey, MemoryObj]] = {}
        for group_id, group_objects in sorted(by_group.items()):
            keys = [o.key for o in group_objects]
            results = self._l1_manager.reserve_write(
                keys,
                [False] * len(keys),
                layouts[group_id],
                mode="new",
                pool=lease.pool(),
            )
            for obj in group_objects:
                err, memory_obj = results[obj.key]
                if err == L1Error.SUCCESS and memory_obj is not None:
                    reserved[(obj.chunk_id, group_id)] = (obj.key, memory_obj)
            errors = {k: e for k, (e, _) in results.items() if e != L1Error.SUCCESS}
            if errors:
                self._l1_manager.abort_write([key for key, _ in reserved.values()])
                if L1Error.OUT_OF_MEMORY in errors.values():
                    raise PlanTooLargeError(
                        f"the request's objects do not fit in RDMA window "
                        f"{lease.window_index}"
                    )
                raise LayerwiseContractError(
                    f"L1 refused to reserve objects in RDMA window "
                    f"{lease.window_index}: {errors}"
                )
        return reserved
