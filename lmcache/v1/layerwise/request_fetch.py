# SPDX-License-Identifier: Apache-2.0
"""Turn a retrieve request's cache lookup into a layerwise fetch plan.

The retrieve path resolves a request to one :class:`ObjectKey` per chunk per
object group (``MPCacheServerContext.resolve_obj_keys``) and reads only each
group's in-window suffix. This module takes exactly that input and produces
the :class:`LayerFetchPlan` a pipelined fetch of those objects expects, so a
plan always names the objects the whole-object retrieve would have read.

Two inputs are not the request's to decide and are injected through
:class:`ChunkPlacer`: which node serves a chunk's object, and where in the
request's leased RDMA window it lands. See
``docs/design/v1/layerwise/system-design.md`` section 11 for why neither can
be produced here, and for the lease rules the placer follows.

Kept out of the package ``__init__`` because it imports the native adapter's
key serialization, and the adapter imports this package.
"""

# Standard
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable
import threading

# First Party
from lmcache.v1.distributed.api import AttnWindowDesc, ObjectKey
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.layerwise.contract import LayerFetchPlan
from lmcache.v1.layerwise.planner import (
    ChunkPlacement,
    FetchPlanner,
    ModelLayout,
    PlanRequest,
    RecordKeys,
)


def first_in_window_chunk(num_chunks: int, window_chunks: int) -> int:
    """Return the first chunk an object group's retrieve reads.

    A full-attention group reads the whole prefix; a sliding-window group
    reads only its trailing ``window_chunks`` chunks. This is the rule
    ``LMCacheDrivenTransferModule.retrieve`` applies, so a plan built with it
    covers exactly the objects the prefetch locked.

    Args:
        num_chunks: Chunks in the request.
        window_chunks: The group's entry in ``AttnWindowDesc.num_chunks_in_sw``:
            ``-1`` for full attention, otherwise the window in chunks.

    Returns:
        Index of the first chunk read, from ``0`` to ``num_chunks``.
    """
    if window_chunks < 0:
        return 0
    return max(0, num_chunks - window_chunks)


def _round_up(value: int, multiple: int) -> int:
    return -(-value // multiple) * multiple


@dataclass(frozen=True)
class ChunkLocation:
    """Where one chunk's object is fetched from and delivered to.

    Attributes:
        node_name: Cluster node that serves the object.
        dest_offset: Byte offset of the object from the start of the leased
            window.
    """

    node_name: str
    dest_offset: int


@dataclass(frozen=True)
class ObjectToPlace:
    """One object a fetch reads, as the placer is asked to place it.

    Attributes:
        chunk_id: Index of the chunk within the request.
        object_group_id: Object group the object belongs to.
        object_bytes: Size of the object, which its destination must fit.
    """

    chunk_id: int
    object_group_id: int
    object_bytes: int


class LeaseOutcome(Enum):
    """How the fetch that used a window lease ended.

    The outcome decides whether the window may be reused at once, so it is
    stated by the one caller that knows it rather than inferred by the placer.
    """

    #: Nothing was issued, so no write can be on the wire. The window is
    #: reusable at once.
    NEVER_FETCHED = "never_fetched"
    #: Every slot landed. The window holds the fetched objects and is
    #: reclaimable at once.
    FINISHED = "finished"
    #: Issued but not finished. Writes may still arrive, so the window is
    #: quarantined until the fetch timeout has passed.
    ABANDONED = "abandoned"


@runtime_checkable
class WindowLease(Protocol):
    """One request's hold on an RDMA window, with its objects placed in it.

    Released exactly once, with the outcome of the fetch that used it.
    """

    def window_bytes(self) -> int:
        """Return the size of the leased window; offsets stay below it."""
        ...

    def locate(self, chunk_id: int, object_group_id: int) -> ChunkLocation:
        """Return where one placed object comes from and where it lands.

        Args:
            chunk_id: Index of the chunk within the request.
            object_group_id: Object group the object belongs to.

        Returns:
            The object's node and window-relative destination offset.

        Raises:
            KeyError: If the object was not among those the lease placed.
        """
        ...

    def release(self, outcome: LeaseOutcome) -> None:
        """Give the window back, stating how its fetch ended.

        Args:
            outcome: Whether the fetch never started, finished, or was
                abandoned; see :class:`LeaseOutcome`.
        """
        ...


@runtime_checkable
class ChunkPlacer(Protocol):
    """Leases a window per request and decides where each object lands."""

    def lease(self, objects: Sequence[ObjectToPlace]) -> WindowLease:
        """Lease one window and place every object of a request in it.

        Args:
            objects: Every object the request reads, as returned by
                :func:`objects_to_place`.

        Returns:
            A lease that locates each of ``objects`` inside one window.

        Raises:
            PlanTooLargeError: If the objects cannot fit in any window, so
                the request would have to be split.
            LayerwiseContractError: If no window can be leased right now,
                e.g. every window is pinned or quarantined, or a fetch is
                already in flight. Splitting would not help.
        """
        ...


@dataclass(frozen=True)
class FetchModel:
    """What a registered model contributes to every fetch plan.

    Attributes:
        layout: Where each layer sits in its object group's payload.
        attn_desc: Each object group's attention window and kind.
    """

    layout: ModelLayout
    attn_desc: AttnWindowDesc

    def request_bytes(self, num_chunks: int, align_bytes: int = 1) -> int:
        """Return the window space a pipelined retrieve of a request needs.

        This is what a window must hold for one request of ``num_chunks``
        chunks: every object the retrieve reads, each rounded up to
        ``align_bytes``. Sliding-window groups contribute only their window,
        and aux groups nothing, exactly as :func:`request_cache_keys` selects
        objects. The transport sizes ``window_bytes`` from this at init.

        Args:
            num_chunks: Chunks in the request.
            align_bytes: Alignment each object's destination is rounded up to.

        Returns:
            Bytes of window space the request's objects occupy.

        Raises:
            ValueError: If ``num_chunks`` is negative or ``align_bytes`` is
                not positive.
            KeyError: If the layout does not cover an object group the
                request reads.
        """
        if num_chunks < 0:
            raise ValueError(f"num_chunks must be non-negative, got {num_chunks}")
        if align_bytes <= 0:
            raise ValueError(f"align_bytes must be positive, got {align_bytes}")
        total = 0
        for group_id in range(self.attn_desc.num_object_groups):
            if self.attn_desc.group_kinds and (
                self.attn_desc.group_kinds[group_id] == "aux"
            ):
                continue
            window = self.attn_desc.num_chunks_in_sw[group_id]
            chunks_read = num_chunks - first_in_window_chunk(num_chunks, window)
            object_bytes = _round_up(
                self.layout.object_group_bytes(group_id), align_bytes
            )
            total += chunks_read * object_bytes
        return total


@dataclass(frozen=True)
class RequestFetch:
    """A planned fetch and the placements it was planned from.

    Attributes:
        request: The chunks, nodes and destinations the plan covers.
        plan: Every slot the fetch expects, in slot-number order.
    """

    request: PlanRequest
    plan: LayerFetchPlan


class FetchModelRegistry:
    """Registered models' :class:`FetchModel`, by ``(model_name, world_size)``.

    Reference-counted the same way as the layout descriptor registry: every
    worker of a model registers it, and the entry lives until the last one
    unregisters. Thread-safe.
    """

    def __init__(self) -> None:
        self._models: dict[tuple[str, int], tuple[FetchModel, int]] = {}
        self._lock = threading.Lock()

    def register(self, model_name: str, world_size: int, model: FetchModel) -> None:
        """Add one registration of a model; the latest ``model`` is kept.

        Args:
            model_name: The model name.
            world_size: The world size.
            model: The model's fetch layout and attention windows.
        """
        key = (model_name, world_size)
        with self._lock:
            _, count = self._models.get(key, (model, 0))
            self._models[key] = (model, count + 1)

    def unregister(self, model_name: str, world_size: int) -> None:
        """Drop one registration; the entry goes with the last one.

        Unregistering a model that was never registered is a no-op, since a
        model whose layout could not be planned is never registered.

        Args:
            model_name: The model name.
            world_size: The world size.
        """
        key = (model_name, world_size)
        with self._lock:
            entry = self._models.get(key)
            if entry is None:
                return
            model, count = entry
            if count <= 1:
                del self._models[key]
            else:
                self._models[key] = (model, count - 1)

    def find(self, model_name: str, world_size: int) -> FetchModel:
        """Return a registered model's fetch layout.

        Args:
            model_name: The model name.
            world_size: The world size.

        Returns:
            The model's :class:`FetchModel`.

        Raises:
            KeyError: If the model is not registered, or its layout could not
                be planned; the caller should load whole objects instead.
        """
        with self._lock:
            entry = self._models.get((model_name, world_size))
        if entry is None:
            raise KeyError(
                f"no layerwise fetch layout for model {model_name!r} with "
                f"world size {world_size}"
            )
        return entry[0]


def request_cache_keys(
    obj_keys_per_obj_group: Sequence[Sequence[ObjectKey]],
    attn_desc: AttnWindowDesc,
) -> dict[tuple[int, int], str]:
    """Map each object a retrieve reads to the key it is stored under.

    Aux object groups are skipped and sliding-window groups contribute only
    their in-window suffix, exactly as the retrieve path reads them.

    Args:
        obj_keys_per_obj_group: Element ``g`` is object group ``g``'s keys,
            one per chunk in request order -- what ``resolve_obj_keys``
            returns for ``list(range(num_object_groups))`` with a worker id.
        attn_desc: The model's per-group windows and kinds.

    Returns:
        ``{(chunk_id, object_group_id): stored key}``, where ``chunk_id`` is
        the chunk's index in the request.

    Raises:
        ValueError: If the number of groups does not match ``attn_desc``, or
            the groups do not all have the same number of chunks.
    """
    if len(obj_keys_per_obj_group) != attn_desc.num_object_groups:
        raise ValueError(
            f"got keys for {len(obj_keys_per_obj_group)} object groups, but "
            f"the model has {attn_desc.num_object_groups}"
        )
    chunk_counts = {len(keys) for keys in obj_keys_per_obj_group}
    if len(chunk_counts) > 1:
        raise ValueError(
            f"object groups disagree on the number of chunks: {sorted(chunk_counts)}"
        )
    num_chunks = chunk_counts.pop() if chunk_counts else 0

    cache_keys: dict[tuple[int, int], str] = {}
    for group_id, keys in enumerate(obj_keys_per_obj_group):
        if attn_desc.group_kinds and attn_desc.group_kinds[group_id] == "aux":
            continue
        start = first_in_window_chunk(num_chunks, attn_desc.num_chunks_in_sw[group_id])
        for chunk_id in range(start, num_chunks):
            cache_keys[(chunk_id, group_id)] = object_key_to_string(keys[chunk_id])
    return cache_keys


def objects_to_place(
    model: FetchModel, obj_keys_per_obj_group: Sequence[Sequence[ObjectKey]]
) -> tuple[ObjectToPlace, ...]:
    """List the objects a retrieve reads, for :meth:`ChunkPlacer.lease`.

    Args:
        model: The registered model's layout and windows.
        obj_keys_per_obj_group: The request's object keys, as described for
            :func:`request_cache_keys`.

    Returns:
        One entry per object read, ordered by chunk, then object group.

    Raises:
        ValueError: If the keys do not match the model (see
            :func:`request_cache_keys`).
        KeyError: If the layout does not cover an object group the request
            reads.
    """
    cache_keys = request_cache_keys(obj_keys_per_obj_group, model.attn_desc)
    return tuple(
        ObjectToPlace(chunk_id, group_id, model.layout.object_group_bytes(group_id))
        for chunk_id, group_id in sorted(cache_keys)
    )


def build_request_fetch(
    model: FetchModel,
    obj_keys_per_obj_group: Sequence[Sequence[ObjectKey]],
    max_record_bytes: int,
    lease: WindowLease,
) -> RequestFetch:
    """Plan the pipelined fetch of every object a retrieve would read.

    Every location the lease returns is checked against the window before it
    is planned: an object reaching past the window, or two objects sharing
    bytes, would have RDMA writes land on the wrong data without any error.

    Args:
        model: The registered model's layout and windows.
        obj_keys_per_obj_group: The request's object keys, as described for
            :func:`request_cache_keys`.
        max_record_bytes: The record cap the objects were written under;
            the connector reports it as ``max_record_bytes()``.
        lease: The request's window lease, which places each object.

    Returns:
        The placements and the plan built from them. Placements are ordered
        by chunk, then object group; node indices follow first appearance.

    Raises:
        ValueError: If the keys do not match the model (see
            :func:`request_cache_keys`), the request reads no objects, the
            lease places an object outside its window or over another one,
            or the model's records cannot be named layer by layer.
        KeyError: If the layout does not cover an object group the request
            reads, or the lease cannot locate an object.
    """
    cache_keys = request_cache_keys(obj_keys_per_obj_group, model.attn_desc)
    window_bytes = lease.window_bytes()
    node_indices: dict[str, int] = {}
    placements: list[ChunkPlacement] = []
    extents: list[tuple[int, int, tuple[int, int]]] = []
    for chunk_id, group_id in sorted(cache_keys):
        location = lease.locate(chunk_id, group_id)
        object_bytes = model.layout.object_group_bytes(group_id)
        end = location.dest_offset + object_bytes
        if location.dest_offset < 0 or end > window_bytes:
            raise ValueError(
                f"object (chunk {chunk_id}, group {group_id}) placed at "
                f"[{location.dest_offset}, {end}), outside the "
                f"{window_bytes}-byte window"
            )
        extents.append((location.dest_offset, end, (chunk_id, group_id)))
        node_index = node_indices.setdefault(location.node_name, len(node_indices))
        placements.append(
            ChunkPlacement(
                chunk_id=chunk_id,
                object_group_id=group_id,
                node_index=node_index,
                dest_offset=location.dest_offset,
            )
        )
    extents.sort()
    for (_, previous_end, previous), (start, _, current) in zip(
        extents, extents[1:], strict=False
    ):
        if start < previous_end:
            raise ValueError(
                f"objects {previous} and {current} overlap in the window "
                f"at byte {start}"
            )
    request = PlanRequest(
        placements=tuple(placements),
        node_names=tuple(node_indices),
        max_record_bytes=max_record_bytes,
    )
    plan = FetchPlanner(model.layout).plan(
        request, RecordKeys(model.layout, max_record_bytes, cache_keys)
    )
    return RequestFetch(request=request, plan=plan)
