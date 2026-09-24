# SPDX-License-Identifier: Apache-2.0
"""Track C's side of the layerwise work: turning a request into a fetch plan.

Planning is the one piece with no counterpart on the other side of a
contract -- Track A consumes the plan and Track B consumes the layer order
derived from it, but neither produces anything Track C has to wait for.

Two stages, matching how the information actually arrives:

:class:`ModelLayout`
    Built once, from the layouts LMCache publishes at registration. It
    answers "where does global layer N live inside its object group's
    payload", which depends only on the model.

:class:`FetchPlanner`
    Built over a layout and used per request. It answers "which writes does
    this request expect", which depends on the chunks the request needs and
    where they were placed in the registered window.

The same arithmetic exists in C++, in
``csrc/storage_backends/aerospike/{slot_planner,shard_plan,memory_layout_conversion}.*``,
where it drives the production fetch path and is proven by the harness in
``tests/v1/distributed/rdma/``. It exists here as well so that Track C's tests
need no native build (acceptance criterion C10). The two must agree exactly;
see ``docs/design/v1/layerwise/system-design.md`` section 7.
"""

# Standard
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

# Local
from .contract import LayerFetchPlan, SlotPlacement

if TYPE_CHECKING:
    # First Party
    from lmcache.v1.distributed.api import MemoryLayoutDesc

#: Slots addressable by one request. The RDMA immediate carries 32 bits, split
#: as ``(generation << 16) | slot``, so a request has 16 bits of slot index.
#: Mirrors ``kMaxSlotsPerRequest`` in
#: ``csrc/storage_backends/aerospike/layer_pipeline.h``; the two must agree,
#: because the transport decodes what this module numbers.
MAX_SLOTS_PER_REQUEST = 0x10000


def _ceil_div(numerator: int, denominator: int) -> int:
    """Divide, rounding up.

    Args:
        numerator: Value to divide, non-negative.
        denominator: Divisor, must be positive.

    Returns:
        The smallest integer at least ``numerator / denominator``.
    """
    return -(-numerator // denominator)


def plane_segment_bytes(plane_bytes: int, max_record_bytes: int) -> int:
    """Return the size of one record of a plane cut plane-aligned.

    A plane is cut into the fewest pieces that each fit the record cap, and
    those pieces are then equally sized::

        pieces  = ceil(plane_bytes / max_record_bytes)
        segment = ceil(plane_bytes / pieces)

    The even split is not cosmetic. A slot is exactly one record, and the
    wire format names a record and a destination with no record-relative
    source offset, so a planner that emitted a full cap plus a short
    remainder would name records the write side never produced. This mirrors
    ``plane_segment_bytes`` in
    ``csrc/storage_backends/aerospike/shard_plan.h``.

    Args:
        plane_bytes: Size of one K/V plane of one layer within one chunk.
        max_record_bytes: Largest record the cluster will hold.

    Returns:
        Size in bytes of one record of the plane.

    Raises:
        ValueError: If either argument is not positive.
    """
    if plane_bytes <= 0:
        raise ValueError(f"plane_bytes must be positive, got {plane_bytes}")
    if max_record_bytes <= 0:
        raise ValueError(f"max_record_bytes must be positive, got {max_record_bytes}")
    return _ceil_div(plane_bytes, _ceil_div(plane_bytes, max_record_bytes))


@dataclass(frozen=True)
class ByteRange:
    """A contiguous span of an object group's payload.

    Attributes:
        offset: Start of the span, relative to the base of the object group's
            payload.
        length: Length of the span in bytes.
    """

    offset: int
    length: int


@dataclass(frozen=True)
class KernelGroupGeometry:
    """One kernel group's shape, as published at registration.

    A kernel group is internally uniform by construction -- every layer in it
    shares one shape -- which is what makes per-layer offsets exactly
    computable. Geometry is held per kernel group and never flattened,
    because the hazard is not that strides are unpredictable but that one
    group's stride gets applied to another group's layers.

    Attributes:
        layer_ids: Global layer indices this group holds, in the order they
            appear along the tensor's layer dimension. Position in this tuple
            is the layer's stride index; the order is therefore the tensor's,
            not necessarily ascending.
        kv_planes: Independent planes per layer: two when key and value are
            separate tensors, one when the engine format puts the layer
            dimension outermost, as MLA does.
        plane_bytes: Bytes in one plane of one layer, which is
            ``num_slots * hidden_dim * element_size``.

    Raises:
        ValueError: If the group holds no layers, repeats a layer, or has a
            non-positive plane count or plane size.
    """

    layer_ids: tuple[int, ...]
    kv_planes: int
    plane_bytes: int

    def __post_init__(self) -> None:
        if not self.layer_ids:
            raise ValueError("a kernel group must hold at least one layer")
        if len(set(self.layer_ids)) != len(self.layer_ids):
            raise ValueError(
                f"kernel group repeats a layer: {self.layer_ids}, so its "
                "plane offsets would be ambiguous"
            )
        if self.kv_planes <= 0:
            raise ValueError(f"kv_planes must be positive, got {self.kv_planes}")
        if self.plane_bytes <= 0:
            raise ValueError(f"plane_bytes must be positive, got {self.plane_bytes}")

    def total_bytes(self) -> int:
        """Return the bytes this group's tensor occupies within one chunk.

        Returns:
            ``kv_planes * len(layer_ids) * plane_bytes``.
        """
        return self.kv_planes * len(self.layer_ids) * self.plane_bytes


@dataclass(frozen=True)
class ChunkPlacement:
    """Where one chunk's object for one object group sits in the window.

    LMCache chooses every destination address, because the window is its own
    registered memory and the server is told where to write. So the offset is
    an input here rather than something derived.

    Attributes:
        chunk_id: Index of the KV chunk within the request.
        object_group_id: Object group this placement is for. A chunk has one
            placement per object group it participates in.
        node_index: Index into :attr:`PlanRequest.node_names` identifying the
            cluster node holding this chunk. A chunk's object is stored whole
            on one node, so every slot cut from it is fetched from there.
        dest_offset: Base offset of this object within the registered window.

    Raises:
        ValueError: If the offset or node index is negative.
    """

    chunk_id: int
    object_group_id: int
    node_index: int
    dest_offset: int

    def __post_init__(self) -> None:
        if self.node_index < 0:
            raise ValueError(
                f"chunk {self.chunk_id} has a negative node index {self.node_index}"
            )
        if self.dest_offset < 0:
            raise ValueError(
                f"chunk {self.chunk_id} has a negative destination offset "
                f"{self.dest_offset}"
            )


@dataclass(frozen=True)
class PlanRequest:
    """What one fetch needs to cover.

    Attributes:
        placements: Where each participating chunk's object sits, one entry
            per (chunk, object group). Under a sliding window this is the
            participating subset, not every chunk of the prompt -- use
            :meth:`FetchPlanner.participating_chunks` to derive it rather
            than computing it at the call site.
        max_record_bytes: Largest record the cluster will hold, which decides
            how a plane is cut into slots.

        node_names: The cluster nodes this fetch talks to, in the order
            :attr:`ChunkPlacement.node_index` numbers them.

    Raises:
        ValueError: If there are no placements or no node names, if
            ``max_record_bytes`` is not positive, if a (chunk, object group)
            pair is placed twice, if a node name is blank or repeated, or if
            a placement names a node outside ``node_names``.
    """

    placements: tuple[ChunkPlacement, ...]
    node_names: tuple[str, ...]
    max_record_bytes: int

    def __post_init__(self) -> None:
        if not self.placements:
            raise ValueError("a fetch must place at least one chunk")
        if not self.node_names:
            raise ValueError("a fetch must name at least one node")
        if any(not name for name in self.node_names):
            raise ValueError("a fetch must not name a node with an empty name")
        if len(set(self.node_names)) != len(self.node_names):
            raise ValueError(
                f"fetch repeats a node name: {self.node_names}, so a "
                "placement's node index would be ambiguous"
            )
        if self.max_record_bytes <= 0:
            raise ValueError(
                f"max_record_bytes must be positive, got {self.max_record_bytes}"
            )
        seen: set[tuple[int, int]] = set()
        for placement in self.placements:
            if placement.node_index >= len(self.node_names):
                raise ValueError(
                    f"chunk {placement.chunk_id} names node index "
                    f"{placement.node_index}, but the fetch has "
                    f"{len(self.node_names)} nodes"
                )
            key = (placement.object_group_id, placement.chunk_id)
            if key in seen:
                # Two placements for one pair would double the layer's
                # expected slot count, so the layer could never complete.
                raise ValueError(
                    f"chunk {placement.chunk_id} is placed twice for object "
                    f"group {placement.object_group_id}"
                )
            seen.add(key)


@runtime_checkable
class SlotDigestSource(Protocol):
    """Names the stored record behind each slot.

    A digest is what the cluster is actually asked for, and it is derived
    from the record's key rather than from its geometry. The planner
    therefore knows which records a fetch needs but not what they are
    called, and asks this.

    Keeping it a lookup rather than a field on the request also resolves an
    ordering problem: which records exist depends on how planes are cut into
    pieces, which is the planner's own output, so a caller cannot enumerate
    the keys before planning.
    """

    def digest_for(self, chunk_id: int, layer_id: int, plane: int, piece: int) -> bytes:
        """Return the digest of one stored record.

        Args:
            chunk_id: Index of the KV chunk within the request.
            layer_id: Global layer index in the model.
            plane: Which K/V plane of the layer, counting from zero.
            piece: Which record of that plane, counting from zero in
                ascending offset order.

        Returns:
            The record digest, which must not be empty.

        Raises:
            KeyError: If no record was stored under that identity, since the
                plan would otherwise name a record the write side never
                produced.
        """
        ...


@dataclass(frozen=True)
class PlaneRun:
    """One kernel group's planes, as the write side is told to cut them.

    Mirrors ``PlaneRun`` in ``csrc/storage_backends/aerospike/shard_plan.h``.
    The writer keeps every record inside one plane, so it needs each kernel
    group's plane size separately: a hybrid object group holds kernel groups
    whose planes differ, and one size for the whole payload cannot describe
    it.

    Attributes:
        plane_bytes: Bytes in each plane of the kernel group.
        planes: Planes the kernel group holds, ``kv_planes * num_layers``.
    """

    plane_bytes: int
    planes: int


@dataclass(frozen=True)
class _LayerLocation:
    """Where one layer sits: its object group, kernel group and planes."""

    object_group_id: int
    kernel_group_index: int
    planes: tuple[ByteRange, ...]


def _parse_kernel_shape(shape: Sequence[int]) -> tuple[int, int, int]:
    """Split a registered kernel-group shape into its planning dimensions.

    Two ranks are supported. The standard KV shape is
    ``(kv_size, num_layers, num_slots, hidden_dim)``, where the key/value
    dimension is outermost. The engine format that puts the layer dimension
    outermost drops that leading dimension, giving
    ``(num_layers, num_slots, hidden_dim)``, which is the same arithmetic
    with one plane.

    Args:
        shape: The kernel group's tensor shape.

    Returns:
        ``(kv_planes, num_layers, plane_elements)`` where ``plane_elements``
        is ``num_slots * hidden_dim``.

    Raises:
        ValueError: If the rank is not three or four, or any dimension is not
            positive.
    """
    if len(shape) not in (3, 4):
        raise ValueError(
            f"expected a 3D or 4D kernel shape, got rank {len(shape)}: {tuple(shape)}"
        )
    if any(dim <= 0 for dim in shape):
        raise ValueError(
            f"kernel shape dimensions must be positive, got {tuple(shape)}"
        )
    if len(shape) == 4:
        kv_planes, num_layers, num_slots, hidden_dim = shape
    else:
        kv_planes = 1
        num_layers, num_slots, hidden_dim = shape
    return int(kv_planes), int(num_layers), int(num_slots) * int(hidden_dim)


def record_plane_runs(
    group_layout_descs: Mapping[int, "MemoryLayoutDesc"],
) -> dict[int, tuple[PlaneRun, ...]]:
    """Return how the write side should cut each object group's payload.

    This is what a storage backend is given at registration so that it keeps
    every record inside one plane of one layer. It reads the same shapes as
    :meth:`ModelLayout.from_registration`, through the same parser, and
    yields the same runs as :meth:`ModelLayout.plane_runs` -- but needs no
    layer indices, because where a record's bytes go in the payload does not
    depend on which global layer they belong to.

    Args:
        group_layout_descs: One layout per object group id, as published at
            registration. Each holds parallel lists of shapes and dtypes, one
            pair per kernel group in payload order.

    Returns:
        One run per kernel group, in payload order, per object group id.

    Raises:
        ValueError: If a group has no kernel groups, or a shape has an
            unsupported rank or a non-positive dimension.
    """
    runs: dict[int, tuple[PlaneRun, ...]] = {}
    for object_group_id, layout_desc in group_layout_descs.items():
        if not layout_desc.shapes:
            raise ValueError(f"object group {object_group_id} has no kernel groups")
        group_runs: list[PlaneRun] = []
        for shape, dtype in zip(layout_desc.shapes, layout_desc.dtypes, strict=True):
            kv_planes, num_layers, plane_elements = _parse_kernel_shape(shape)
            group_runs.append(
                PlaneRun(
                    plane_bytes=plane_elements * dtype.itemsize,
                    planes=kv_planes * num_layers,
                )
            )
        runs[object_group_id] = tuple(group_runs)
    return runs


def _unattributable_object_groups(
    group_bytes: Mapping[int, int], runs: Mapping[int, tuple[PlaneRun, ...]]
) -> frozenset[int]:
    """Return object groups whose payload size the writer cannot attribute.

    The writer is handed a key and a byte count, so it picks a record layout
    by payload size. Two object groups of the same size with different runs
    leave it unable to tell which layout a payload has, and it falls back to
    byte-count sharding for that size -- see ``record_layouts_by_payload`` in
    ``shard_plan.h``. Records of those groups do not follow layer boundaries.

    Args:
        group_bytes: Payload size per object group id.
        runs: Plane runs per object group id.

    Returns:
        Ids of every object group whose size another group shares with
        different runs.
    """
    runs_by_size: dict[int, set[tuple[PlaneRun, ...]]] = {}
    for object_group_id, size in group_bytes.items():
        runs_by_size.setdefault(size, set()).add(runs[object_group_id])
    return frozenset(
        object_group_id
        for object_group_id, size in group_bytes.items()
        if len(runs_by_size[size]) > 1
    )


class ModelLayout:
    """Resolves a global layer index to byte ranges within its object group.

    Built once from the registered layout and then read, so the lookup is a
    prepared mapping rather than a search. Immutable after construction and
    therefore safe to share across concurrently planned requests.

    An object group's payload is its kernel groups concatenated in the order
    declared, so a kernel group's base is the running sum of the sizes of
    those before it. Within a kernel group the key/value dimension is
    outermost, so layer ``L``'s planes are a whole layer dimension apart
    rather than adjacent.
    """

    def __init__(
        self, object_groups: Mapping[int, Sequence[KernelGroupGeometry]]
    ) -> None:
        """Build a layout over every object group of the model.

        Args:
            object_groups: Kernel groups per object group id, in the order
                their tensors are concatenated in the payload.

        Raises:
            ValueError: If there are no object groups, if an object group has
                no kernel groups, or if a global layer index appears in more
                than one kernel group, which would make its plane offsets
                ambiguous.
        """
        if not object_groups:
            raise ValueError("a layout must cover at least one object group")

        locations: dict[int, _LayerLocation] = {}
        group_bytes: dict[int, int] = {}
        runs: dict[int, tuple[PlaneRun, ...]] = {}
        run_bases: dict[int, tuple[int, ...]] = {}
        for object_group_id, kernel_groups in object_groups.items():
            if not kernel_groups:
                raise ValueError(f"object group {object_group_id} has no kernel groups")
            base = 0
            bases: list[int] = []
            for kernel_index, kernel_group in enumerate(kernel_groups):
                bases.append(base)
                num_layers = len(kernel_group.layer_ids)
                for position, layer_id in enumerate(kernel_group.layer_ids):
                    if layer_id in locations:
                        raise ValueError(
                            f"layer {layer_id} appears in more than one kernel "
                            "group, so its plane offsets would be ambiguous"
                        )
                    planes = tuple(
                        ByteRange(
                            offset=base
                            + (
                                ((plane * num_layers) + position)
                                * kernel_group.plane_bytes
                            ),
                            length=kernel_group.plane_bytes,
                        )
                        for plane in range(kernel_group.kv_planes)
                    )
                    locations[layer_id] = _LayerLocation(
                        object_group_id=object_group_id,
                        kernel_group_index=kernel_index,
                        planes=planes,
                    )
                base += kernel_group.total_bytes()
            group_bytes[object_group_id] = base
            run_bases[object_group_id] = tuple(bases)
            runs[object_group_id] = tuple(
                PlaneRun(
                    plane_bytes=kernel_group.plane_bytes,
                    planes=kernel_group.kv_planes * len(kernel_group.layer_ids),
                )
                for kernel_group in kernel_groups
            )

        self._locations = locations
        self._group_bytes = group_bytes
        self._runs = runs
        self._run_bases = run_bases
        self._unattributable = _unattributable_object_groups(group_bytes, runs)

    @classmethod
    def from_registration(
        cls,
        group_layout_descs: Mapping[int, "MemoryLayoutDesc"],
        group_kernel_layer_indices: Mapping[int, list[list[int]]] | None = None,
    ) -> "ModelLayout":
        """Build a layout from what LMCache publishes at registration.

        These are the exact arguments
        ``StorageManager.set_object_group_layouts`` receives, so this is the
        seam between the engine's view of the model and the planner's. Taking
        the geometry from the published layout rather than re-deriving it
        from model config keeps one source of truth for shapes and strides.

        Args:
            group_layout_descs: One layout per object group id. Each holds
                parallel lists of shapes and dtypes, one pair per kernel
                group.
            group_kernel_layer_indices: Global layer indices per kernel group,
                keyed by object group id and parallel to that group's shapes.
                When absent for an object group, consecutive indices are
                assigned from zero within it, which is correct only for a
                single-object-group model and is why real callers supply it.

        Returns:
            A layout covering every object group described.

        Raises:
            ValueError: If a group has no kernel groups, a shape has an
                unsupported rank or a non-positive dimension, or the supplied
                layer indices do not match the tensor's layer dimension.
        """
        object_groups: dict[int, list[KernelGroupGeometry]] = {}
        for object_group_id, layout_desc in group_layout_descs.items():
            per_group_indices = (group_kernel_layer_indices or {}).get(
                object_group_id, []
            )
            kernel_groups: list[KernelGroupGeometry] = []
            next_auto_layer = 0
            for index, (shape, dtype) in enumerate(
                zip(layout_desc.shapes, layout_desc.dtypes, strict=True)
            ):
                kv_planes, num_layers, plane_elements = _parse_kernel_shape(shape)
                if index < len(per_group_indices) and per_group_indices[index]:
                    layer_ids = tuple(per_group_indices[index])
                    if len(layer_ids) != num_layers:
                        raise ValueError(
                            f"object group {object_group_id} kernel group "
                            f"{index}: got {len(layer_ids)} layer indices for a "
                            f"layer dimension of {num_layers}"
                        )
                else:
                    layer_ids = tuple(
                        range(next_auto_layer, next_auto_layer + num_layers)
                    )
                    next_auto_layer += num_layers
                kernel_groups.append(
                    KernelGroupGeometry(
                        layer_ids=layer_ids,
                        kv_planes=kv_planes,
                        plane_bytes=plane_elements * dtype.itemsize,
                    )
                )
            object_groups[object_group_id] = kernel_groups
        return cls(object_groups)

    def layer_ids(self) -> tuple[int, ...]:
        """Return every global layer index in the layout, ascending.

        Returns:
            Each covered layer index exactly once, ascending.
        """
        return tuple(sorted(self._locations))

    def object_group_of_layer(self, layer_id: int) -> int:
        """Return the object group holding ``layer_id``.

        Args:
            layer_id: Global layer index in the model.

        Returns:
            The object group id.

        Raises:
            KeyError: If the layout does not cover ``layer_id``.
        """
        return self._layer_location(layer_id).object_group_id

    def layer_plane_ranges(self, layer_id: int) -> tuple[ByteRange, ...]:
        """Return the byte ranges ``layer_id`` occupies within its group.

        Offsets are relative to the start of the object group's payload, so a
        caller adds the chunk's destination offset to place them in the
        window.

        Args:
            layer_id: Global layer index in the model.

        Returns:
            One range per plane, ascending by offset.

        Raises:
            KeyError: If the layout does not cover ``layer_id``.
        """
        return self._layer_location(layer_id).planes

    def object_group_ids(self) -> tuple[int, ...]:
        """Return every object group id in the layout, ascending.

        Returns:
            Each covered object group id exactly once, ascending.
        """
        return tuple(sorted(self._group_bytes))

    def plane_runs(self, object_group_id: int) -> tuple[PlaneRun, ...]:
        """Return how the write side should cut one object group's payload.

        This is what the storage backend is handed at registration, one run
        per kernel group in payload order, so that it can keep every record
        inside one plane of one layer.

        Args:
            object_group_id: The object group to describe.

        Returns:
            One run per kernel group, in the order the payload holds them.

        Raises:
            KeyError: If the layout does not cover ``object_group_id``.
        """
        if object_group_id not in self._runs:
            raise KeyError(f"no object group {object_group_id} in the layout")
        return self._runs[object_group_id]

    def record_index_for(
        self, layer_id: int, plane: int, piece: int, max_record_bytes: int
    ) -> int:
        """Return which stored record of a chunk's object holds one slot.

        The plan says which bytes a slot carries; it does not say whether a
        record holding exactly those bytes was ever written. This is the join
        between the two, and it is what a real
        :class:`SlotDigestSource` needs in order to name a record: the write
        side stores an object's records under keys ending in their index, so
        the index is the last thing missing between a slot and its digest.

        Records are numbered in payload order, one kernel group at a time:
        every piece of the first kernel group's first plane, then its second
        plane, and so on, then the next kernel group. Each kernel group is
        cut against its own plane size. This is how ``segment_range`` in
        ``shard_plan.h`` resolves an index back to a range, and for a model
        whose planes are all one size it is the same numbering as a single
        plane-major sweep of the object.

        Args:
            layer_id: Global layer index in the model.
            plane: Which K/V plane of the layer, counting from zero.
            piece: Which record of that plane, counting from zero.
            max_record_bytes: Largest record the cluster will hold.

        Returns:
            The record's index within the chunk's object for that object
            group.

        Raises:
            KeyError: If the layout does not cover ``layer_id``.
            ValueError: If the layer's object group is one the write side
                cannot attribute a record layout to (see
                :meth:`record_count`), so no record matches a slot; if
                ``max_record_bytes`` is not positive; or if ``plane`` or
                ``piece`` is outside what the layer actually has.
        """
        location = self._layer_location(layer_id)
        self._require_attributable(location.object_group_id)
        if max_record_bytes <= 0:
            raise ValueError(
                f"max_record_bytes must be positive, got {max_record_bytes}"
            )
        if not 0 <= plane < len(location.planes):
            raise ValueError(
                f"layer {layer_id} has {len(location.planes)} planes, so plane "
                f"{plane} does not exist"
            )

        runs = self._runs[location.object_group_id]
        run = runs[location.kernel_group_index]
        pieces = _ceil_div(run.plane_bytes, max_record_bytes)
        if not 0 <= piece < pieces:
            raise ValueError(
                f"a plane of {run.plane_bytes} bytes under a {max_record_bytes} "
                f"byte cap is {pieces} pieces, so piece {piece} does not exist"
            )
        records_before = sum(
            earlier.planes * _ceil_div(earlier.plane_bytes, max_record_bytes)
            for earlier in runs[: location.kernel_group_index]
        )
        run_base = self._run_bases[location.object_group_id][
            location.kernel_group_index
        ]
        plane_in_run = (location.planes[plane].offset - run_base) // run.plane_bytes
        return records_before + (plane_in_run * pieces) + piece

    def record_count(self, object_group_id: int, max_record_bytes: int) -> int:
        """Return how many records one chunk's object is stored as.

        Needed because the write side names a single-record object
        differently from a sharded one, so a caller cannot form a record's
        key without knowing which case it is in.

        Args:
            object_group_id: The object group to size.
            max_record_bytes: Largest record the cluster will hold.

        Returns:
            The number of records the object occupies.

        Raises:
            KeyError: If the layout does not cover ``object_group_id``.
            ValueError: If ``max_record_bytes`` is not positive, or another
                object group has the same payload size with a different
                kernel-group layout. The write side picks a record layout by
                payload size alone, so for such a size it cannot tell the
                groups apart and shards by byte count instead.
        """
        runs = self.plane_runs(object_group_id)
        self._require_attributable(object_group_id)
        if max_record_bytes <= 0:
            raise ValueError(
                f"max_record_bytes must be positive, got {max_record_bytes}"
            )
        return sum(
            run.planes * _ceil_div(run.plane_bytes, max_record_bytes) for run in runs
        )

    def object_group_bytes(self, object_group_id: int) -> int:
        """Return the size of one chunk's object for an object group.

        Args:
            object_group_id: The object group to size.

        Returns:
            Total bytes of that group's payload for a single chunk.

        Raises:
            KeyError: If the layout does not cover ``object_group_id``.
        """
        if object_group_id not in self._group_bytes:
            raise KeyError(f"no object group {object_group_id} in the layout")
        return self._group_bytes[object_group_id]

    def _layer_location(self, layer_id: int) -> _LayerLocation:
        """Return where ``layer_id`` lives.

        Args:
            layer_id: Global layer index in the model.

        Returns:
            The layer's object group and plane ranges.

        Raises:
            KeyError: If the layout does not cover ``layer_id``.
        """
        location = self._locations.get(layer_id)
        if location is None:
            raise KeyError(f"no layer {layer_id} in the layout")
        return location

    def _require_attributable(self, object_group_id: int) -> None:
        """Refuse an object group whose records do not follow its layers.

        Args:
            object_group_id: The object group being named.

        Raises:
            ValueError: If another object group shares its payload size with
                a different kernel-group layout.
        """
        if object_group_id in self._unattributable:
            raise ValueError(
                f"object group {object_group_id} is "
                f"{self._group_bytes[object_group_id]} bytes, the same as another "
                "object group laid out differently; the write side cannot tell "
                "their payloads apart, shards that size by byte count, and no "
                "record lines up with a slot, so a layerwise fetch cannot be "
                "served for it"
            )


class RecordKeyDigests:
    """Names records by the keys the write side stored them under.

    Composes the two halves of the answer. Which record a slot needs comes
    from the layout; what that record is called comes from the key naming the
    connector uses; and turning a key into a digest is RIPEMD-160 over the
    Aerospike key, which is the client's job and is not available in Python
    -- most builds of OpenSSL disable RIPEMD-160 outright. So the hash is
    injected and everything around it is testable without a cluster.

    The key layout mirrors ``meta_user_key`` and ``segment_user_key`` in
    ``csrc/storage_backends/aerospike/connector.cpp``. An object stored as a
    single record lives under its meta key; a sharded one has its records
    suffixed by index. Getting that wrong asks for a record that does not
    exist, which at least fails loudly.
    """

    def __init__(
        self,
        layout: ModelLayout,
        max_record_bytes: int,
        cache_keys: Mapping[tuple[int, int], str],
        digest_of: Callable[[str], bytes],
    ) -> None:
        """Build a digest source for one request.

        Args:
            layout: Where each layer lives within its object group.
            max_record_bytes: Largest record the cluster will hold. Must be
                the value the object was *written* under, since it decides
                how many records exist.
            cache_keys: The stored object's cache key per ``(chunk id, object
                group id)``.
            digest_of: Maps a record's user key to its Aerospike digest.

        Raises:
            ValueError: If ``max_record_bytes`` is not positive.
        """
        if max_record_bytes <= 0:
            raise ValueError(
                f"max_record_bytes must be positive, got {max_record_bytes}"
            )
        self._layout = layout
        self._max_record_bytes = max_record_bytes
        self._cache_keys = dict(cache_keys)
        self._digest_of = digest_of

    def digest_for(self, chunk_id: int, layer_id: int, plane: int, piece: int) -> bytes:
        """Return the digest of the record holding one slot.

        Args:
            chunk_id: Index of the KV chunk within the request.
            layer_id: Global layer index in the model.
            plane: Which K/V plane of the layer, counting from zero.
            piece: Which record of that plane, counting from zero.

        Returns:
            The record's Aerospike digest.

        Raises:
            KeyError: If no object was stored for this chunk and object
                group, or the layout does not cover ``layer_id``.
            ValueError: If the model is one the write side does not shard
                along layer boundaries, or ``digest_of`` returns nothing.
        """
        object_group_id = self._layout.object_group_of_layer(layer_id)
        cache_key = self._cache_keys.get((chunk_id, object_group_id))
        if cache_key is None:
            raise KeyError(
                f"no object was stored for chunk {chunk_id} of object group "
                f"{object_group_id}, so this fetch cannot be served"
            )

        index = self._layout.record_index_for(
            layer_id, plane, piece, self._max_record_bytes
        )
        if self._layout.record_count(object_group_id, self._max_record_bytes) == 1:
            user_key = f"{cache_key}|m"
        else:
            user_key = f"{cache_key}|s|{index}"

        digest = self._digest_of(user_key)
        if not digest:
            raise ValueError(f"no digest for record {user_key!r}")
        return digest


class FetchPlanner:
    """Builds the slot layout for one pipelined fetch.

    Holds no per-request state, so one planner serves every request against a
    given model layout.
    """

    def __init__(self, layout: ModelLayout) -> None:
        """Build a planner over a model layout.

        Args:
            layout: Where each layer lives within its object group.
        """
        self._layout = layout

    def plan(self, request: PlanRequest, digests: SlotDigestSource) -> LayerFetchPlan:
        """Lay out every slot the fetch will request.

        Slots are appended layer-major -- every participating chunk's pieces
        of the lowest layer, then of the next, and so on -- which is the order
        the servers are asked to push in so the head of the pipeline arrives
        first. The order is only a hint: the fabric reorders writes in flight
        and independent nodes interleave regardless.

        A slot's index is **its position in the returned plan's** ``slots``.
        That numbering spans the whole request rather than restarting per
        node, which is what keeps two nodes' notifications distinguishable
        when they land on the same queue pair. Slot order is therefore
        load-bearing, and this method is deterministic for a given request.

        Placements for an object group the layout does not cover are ignored,
        and a layer whose object group the request did not place contributes
        no slots -- the correct answer for a layer this fetch is not after.

        Args:
            request: Which chunks to fetch and where they were placed.
            digests: Names the stored record behind each slot.

        Returns:
            A plan whose slots cover exactly the requested bytes, with no gaps
            and no overlaps.

        Raises:
            ValueError: If the placements cover none of the layout's layers,
                if the fetch needs more slots than the RDMA immediate can
                address, or if ``digests`` returns an empty digest. The
                device's own limit on writes in flight is checked by the
                transport, not here.
            KeyError: If ``digests`` has no record for a slot the fetch
                needs, which means the write side stored the chunk under a
                different geometry than this layout describes.
        """
        placements_by_group: dict[int, list[ChunkPlacement]] = {}
        for placement in request.placements:
            placements_by_group.setdefault(placement.object_group_id, []).append(
                placement
            )

        slots: list[SlotPlacement] = []
        for layer_id in self._layout.layer_ids():
            group_id = self._layout.object_group_of_layer(layer_id)
            group_placements = placements_by_group.get(group_id)
            if not group_placements:
                continue
            planes = self._layout.layer_plane_ranges(layer_id)
            for placement in group_placements:
                for plane_index, plane in enumerate(planes):
                    slots.extend(
                        self._plane_slots(
                            layer_id,
                            placement,
                            plane_index,
                            plane,
                            request.max_record_bytes,
                            digests,
                        )
                    )
                    if len(slots) > MAX_SLOTS_PER_REQUEST:
                        raise ValueError(
                            f"request needs more than {MAX_SLOTS_PER_REQUEST} "
                            "slots, which is all the RDMA immediate can "
                            "address; fetch fewer chunks per request or use a "
                            "coarser readiness granularity"
                        )

        if not slots:
            raise ValueError(
                "none of the placed object groups hold layers in this layout, "
                "so the fetch would expect no writes"
            )
        return LayerFetchPlan(tuple(slots), request.node_names)

    def participating_chunks(
        self,
        chunk_ids: Sequence[int],
        window_start_token: int,
        window_end_token: int,
        tokens_per_chunk: int,
    ) -> tuple[int, ...]:
        """Return the chunks a sliding window actually touches.

        This exists so call sites do not each re-derive it. Window arithmetic
        is easy to get subtly wrong -- particularly a window starting
        mid-chunk -- and a wrong answer here silently fetches the wrong
        tokens rather than failing. A chunk is included when it overlaps the
        window at all, including partially, since the model needs whatever
        part of it falls inside.

        A chunk id is its own index in the request, so chunk ``c`` spans
        tokens ``[c * tokens_per_chunk, (c + 1) * tokens_per_chunk - 1]``.
        Deriving the span from position in ``chunk_ids`` instead would shift
        every span whenever the caller passed a non-contiguous candidate
        list.

        Args:
            chunk_ids: Candidate chunk indices.
            window_start_token: First token index in the window, inclusive.
            window_end_token: Last token index in the window, inclusive.
            tokens_per_chunk: Number of tokens each chunk covers.

        Returns:
            The subset of ``chunk_ids`` overlapping the window, ascending.

        Raises:
            ValueError: If ``tokens_per_chunk`` is not positive, the window
                bounds are inverted, or a chunk id is negative.
        """
        if tokens_per_chunk <= 0:
            raise ValueError(
                f"tokens_per_chunk must be positive, got {tokens_per_chunk}"
            )
        if window_end_token < window_start_token:
            raise ValueError(
                f"window is inverted: start {window_start_token} is after "
                f"end {window_end_token}"
            )
        if any(chunk_id < 0 for chunk_id in chunk_ids):
            raise ValueError(f"chunk ids must not be negative: {tuple(chunk_ids)}")
        return tuple(
            chunk_id
            for chunk_id in sorted(set(chunk_ids))
            if chunk_id * tokens_per_chunk <= window_end_token
            and ((chunk_id + 1) * tokens_per_chunk) - 1 >= window_start_token
        )

    @staticmethod
    def _plane_slots(
        layer_id: int,
        placement: ChunkPlacement,
        plane_index: int,
        plane: ByteRange,
        max_record_bytes: int,
        digests: SlotDigestSource,
    ) -> list[SlotPlacement]:
        """Cut one plane of one layer of one chunk into record-sized slots.

        Args:
            layer_id: Global layer index in the model.
            placement: Where the chunk's object sits in the window.
            plane_index: Which K/V plane of the layer this is.
            plane: The plane's range within the object group's payload.
            max_record_bytes: Largest record the cluster will hold.
            digests: Names the stored record behind each slot.

        Returns:
            One slot per record of the plane, ascending by offset.

        Raises:
            ValueError: If ``digests`` returns an empty digest, which names
                no record.
            KeyError: If ``digests`` has no record for one of the pieces.
        """
        record_bytes = plane_segment_bytes(plane.length, max_record_bytes)
        base = placement.dest_offset + plane.offset
        slots: list[SlotPlacement] = []
        for piece, piece_offset in enumerate(range(0, plane.length, record_bytes)):
            digest = digests.digest_for(
                chunk_id=placement.chunk_id,
                layer_id=layer_id,
                plane=plane_index,
                piece=piece,
            )
            slots.append(
                SlotPlacement(
                    layer_id=layer_id,
                    chunk_id=placement.chunk_id,
                    node_index=placement.node_index,
                    digest=digest,
                    plane=plane_index,
                    piece=piece,
                    offset=base + piece_offset,
                    length=min(record_bytes, plane.length - piece_offset),
                )
            )
        return slots
