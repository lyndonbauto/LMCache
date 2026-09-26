# SPDX-License-Identifier: Apache-2.0
"""The layerwise ``ChunkPlacer`` over leased RDMA windows.

:class:`RdmaWindowPlacer` implements
:class:`~lmcache.v1.layerwise.request_fetch.ChunkPlacer` and
:class:`WindowPlacement` implements
:class:`~lmcache.v1.layerwise.request_fetch.WindowLease`: a retrieve's
objects are reserved in L1 inside one leased window, and each is fetched
from the one node of the cluster. Pipelined fetches run on single-node
clusters only (N1 in
``docs/design/v1/layerwise/track-a-questions-for-track-c.md``); the
connector refuses to initialize them on a larger one.

Every window is published to the nodes as one registration covering the
whole window range, which starts at slab offset 0. So an object's destination
offset is its slab offset, ``memory_obj.meta.address``.
"""

# Standard
from collections.abc import Callable, Mapping, Sequence
import enum

# First Party
from lmcache.utils import get_size_bytes
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.rdma_window_leaser import (
    FetchOutcome,
    RdmaWindowLeaser,
)
from lmcache.v1.distributed.l2_adapters.rdma_window_leaser import (
    WindowLease as LeasedWindow,
)
from lmcache.v1.layerwise.contract import LayerwiseContractError, PlanTooLargeError
from lmcache.v1.layerwise.request_fetch import (
    ChunkLocation,
    FetchModel,
    LeaseOutcome,
    ObjectToPlace,
)
from lmcache.v1.memory_management import MemoryObj


def check_window_holds_request(
    window_bytes: int,
    model: FetchModel,
    max_pipelined_chunks: int,
    align_bytes: int,
) -> None:
    """Check that one RDMA window holds a model's largest pipelined retrieve.

    Called when a model registers. The windows are carved out of L1 before
    any layout is known, so ``window_bytes`` comes from config and can only
    be checked here, not derived.

    Args:
        window_bytes: The configured size of each RDMA window.
        model: The registered model's fetch model.
        max_pipelined_chunks: The most chunks one pipelined retrieve may
            read.
        align_bytes: The L1 slab alignment each object is rounded up to.

    Raises:
        ValueError: If ``max_pipelined_chunks`` is not positive, or the
            window is smaller than
            ``model.request_bytes(max_pipelined_chunks, align_bytes)``. The
            message states the window size needed.
    """
    if max_pipelined_chunks <= 0:
        raise ValueError(
            f"max_pipelined_chunks must be positive, got {max_pipelined_chunks}"
        )
    needed = model.request_bytes(max_pipelined_chunks, align_bytes)
    if needed > window_bytes:
        raise ValueError(
            f"an RDMA window of {window_bytes} bytes cannot hold a pipelined "
            f"retrieve of {max_pipelined_chunks} chunks; set rdma_window_bytes "
            f"to at least {needed}"
        )


def retain_none(keys: list[ObjectKey]) -> list[bool]:
    """Keep none of the fetched objects in L1 once their fetch finishes.

    The placer's default retention, matching the ``default`` prefetch
    policy's ``select_l1_retentions``.

    Args:
        keys: Keys of the objects being placed.

    Returns:
        ``False`` for every key.
    """
    return [False] * len(keys)


def _align_up(size: int, align_bytes: int) -> int:
    """Round ``size`` up to a multiple of ``align_bytes`` (0 means no rounding)."""
    if align_bytes <= 0:
        return size
    return -(-size // align_bytes) * align_bytes


class _PlacementState(enum.Enum):
    """Where a placement is in its life."""

    PLACED = enum.auto()
    RELEASED = enum.auto()


class WindowPlacement:
    """A retrieve's objects, reserved for write inside one leased window.

    Created by :meth:`RdmaWindowPlacer.lease`. The objects stay write-locked
    until :meth:`release` is called exactly once.

    Not thread-safe: one retrieve drives it.
    """

    def __init__(
        self,
        l1_manager: L1Manager,
        leaser: RdmaWindowLeaser,
        window: LeasedWindow,
        node_name: str,
        objects: Mapping[tuple[int, int], tuple[ObjectKey, MemoryObj]],
    ) -> None:
        """Wrap reserved objects; use :meth:`RdmaWindowPlacer.lease` instead.

        Args:
            l1_manager: The L1 holding the reservations.
            leaser: The leaser ``window`` came from.
            window: The leased window the objects are in.
            node_name: The node every object is fetched from.
            objects: ``{(chunk_id, object_group_id): (key, memory_obj)}``.
        """
        self._l1_manager = l1_manager
        self._leaser = leaser
        self._window = window
        self._node_name = node_name
        self._objects = dict(objects)
        self._state = _PlacementState.PLACED

    def window_start(self) -> int:
        """Return where the leased window begins, as a slab offset."""
        return self._window.base_offset

    def window_bytes(self) -> int:
        """Return the size of the leased window."""
        return self._window.size_bytes

    def locate(self, chunk_id: int, object_group_id: int) -> ChunkLocation:
        """Return where one placed object comes from and where it lands.

        Args:
            chunk_id: Index of the chunk within the request.
            object_group_id: Object group the object belongs to.

        Returns:
            The node the placer was built with, and the object's slab offset,
            which lies inside ``[window_start, window_start + window_bytes)``.

        Raises:
            KeyError: If the object was not placed.
        """
        memory_obj = self._memory_obj(chunk_id, object_group_id)
        return ChunkLocation(self._node_name, memory_obj.meta.address)

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

    def release(self, outcome: LeaseOutcome) -> None:
        """End the placement, stating how its fetch ended.

        - ``FINISHED``: every layer became resident and the reader has
          consumed it. Objects the placer's retention keeps become readable
          L1 cache entries; the rest are freed. Neither is stored back to
          L2, since that is where they came from. The window is reusable at
          once, so the reader's copies out of it must have completed.
        - ``NEVER_FETCHED``: nothing was issued, so no write can be on the
          wire. The reservations are aborted and the window is reusable at
          once.
        - ``ABANDONED``: issued but not finished, so writes may still
          arrive. The reservations are aborted, so no half-written object
          becomes readable, and the window is quarantined until the fetch
          timeout has passed. A fallback load must reserve fresh objects in
          general L1 (W4).

        Args:
            outcome: How the fetch that used the window ended.

        Raises:
            ValueError: If the placement was already released.
            RuntimeError: On ``FINISHED``, if L1 refuses to finish a write,
                e.g. a write lock expired; the window is still released.
        """
        if self._state is not _PlacementState.PLACED:
            raise ValueError("placement already released")
        self._state = _PlacementState.RELEASED
        if outcome is LeaseOutcome.FINISHED:
            self._finish()
        elif outcome is LeaseOutcome.NEVER_FETCHED:
            self._abort(FetchOutcome.FINISHED)
        else:
            self._abort(FetchOutcome.ABANDONED)

    def _finish(self) -> None:
        """End every write as a consumed load and release the window.

        ``finish_write_and_reserve_read`` rather than ``finish_write``: the
        store controller ignores it, so nothing is stored back to L2. The
        read lock is dropped at once, which frees temporary objects.
        """
        keys = self.keys()
        try:
            results = self._l1_manager.finish_write_and_reserve_read(keys)
            finished = [k for k, (e, _) in results.items() if e == L1Error.SUCCESS]
            self._l1_manager.finish_read(finished)
        finally:
            self._leaser.release(self._window, FetchOutcome.FINISHED)
        failed = {k: e for k, (e, _) in results.items() if e != L1Error.SUCCESS}
        if failed:
            raise RuntimeError(f"L1 refused to finish writes: {failed}")

    def _abort(self, window_outcome: FetchOutcome) -> None:
        """Abort every write and release the window with ``window_outcome``."""
        try:
            self._l1_manager.abort_write(self.keys())
        finally:
            self._leaser.release(self._window, window_outcome)

    def _memory_obj(self, chunk_id: int, object_group_id: int) -> MemoryObj:
        """Return one placed object's memory. Raises ``KeyError`` if absent."""
        pair = (chunk_id, object_group_id)
        if pair not in self._objects:
            raise KeyError(
                f"chunk {chunk_id} of object group {object_group_id} was not placed"
            )
        return self._objects[pair][1]


class RdmaWindowPlacer:
    """Reserves every object of a pipelined retrieve inside one leased window.

    Thread-safe to the extent its collaborators are: each call to
    :meth:`lease` works on its own window, and the leaser never leases one
    window twice at once.
    """

    def __init__(
        self,
        l1_manager: L1Manager,
        leaser: RdmaWindowLeaser,
        layouts: Mapping[int, MemoryLayoutDesc],
        node_name: str,
        select_retentions: Callable[[list[ObjectKey]], list[bool]] = retain_none,
    ) -> None:
        """Create a placer for one registered model on a single-node cluster.

        Args:
            l1_manager: The L1 whose windows ``leaser`` hands out.
            leaser: Hands out the windows.
            layouts: ``{object_group_id: layout}``, the L1 memory layout of
                each object group, as a retrieve would reserve it in general
                L1.
            node_name: The cluster's one node, which every object is fetched
                from.
            select_retentions: Given the keys of one lease's objects, in
                order, returns whether to keep each in L1 after its fetch
                finishes, as the prefetch policy's ``select_l1_retentions``
                does. Called once per lease. Defaults to keeping none.

        Raises:
            ValueError: If ``layouts`` is empty or ``node_name`` is empty.
        """
        if not layouts:
            raise ValueError("a placer needs the layout of every object group")
        if not node_name:
            raise ValueError("a placer needs the name of the node to fetch from")
        self._l1_manager = l1_manager
        self._leaser = leaser
        self._layouts = dict(layouts)
        self._node_name = node_name
        self._select_retentions = select_retentions
        self._align_bytes = l1_manager.get_l1_memory_desc().align_bytes

    def lease(self, objects: Sequence[ObjectToPlace]) -> WindowPlacement:
        """Lease a window and reserve every object in it, all or nothing.

        Args:
            objects: The objects the retrieve reads, each ``(chunk_id,
                object_group_id)`` at most once.

        Returns:
            The placement. End it with :meth:`WindowPlacement.release`.

        Raises:
            ValueError: If ``objects`` is empty, repeats a pair, names an
                object group with no layout, or is larger than its group's
                L1 layout, so the fetched bytes would overrun the object;
                or if ``select_retentions`` returns the wrong number of
                choices.
            PlanTooLargeError: If the objects, each rounded up to the L1
                alignment, do not fit in one window. The caller can split
                the request.
            LayerwiseContractError: If no window can be leased, or L1 refuses
                a reservation, e.g. because a key is already cached. Nothing
                is left reserved or leased. The caller falls back.
        """
        request_bytes = self._request_bytes(objects)
        keys = [o.key for o in objects]
        retentions = self._select_retentions(keys)
        if len(retentions) != len(keys):
            raise ValueError(
                f"select_retentions returned {len(retentions)} choices for "
                f"{len(keys)} objects"
            )
        retained = {key for key, keep in zip(keys, retentions, strict=True) if keep}
        window = self._leaser.lease(request_bytes)
        try:
            reserved = self._reserve_all(objects, window, retained)
        except BaseException:
            self._leaser.release(window, FetchOutcome.FINISHED)
            raise
        return WindowPlacement(
            self._l1_manager, self._leaser, window, self._node_name, reserved
        )

    def _request_bytes(self, objects: Sequence[ObjectToPlace]) -> int:
        """Return the window bytes ``objects`` need, validating the input."""
        if not objects:
            raise ValueError("a placement needs at least one object")
        pairs = [(o.chunk_id, o.object_group_id) for o in objects]
        if len(set(pairs)) != len(pairs):
            raise ValueError(f"objects repeat a (chunk, object group) pair: {pairs}")
        total = 0
        for obj in objects:
            layout = self._layouts.get(obj.object_group_id)
            if layout is None:
                raise ValueError(f"no L1 layout for object group {obj.object_group_id}")
            raw = get_size_bytes(layout.shapes, layout.dtypes)
            if obj.object_bytes > raw:
                raise ValueError(
                    f"object group {obj.object_group_id} objects are "
                    f"{obj.object_bytes} bytes, larger than its {raw}-byte "
                    f"L1 layout"
                )
            total += _align_up(raw, self._align_bytes)
        return total

    def _reserve_all(
        self,
        objects: Sequence[ObjectToPlace],
        window: LeasedWindow,
        retained: set[ObjectKey],
    ) -> dict[tuple[int, int], tuple[ObjectKey, MemoryObj]]:
        """Reserve ``objects`` in the leased window, undoing all on failure.

        Objects whose key is in ``retained`` are reserved permanent, the
        rest temporary.

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
                [key not in retained for key in keys],
                self._layouts[group_id],
                mode="new",
                pool=window.pool(),
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
                        f"{window.window_index}"
                    )
                raise LayerwiseContractError(
                    f"L1 refused to reserve objects in RDMA window "
                    f"{window.window_index}: {errors}"
                )
        return reserved
