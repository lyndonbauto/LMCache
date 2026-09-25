# SPDX-License-Identifier: Apache-2.0
"""Track C: turning a registered layout and a request into a fetch plan.

These tests are the Python-side statement of acceptance criteria C1 to C5.
The same arithmetic is proven against the production C++ ``SlotPlanner`` by
the harness in ``tests/v1/distributed/rdma/``; what is checked here is the
path from what LMCache publishes at registration to a
:class:`LayerFetchPlan`, which is the piece nothing built before.

Everything here is pure CPU logic by design (C10). A test that needed a GPU
or a fabric would mean something had leaked across a contract boundary.
"""

# Standard
from collections.abc import Iterable, Sequence

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc
from lmcache.v1.layerwise import LayerFetchPlan
from lmcache.v1.layerwise.planner import (
    MAX_SLOTS_PER_REQUEST,
    ChunkPlacement,
    FetchPlanner,
    KernelGroupGeometry,
    ModelLayout,
    PlaneRun,
    PlanRequest,
    RecordKeys,
    plane_segment_bytes,
)

#: Chunk objects are spaced far enough apart that two chunks cannot overlap
#: by accident, so a coverage failure is always a planning bug.
OBJECT_STRIDE = 1 << 20


class FakeRecordKeys:
    """Names a distinct record for every ``(chunk, layer, plane, piece)``.

    The real write side stores one record per slot under its own key. This
    reproduces only the property the planner depends on -- that the identity
    is four-valued and the keys are distinct -- so that a slot given another
    slot's key is detectable.
    """

    def record_key_for(
        self, chunk_id: int, layer_id: int, plane: int, piece: int
    ) -> str:
        """Return a distinct key for one record identity."""
        return f"record-{chunk_id}:{layer_id}:{plane}:{piece}"


#: Shared because it is stateless; a test needing different behaviour builds
#: its own.
KEYS = FakeRecordKeys()


def uniform_layout(
    num_layers: int = 3,
    kv_planes: int = 2,
    plane_bytes: int = 4096,
    object_group_id: int = 0,
) -> ModelLayout:
    """Build a layout with one object group of one uniform kernel group.

    Args:
        num_layers: Layers in the model.
        kv_planes: Planes per layer. Two for separate key and value.
        plane_bytes: Bytes in one plane of one layer.
        object_group_id: Object group to place the kernel group in.

    Returns:
        A layout covering layers ``0`` to ``num_layers - 1``.
    """
    return ModelLayout(
        {
            object_group_id: [
                KernelGroupGeometry(
                    layer_ids=tuple(range(num_layers)),
                    kv_planes=kv_planes,
                    plane_bytes=plane_bytes,
                )
            ]
        }
    )


def place(
    chunk_ids: Iterable[int],
    object_group_id: int = 0,
    nodes: Sequence[int] | None = None,
) -> tuple[ChunkPlacement, ...]:
    """Place chunks one per ``OBJECT_STRIDE`` in the registered window.

    Args:
        chunk_ids: Chunks to place.
        object_group_id: Object group these placements are for.
        nodes: Node index per chunk, positionally. Defaults to one node per
            chunk, so plans built from it exercise request-scoped slot
            numbering rather than per-node numbering.

    Returns:
        One placement per chunk.
    """
    chunks = tuple(chunk_ids)
    if nodes is None:
        nodes = tuple(range(len(chunks)))
    # Offsets are keyed on the object group too, so a chunk that appears in
    # two groups gets two disjoint objects rather than one aliased one.
    base = object_group_id * len(chunks) * OBJECT_STRIDE
    return tuple(
        ChunkPlacement(
            chunk_id=chunk_id,
            object_group_id=object_group_id,
            node_index=nodes[i],
            dest_offset=base + (i * OBJECT_STRIDE),
        )
        for i, chunk_id in enumerate(chunks)
    )


def request_for(
    placements: tuple[ChunkPlacement, ...],
    max_record_bytes: int = 4096,
) -> PlanRequest:
    """Build a request naming exactly the nodes its placements refer to.

    Args:
        placements: The chunk placements to fetch.
        max_record_bytes: Largest record the cluster will hold.

    Returns:
        A request over those placements.
    """
    node_count = max((p.node_index for p in placements), default=0) + 1
    return PlanRequest(
        placements=placements,
        node_names=tuple(f"node-{i}" for i in range(node_count)),
        max_record_bytes=max_record_bytes,
    )


def assert_tiles_exactly(
    plan: LayerFetchPlan,
    placement: ChunkPlacement,
    object_bytes: int,
) -> None:
    """Assert the plan's slots for one placement tile its object exactly.

    Checks for gaps and overlaps together by walking the slots in offset
    order: a gap leaves the model reading whatever was in the window before,
    and an overlap means two writes race for the same bytes. Both produce
    plausible tensors and no error, which is why coverage is asserted rather
    than slot counts.

    Args:
        plan: The plan to inspect.
        placement: The chunk placement whose coverage is checked.
        object_bytes: Size of one chunk's object for that object group.
    """
    end = placement.dest_offset + object_bytes
    ranges = sorted(
        (slot.offset, slot.length)
        for slot in plan.slots
        if slot.chunk_id == placement.chunk_id
        and placement.dest_offset <= slot.offset < end
    )
    assert ranges, f"no slots planned for chunk {placement.chunk_id}"

    cursor = placement.dest_offset
    for offset, length in ranges:
        assert offset == cursor, (
            f"chunk {placement.chunk_id}: expected the next slot at {cursor} "
            f"but found one at {offset}"
        )
        cursor = offset + length
    assert cursor == placement.dest_offset + object_bytes


# --------------------------------------------------------------------------
# ModelLayout: reading the geometry LMCache publishes at registration.
# --------------------------------------------------------------------------


def test_a_four_dimensional_shape_is_read_as_separate_key_and_value() -> None:
    """The standard KV shape puts the key/value dimension outermost."""
    layout = ModelLayout.from_registration(
        {0: MemoryLayoutDesc([torch.Size([2, 4, 16, 64])], [torch.bfloat16])}
    )
    assert layout.layer_ids() == (0, 1, 2, 3)
    assert len(layout.layer_plane_ranges(0)) == 2
    assert layout.layer_plane_ranges(0)[0].length == 16 * 64 * 2


def test_a_three_dimensional_shape_is_read_as_one_plane_per_layer() -> None:
    """Dropping the leading dimension is the MLA case, not a special case."""
    layout = ModelLayout.from_registration(
        {0: MemoryLayoutDesc([torch.Size([4, 16, 64])], [torch.float16])}
    )
    assert layout.layer_ids() == (0, 1, 2, 3)
    assert len(layout.layer_plane_ranges(0)) == 1


def test_plane_size_follows_the_registered_dtype() -> None:
    """Element size comes from the published dtype, not from model config."""
    small = ModelLayout.from_registration(
        {0: MemoryLayoutDesc([torch.Size([1, 1, 4, 8])], [torch.uint8])}
    )
    large = ModelLayout.from_registration(
        {0: MemoryLayoutDesc([torch.Size([1, 1, 4, 8])], [torch.float32])}
    )
    assert small.layer_plane_ranges(0)[0].length == 32
    assert large.layer_plane_ranges(0)[0].length == 128


def test_supplied_layer_indices_are_used_verbatim() -> None:
    """Global layer indices are the engine's, not a local renumbering."""
    layout = ModelLayout.from_registration(
        {1: MemoryLayoutDesc([torch.Size([2, 3, 4, 8])], [torch.bfloat16])},
        {1: [[10, 11, 12]]},
    )
    assert layout.layer_ids() == (10, 11, 12)
    assert layout.object_group_of_layer(11) == 1


def test_layer_indices_of_the_wrong_length_are_rejected() -> None:
    """A mismatch would silently misattribute every layer in the group."""
    with pytest.raises(ValueError, match="layer indices"):
        ModelLayout.from_registration(
            {0: MemoryLayoutDesc([torch.Size([2, 3, 4, 8])], [torch.bfloat16])},
            {0: [[10, 11]]},
        )


@pytest.mark.parametrize(
    "shape",
    [torch.Size([4, 8]), torch.Size([2, 2, 4, 8, 2])],
)
def test_an_unsupported_shape_rank_is_rejected(shape: torch.Size) -> None:
    """Only the two known KV tensor ranks can be planned."""
    with pytest.raises(ValueError, match="3D or 4D"):
        ModelLayout.from_registration({0: MemoryLayoutDesc([shape], [torch.bfloat16])})


def test_a_layer_in_two_kernel_groups_is_rejected() -> None:
    """A duplicated layer has two sets of plane offsets, so it is ambiguous."""
    with pytest.raises(ValueError, match="more than one kernel group"):
        ModelLayout(
            {
                0: [
                    KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=64),
                    KernelGroupGeometry((1, 2), kv_planes=2, plane_bytes=64),
                ]
            }
        )


def test_object_group_bytes_is_the_sum_of_its_kernel_groups() -> None:
    """A chunk's object is its kernel groups' tensors concatenated."""
    layout = ModelLayout(
        {
            0: [
                KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=100),
                KernelGroupGeometry((2,), kv_planes=2, plane_bytes=300),
            ]
        }
    )
    assert layout.object_group_bytes(0) == (2 * 2 * 100) + (2 * 1 * 300)


# --------------------------------------------------------------------------
# C1. Plans come from real requests: exact coverage, correct attribution.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("num_layers", [1, 3, 8])
@pytest.mark.parametrize("kv_planes", [1, 2])
@pytest.mark.parametrize(
    "plane_bytes, max_record_bytes",
    [(4096, 4096), (10_000, 4096), (5000, 4096), (1024, 65_536), (7, 3)],
)
@pytest.mark.parametrize("num_chunks", [1, 4])
def test_slots_tile_every_chunks_object_exactly_once(
    num_layers: int,
    kv_planes: int,
    plane_bytes: int,
    max_record_bytes: int,
    num_chunks: int,
) -> None:
    """Across shapes and record caps, coverage is exact with no overlap.

    Swept rather than spot-checked because the failure this guards against
    is silent: a gap or an overlap yields a plausible tensor and no error.
    """
    layout = uniform_layout(num_layers, kv_planes, plane_bytes)
    placements = place(range(num_chunks))
    plan = FetchPlanner(layout).plan(
        request_for(placements, max_record_bytes=max_record_bytes),
        KEYS,
    )

    object_bytes = layout.object_group_bytes(0)
    for placement in placements:
        assert_tiles_exactly(plan, placement, object_bytes)
    assert plan.layer_ids() == tuple(range(num_layers))


def test_a_hybrid_object_group_tiles_exactly_across_kernel_groups() -> None:
    """Kernel groups of different geometry concatenate without a seam."""
    layout = ModelLayout(
        {
            0: [
                KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=100),
                KernelGroupGeometry((2,), kv_planes=2, plane_bytes=300),
            ]
        }
    )
    placements = place([0, 1])
    plan = FetchPlanner(layout).plan(
        request_for(placements, max_record_bytes=4096), KEYS
    )

    for placement in placements:
        assert_tiles_exactly(plan, placement, layout.object_group_bytes(0))


def test_each_slot_is_addressed_to_the_node_holding_its_own_chunk() -> None:
    """Node attribution follows the chunk, not the layer.

    Chunks are what the cluster distributes, so a slot sent to the wrong node
    names a record that node does not hold.
    """
    placements = place([0, 1, 2])
    by_chunk = {placement.chunk_id: placement for placement in placements}
    plan = FetchPlanner(uniform_layout()).plan(request_for(placements), KEYS)

    for slot in plan.slots:
        assert slot.node_index == by_chunk[slot.chunk_id].node_index
        assert plan.node_name_for(slot) == f"node-{slot.node_index}"


def test_each_slot_carries_the_key_of_the_record_it_names() -> None:
    """A slot's record key is looked up by its own four-part identity.

    A per-chunk or per-layer key would be wrong in the same silent way a
    coverage gap is: the transport would fetch a real record into the right
    address, and the model would read another piece's bytes. So every slot
    is checked against the source rather than against its neighbours.
    """
    plan = FetchPlanner(uniform_layout(num_layers=3, kv_planes=2)).plan(
        request_for(place([0, 1]), max_record_bytes=1024), KEYS
    )

    for slot in plan.slots:
        assert slot.record_key == KEYS.record_key_for(
            slot.chunk_id, slot.layer_id, slot.plane, slot.piece
        )
    assert len({slot.record_key for slot in plan.slots}) == len(plan.slots)


def test_a_source_that_cannot_name_a_record_fails_the_whole_plan() -> None:
    """A missing record is refused rather than planned around.

    Planning the rest would produce a fetch that can never complete, since
    the layer with the missing piece would stay pending forever.
    """

    class MissingOnePiece:
        def record_key_for(
            self, chunk_id: int, layer_id: int, plane: int, piece: int
        ) -> str:
            if layer_id == 1 and plane == 1:
                raise KeyError("no record")
            return KEYS.record_key_for(chunk_id, layer_id, plane, piece)

    with pytest.raises(KeyError):
        FetchPlanner(uniform_layout(num_layers=3)).plan(
            request_for(place([0])), MissingOnePiece()
        )


def test_a_source_returning_an_empty_key_is_rejected() -> None:
    """An empty key names no record, so it cannot reach the transport."""

    class EmptyKeys:
        def record_key_for(
            self, chunk_id: int, layer_id: int, plane: int, piece: int
        ) -> str:
            return ""

    with pytest.raises(ValueError, match="empty record key"):
        FetchPlanner(uniform_layout()).plan(request_for(place([0])), EmptyKeys())


def test_layers_of_an_unplaced_object_group_contribute_no_slots() -> None:
    """A layer the request is not fetching is simply absent from the plan.

    It is then never reported ready, which is the correct answer rather than
    a fetch that waits forever on data nobody asked for.
    """
    layout = ModelLayout(
        {
            0: [KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=128)],
            1: [KernelGroupGeometry((2, 3), kv_planes=2, plane_bytes=128)],
        }
    )
    plan = FetchPlanner(layout).plan(
        request_for(place([0], object_group_id=1), max_record_bytes=4096),
        KEYS,
    )
    assert plan.layer_ids() == (2, 3)


def test_a_request_placing_no_covered_group_is_rejected() -> None:
    """An empty plan is refused where the cause is still visible."""
    layout = uniform_layout(object_group_id=0)
    with pytest.raises(ValueError, match="no writes"):
        FetchPlanner(layout).plan(
            request_for(place([0], object_group_id=9), max_record_bytes=4096),
            KEYS,
        )


def test_layers_from_several_object_groups_interleave_by_layer_id() -> None:
    """Ordering is by global layer, not grouped by object group.

    The model consumes layers in order regardless of which group holds them,
    so a plan grouped by object group would fetch layer 3 before layer 1.
    """
    layout = ModelLayout(
        {
            0: [KernelGroupGeometry((0, 2), kv_planes=1, plane_bytes=64)],
            1: [KernelGroupGeometry((1, 3), kv_planes=1, plane_bytes=64)],
        }
    )
    plan = FetchPlanner(layout).plan(
        request_for(
            place([0], object_group_id=0) + place([0], object_group_id=1),
            max_record_bytes=4096,
        ),
        KEYS,
    )
    assert [slot.layer_id for slot in plan.slots] == [0, 1, 2, 3]


# --------------------------------------------------------------------------
# Naming the stored record behind a slot.
#
# Cross-checked against the write side's own sharding in
# tests/v1/distributed/rdma/test_slot_plan_parity.py; these pin the
# behaviour that does not need a compiler.
# --------------------------------------------------------------------------


def hybrid_layout() -> ModelLayout:
    """An attention kernel group beside a larger-plane one, in one object.

    Layers 0 and 1 have 1024-byte planes; layer 2 has 10000-byte planes,
    which a 4096-byte cap cuts into three pieces. No single plane size
    describes this payload, which is the case the writer used to give up on.
    """
    return ModelLayout(
        {
            0: [
                KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=1024),
                KernelGroupGeometry((2,), kv_planes=2, plane_bytes=10_000),
            ]
        }
    )


def test_plane_runs_describe_each_kernel_group_in_payload_order() -> None:
    """This is what the writer is handed, so it must match the payload."""
    layout = hybrid_layout()
    assert layout.plane_runs(0) == (
        PlaneRun(plane_bytes=1024, planes=4),
        PlaneRun(plane_bytes=10_000, planes=2),
    )
    assert layout.object_group_ids() == (0,)
    with pytest.raises(KeyError, match="object group 1"):
        layout.plane_runs(1)


def test_plane_runs_tile_the_object_exactly() -> None:
    """A run list that disagreed with the offsets would misplace records."""
    layout = ModelLayout(
        {
            0: [
                KernelGroupGeometry((3, 0), kv_planes=2, plane_bytes=512),
                KernelGroupGeometry((1,), kv_planes=1, plane_bytes=2048),
            ],
            1: [KernelGroupGeometry((2,), kv_planes=2, plane_bytes=768)],
        }
    )
    for object_group_id in layout.object_group_ids():
        runs = layout.plane_runs(object_group_id)
        assert sum(run.plane_bytes * run.planes for run in runs) == (
            layout.object_group_bytes(object_group_id)
        )


def test_records_are_numbered_plane_major_across_the_object() -> None:
    """Record order follows the object's planes, not the model's layers.

    A layer's two planes sit a whole layer dimension apart, so its records
    are far apart in the numbering even though the layer is one unit to the
    reader.
    """
    layout = uniform_layout(num_layers=3, kv_planes=2, plane_bytes=1024)
    indices = [
        layout.record_index_for(layer_id, plane, 0, max_record_bytes=4096)
        for plane in range(2)
        for layer_id in range(3)
    ]
    assert indices == [0, 1, 2, 3, 4, 5]


def test_a_plane_cut_into_pieces_numbers_them_within_the_plane() -> None:
    """Pieces are consecutive inside a plane before the next plane starts."""
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=10_000)
    # Three pieces per plane, so layer 1's key plane starts at record 3.
    assert layout.record_index_for(0, 0, 0, max_record_bytes=4096) == 0
    assert layout.record_index_for(0, 0, 2, max_record_bytes=4096) == 2
    assert layout.record_index_for(1, 0, 0, max_record_bytes=4096) == 3
    assert layout.record_index_for(0, 1, 0, max_record_bytes=4096) == 6


def test_a_hybrid_object_numbers_each_kernel_group_by_its_own_planes() -> None:
    """Each kernel group is cut against its own plane size, in payload order.

    The first group's four 1024-byte planes are one record each (0-3); the
    second group's two 10000-byte planes are three pieces each (4-9).
    """
    layout = hybrid_layout()
    assert layout.record_index_for(0, 0, 0, max_record_bytes=4096) == 0
    assert layout.record_index_for(1, 0, 0, max_record_bytes=4096) == 1
    assert layout.record_index_for(0, 1, 0, max_record_bytes=4096) == 2
    assert layout.record_index_for(1, 1, 0, max_record_bytes=4096) == 3
    assert layout.record_index_for(2, 0, 0, max_record_bytes=4096) == 4
    assert layout.record_index_for(2, 0, 2, max_record_bytes=4096) == 6
    assert layout.record_index_for(2, 1, 0, max_record_bytes=4096) == 7
    assert layout.record_count(0, max_record_bytes=4096) == 10


def test_a_small_plane_does_not_borrow_a_larger_groups_piece_count() -> None:
    """Pieces are counted per kernel group, not taken from the largest plane."""
    layout = hybrid_layout()
    with pytest.raises(ValueError, match="piece 1 does not exist"):
        layout.record_index_for(0, 0, 1, max_record_bytes=4096)
    with pytest.raises(ValueError, match="piece 3 does not exist"):
        layout.record_index_for(2, 0, 3, max_record_bytes=4096)


def test_every_slot_of_a_hybrid_plan_names_a_distinct_record() -> None:
    """The plan and the numbering agree: each record is fetched exactly once."""
    layout = hybrid_layout()
    plan = FetchPlanner(layout).plan(request_for(place([0, 1])), KEYS)

    by_chunk: dict[int, list[int]] = {}
    for slot in plan.slots:
        by_chunk.setdefault(slot.chunk_id, []).append(
            layout.record_index_for(slot.layer_id, slot.plane, slot.piece, 4096)
        )
    for chunk_id, indices in by_chunk.items():
        assert sorted(indices) == list(range(layout.record_count(0, 4096))), (
            f"chunk {chunk_id} does not name every record exactly once"
        )


def test_naming_is_refused_for_a_payload_size_the_writer_cannot_attribute() -> None:
    """Two differently laid out groups of one size get byte-count records.

    The writer picks a layout by payload size, so it cannot align either
    group; returning a plausible index would name a record holding other
    layers' bytes, which the transport would happily fetch. A group of a
    different size is unaffected.
    """
    layout = ModelLayout(
        {
            0: [KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=1024)],
            1: [KernelGroupGeometry((2,), kv_planes=1, plane_bytes=4096)],
            2: [KernelGroupGeometry((3,), kv_planes=2, plane_bytes=1024)],
        }
    )
    for layer_id, object_group_id in ((0, 0), (2, 1)):
        with pytest.raises(ValueError, match="cannot tell"):
            layout.record_index_for(layer_id, 0, 0, max_record_bytes=4096)
        with pytest.raises(ValueError, match="cannot tell"):
            layout.record_count(object_group_id, max_record_bytes=4096)
    assert layout.record_index_for(3, 1, 0, max_record_bytes=4096) == 1


def test_same_sized_groups_with_the_same_layout_stay_nameable() -> None:
    """Identical layouts are not ambiguous: either reading of the size agrees."""
    layout = ModelLayout(
        {
            0: [KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=1024)],
            1: [KernelGroupGeometry((2, 3), kv_planes=2, plane_bytes=1024)],
        }
    )
    assert layout.record_index_for(3, 1, 0, max_record_bytes=4096) == 3
    assert layout.record_count(1, max_record_bytes=4096) == 4


@pytest.mark.parametrize(
    "plane, piece, match",
    [(2, 0, "plane 2 does not exist"), (0, 3, "piece 3 does not exist")],
)
def test_naming_a_record_outside_the_layer_is_refused(
    plane: int, piece: int, match: str
) -> None:
    """An out-of-range plane or piece names a record of another layer."""
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=4096)
    with pytest.raises(ValueError, match=match):
        layout.record_index_for(0, plane, piece, max_record_bytes=4096)


def test_every_slot_of_a_plan_names_a_distinct_record() -> None:
    """Two slots sharing a record would each report the other's layer ready.

    Uniqueness is checked per chunk, since records are numbered within a
    chunk's object and two chunks reuse the same numbers.
    """
    layout = uniform_layout(num_layers=3, kv_planes=2, plane_bytes=10_000)
    plan = FetchPlanner(layout).plan(request_for(place([0, 1])), KEYS)

    by_chunk: dict[int, list[int]] = {}
    for slot in plan.slots:
        by_chunk.setdefault(slot.chunk_id, []).append(
            layout.record_index_for(slot.layer_id, slot.plane, slot.piece, 4096)
        )
    for chunk_id, indices in by_chunk.items():
        assert len(set(indices)) == len(indices), f"chunk {chunk_id} reuses a record"
        assert sorted(indices) == list(range(len(indices)))


# --------------------------------------------------------------------------
# Naming records by the keys the write side stored them under.
# --------------------------------------------------------------------------


def test_a_sharded_object_names_its_records_by_index() -> None:
    """Record keys follow the write side's segment naming."""
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=4096)
    source = RecordKeys(layout, 4096, {(7, 0): "cache-key"})

    key = source.record_key_for(chunk_id=7, layer_id=1, plane=1, piece=0)
    assert key == "cache-key|s|3"


def test_an_object_stored_as_one_record_uses_its_meta_key() -> None:
    """A single-record object is written under the meta key, not |s|0.

    The write side takes that branch whenever the object is one record, so a
    reader that always appended an index would ask for a key that was never
    stored.
    """
    layout = uniform_layout(num_layers=1, kv_planes=1, plane_bytes=4096)
    assert layout.record_count(0, 4096) == 1
    source = RecordKeys(layout, 4096, {(0, 0): "cache-key"})

    key = source.record_key_for(chunk_id=0, layer_id=0, plane=0, piece=0)
    assert key == "cache-key|m"


def test_each_chunk_is_named_by_its_own_stored_object() -> None:
    """Chunks are separate objects, so they must not share a cache key."""
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=4096)
    source = RecordKeys(layout, 4096, {(0, 0): "key-a", (1, 0): "key-b"})

    first = source.record_key_for(chunk_id=0, layer_id=0, plane=0, piece=0)
    second = source.record_key_for(chunk_id=1, layer_id=0, plane=0, piece=0)
    assert (first, second) == ("key-a|s|0", "key-b|s|0")


def test_a_chunk_with_no_stored_object_is_reported_not_guessed() -> None:
    """A miss must reach the planner, which refuses the whole fetch."""
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=4096)
    source = RecordKeys(layout, 4096, {(0, 0): "key-a"})

    with pytest.raises(KeyError, match="chunk 4"):
        source.record_key_for(chunk_id=4, layer_id=0, plane=0, piece=0)


def test_a_planned_fetch_asks_for_every_record_of_every_chunk_once() -> None:
    """End to end: the plan's slots name each stored record exactly once."""
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=10_000)
    source = RecordKeys(layout, 4096, {(0, 0): "key-a", (1, 0): "key-b"})

    plan = FetchPlanner(layout).plan(request_for(place([0, 1])), source)

    records = layout.record_count(0, 4096)
    assert len(plan.slots) == 2 * records
    assert sorted(slot.record_key for slot in plan.slots) == sorted(
        f"{key}|s|{index}" for key in ("key-a", "key-b") for index in range(records)
    )


def test_a_hybrid_fetch_asks_for_every_record_of_its_object_once() -> None:
    """End to end for a hybrid object: every stored record, none twice."""
    layout = hybrid_layout()
    source = RecordKeys(layout, 4096, {(0, 0): "key-a"})

    plan = FetchPlanner(layout).plan(request_for(place([0])), source)
    assert sorted(slot.record_key for slot in plan.slots) == sorted(
        f"key-a|s|{index}" for index in range(10)
    )


def test_an_unattributable_payload_size_cannot_be_named_at_all() -> None:
    """The refusal propagates, so no fetch is planned against bad records."""
    layout = ModelLayout(
        {
            0: [KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=1024)],
            1: [KernelGroupGeometry((2,), kv_planes=1, plane_bytes=4096)],
        }
    )
    source = RecordKeys(layout, 4096, {(0, 0): "key-a"})

    with pytest.raises(ValueError, match="cannot tell"):
        FetchPlanner(layout).plan(request_for(place([0])), source)


# --------------------------------------------------------------------------
# C2. A layer is kv_planes disjoint ranges, not one.
# --------------------------------------------------------------------------


def test_a_layer_occupies_one_range_per_plane_and_they_are_not_adjacent() -> None:
    """A layer's key and value halves sit a whole layer dimension apart.

    The key/value dimension is outermost, so the key plane of every layer
    precedes the value plane of any layer. A planner that emitted one range
    per layer would deliver the key half, see its single slot land, report
    the layer ready, and hand the model a cache whose value half is stale.
    Non-adjacency is asserted because a contiguous pair would mean the
    striding had collapsed.
    """
    num_layers, plane_bytes = 3, 4096
    layout = uniform_layout(num_layers, kv_planes=2, plane_bytes=plane_bytes)
    plan = FetchPlanner(layout).plan(
        request_for(place([0]), max_record_bytes=4096), KEYS
    )

    for layer_id in range(num_layers):
        offsets = sorted(
            slot.offset for slot in plan.slots if slot.layer_id == layer_id
        )
        assert len(offsets) == 2
        assert offsets[1] - offsets[0] == num_layers * plane_bytes
        assert offsets[1] - offsets[0] > plane_bytes


def test_each_kernel_group_is_strided_by_its_own_geometry() -> None:
    """One group's stride is never applied to another group's layers.

    This is the specific hazard a hybrid model introduces: the two groups
    have different plane sizes and different layer counts, so a single
    flattened stride would place one of them wrongly while still producing a
    fully covered, plausible-looking object.
    """
    layout = ModelLayout(
        {
            0: [
                KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=100),
                KernelGroupGeometry((2,), kv_planes=2, plane_bytes=300),
            ]
        }
    )
    plan = FetchPlanner(layout).plan(
        request_for(place([0]), max_record_bytes=4096), KEYS
    )

    def stride_of(layer_id: int) -> int:
        offsets = sorted(
            slot.offset for slot in plan.slots if slot.layer_id == layer_id
        )
        return offsets[1] - offsets[0]

    # Two layers of 100-byte planes, so one layer dimension is 200.
    assert stride_of(0) == 200
    assert stride_of(1) == 200
    # One layer of 300-byte planes, so one layer dimension is 300.
    assert stride_of(2) == 300


def test_a_single_plane_layout_yields_one_range_per_layer() -> None:
    """With one plane per layer the layer is contiguous, as MLA expects."""
    layout = uniform_layout(num_layers=2, kv_planes=1)
    plan = FetchPlanner(layout).plan(
        request_for(place([0]), max_record_bytes=4096), KEYS
    )
    for layer_id in (0, 1):
        assert plan.slots_for_layer(layer_id) == 1


# --------------------------------------------------------------------------
# C4. Slot indices are request-scoped, so order is load-bearing.
# --------------------------------------------------------------------------


def test_slot_position_is_unique_across_the_whole_request() -> None:
    """Slot numbering spans the request, not each node separately.

    A slot's index is its position in the plan. If numbering restarted per
    node, node 1's slot 5 and node 0's slot 5 would be indistinguishable in
    the readiness table: one arrival would be counted as a duplicate and its
    layer would never complete.
    """
    placements = place([0, 1, 2], nodes=(0, 1, 0))
    plan = FetchPlanner(uniform_layout()).plan(
        request_for(placements, max_record_bytes=4096),
        KEYS,
    )

    positions_by_node: dict[int, set[int]] = {}
    for index, slot in enumerate(plan.slots):
        positions_by_node.setdefault(slot.node_index, set()).add(index)

    assert set(positions_by_node) == {0, 1}
    assert positions_by_node[0].isdisjoint(positions_by_node[1])
    assert positions_by_node[0] | positions_by_node[1] == set(range(len(plan.slots)))


def test_slots_are_ordered_layer_major() -> None:
    """Every chunk's layer 0 is requested before any chunk's layer 1.

    The order is a hint to the servers rather than a guarantee, but it is the
    hint that makes the head of the pipeline arrive first.
    """
    plan = FetchPlanner(uniform_layout(num_layers=3)).plan(
        request_for(place([0, 1]), max_record_bytes=4096),
        KEYS,
    )
    layer_sequence = [slot.layer_id for slot in plan.slots]
    assert layer_sequence == sorted(layer_sequence)


def test_planning_the_same_request_twice_gives_identical_slot_order() -> None:
    """Slot position is the wire identity, so it cannot vary between runs."""
    planner = FetchPlanner(uniform_layout())
    request = request_for(place([0, 1]), max_record_bytes=4096)
    assert planner.plan(request, KEYS).slots == planner.plan(request, KEYS).slots


def test_slot_count_per_layer_matches_what_the_transport_will_wait_for() -> None:
    """A layer is resident once every slot carrying part of it has landed.

    The transport checks its own accounting against this, so an off-by-one
    here becomes a fetch that hangs or one that completes early.
    """
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=10_000)
    placements = place([0, 1])
    plan = FetchPlanner(layout).plan(
        request_for(placements, max_record_bytes=4096),
        KEYS,
    )
    pieces_per_plane = 3
    for layer_id in (0, 1):
        assert plan.slots_for_layer(layer_id) == (
            len(placements) * 2 * pieces_per_plane
        )


# --------------------------------------------------------------------------
# C5. Record cutting, the per-command sink cap, and the slot space.
# --------------------------------------------------------------------------


def test_a_plane_larger_than_the_record_cap_becomes_several_slots() -> None:
    """A plane is cut into records, and a slot is exactly one record.

    A sink names one record and one destination, so a slot larger than its
    record is unservable and a slot smaller than one names no particular
    part of it.
    """
    layout = uniform_layout(num_layers=1, kv_planes=1, plane_bytes=10_000)
    plan = FetchPlanner(layout).plan(
        request_for(place([0]), max_record_bytes=4096), KEYS
    )

    lengths = [slot.length for slot in plan.slots]
    assert len(lengths) == 3
    assert sum(lengths) == 10_000
    assert max(lengths) <= 4096


def test_a_plane_is_cut_into_evenly_sized_records() -> None:
    """Pieces are equal fractions of a plane, not a full cap plus a remainder.

    Matching the shard plan matters because the write side derives the same
    answer independently; a planner that emitted 4096 + 904 would name
    records that were never written.
    """
    layout = uniform_layout(num_layers=1, kv_planes=1, plane_bytes=5000)
    plan = FetchPlanner(layout).plan(
        request_for(place([0]), max_record_bytes=4096), KEYS
    )
    assert [slot.length for slot in plan.slots] == [2500, 2500]


@pytest.mark.parametrize(
    "plane_bytes, max_record_bytes, expected",
    [
        (4096, 4096, 4096),
        (4097, 4096, 2049),
        (5000, 4096, 2500),
        (10_000, 4096, 3334),
        (7, 3, 3),
        (1024, 65_536, 1024),
    ],
)
def test_plane_segment_bytes_matches_the_shard_plan_formula(
    plane_bytes: int, max_record_bytes: int, expected: int
) -> None:
    """Concrete values are pinned, not the formula restated.

    The C++ side computes this independently. Pinning bytes is what would
    catch the two drifting apart; asserting the formula against itself would
    not.
    """
    assert plane_segment_bytes(plane_bytes, max_record_bytes) == expected


def test_a_plan_is_splittable_into_per_node_commands_within_the_sink_cap() -> None:
    """A node's slots can be issued as commands of at most max_sinks sinks.

    The plan is request-scoped but issued per node, so what matters is that
    each node's own slots can be chunked without renumbering them.
    """
    max_sinks = 256
    layout = uniform_layout(num_layers=40)
    plan = FetchPlanner(layout).plan(
        request_for(place([0, 1]), max_record_bytes=4096),
        KEYS,
    )

    covered: set[int] = set()
    for node_index in {slot.node_index for slot in plan.slots}:
        positions = [
            index
            for index, slot in enumerate(plan.slots)
            if slot.node_index == node_index
        ]
        commands = [
            positions[i : i + max_sinks] for i in range(0, len(positions), max_sinks)
        ]
        assert commands
        assert all(len(command) <= max_sinks for command in commands)
        covered.update(position for command in commands for position in command)
    assert covered == set(range(len(plan.slots)))


def test_a_request_exceeding_the_slot_space_is_rejected_not_truncated() -> None:
    """Too many slots fails loudly rather than silently dropping writes.

    The immediate carries 16 bits of slot index. A truncated plan would look
    valid and hang on the layers whose slots were dropped.
    """
    layout = uniform_layout(num_layers=80, kv_planes=2, plane_bytes=4096)
    placements = place(range(600), nodes=tuple(0 for _ in range(600)))
    with pytest.raises(ValueError, match="slots"):
        FetchPlanner(layout).plan(
            request_for(placements, max_record_bytes=4096),
            KEYS,
        )


def test_the_slot_space_matches_the_immediate_encoding() -> None:
    """The ceiling is the 16 bits the RDMA immediate reserves for a slot."""
    assert MAX_SLOTS_PER_REQUEST == 0x10000


# --------------------------------------------------------------------------
# C3. Sliding windows.
# --------------------------------------------------------------------------


def test_the_window_helper_selects_only_overlapping_chunks() -> None:
    """Chunks wholly outside the window do not participate.

    Requiring them would wait forever on chunks the group never covers.
    """
    planner = FetchPlanner(uniform_layout())
    assert planner.participating_chunks((0, 1, 2, 3), 32, 63, 16) == (2, 3)


def test_a_window_starting_mid_chunk_includes_that_chunk() -> None:
    """A partially covered chunk still participates.

    This is the boundary most easily got wrong, and getting it wrong fetches
    the wrong tokens rather than failing.
    """
    planner = FetchPlanner(uniform_layout())
    assert planner.participating_chunks((0, 1, 2), 8, 40, 16) == (0, 1, 2)


def test_a_window_inside_one_chunk_selects_only_that_chunk() -> None:
    """The narrowest window still resolves to the chunk containing it."""
    planner = FetchPlanner(uniform_layout())
    assert planner.participating_chunks((0, 1, 2), 17, 18, 16) == (1,)


def test_a_window_ending_on_a_chunk_boundary_excludes_the_next_chunk() -> None:
    """Bounds are inclusive, so token 31 is the last token of chunk 1."""
    planner = FetchPlanner(uniform_layout())
    assert planner.participating_chunks((0, 1, 2), 16, 31, 16) == (1,)
    assert planner.participating_chunks((0, 1, 2), 16, 32, 16) == (1, 2)


def test_a_chunk_id_is_its_own_index_in_the_request() -> None:
    """A chunk's token span comes from its id, not its position in the list.

    Chunk ids are indices into the request's chunk list, which is how the
    transport reconciles a request-scoped plan with per-node commands. A
    helper that used position instead would silently shift every span when
    the caller passed a non-contiguous candidate list.
    """
    planner = FetchPlanner(uniform_layout())
    assert planner.participating_chunks((0, 1, 2, 3), 48, 63, 16) == (3,)
    assert planner.participating_chunks((0, 2), 32, 47, 16) == (2,)


def test_a_windowed_group_plans_only_its_own_chunks() -> None:
    """The helper and the planner compose: a window narrows the fetch.

    A sliding-window group covers only trailing chunks, so planning it over
    every chunk of the prompt would expect writes that never come.
    """
    planner = FetchPlanner(uniform_layout(num_layers=2))
    windowed = planner.participating_chunks((0, 1, 2, 3), 32, 63, 16)
    plan = planner.plan(request_for(place(windowed), max_record_bytes=4096), KEYS)
    assert {slot.chunk_id for slot in plan.slots} == {2, 3}


@pytest.mark.parametrize(
    "args, match",
    [
        (((0, 1), 32, 16, 16), "window"),
        (((0, 1), 0, 16, 0), "tokens_per_chunk"),
        (((0, -1), 0, 16, 16), "negative"),
    ],
)
def test_the_window_helper_rejects_nonsense(
    args: tuple[Sequence[int], int, int, int], match: str
) -> None:
    """Bad window arithmetic fails rather than returning a plausible subset."""
    with pytest.raises(ValueError, match=match):
        FetchPlanner(uniform_layout()).participating_chunks(*args)


# --------------------------------------------------------------------------
# Inputs that cannot describe a fetch are rejected where they are built.
# --------------------------------------------------------------------------


def test_placing_a_chunk_twice_for_one_group_is_rejected() -> None:
    """A repeat would double the layer's expected slots, so it never completes."""
    placement = ChunkPlacement(0, 0, 0, 0)
    with pytest.raises(ValueError, match="placed twice"):
        request_for((placement, placement))


def test_the_same_chunk_may_be_placed_once_per_object_group() -> None:
    """One chunk has a separate object in every group it participates in."""
    request = request_for(
        (
            ChunkPlacement(0, 0, 0, 0),
            ChunkPlacement(0, 1, 0, 4096),
        )
    )
    assert len(request.placements) == 2


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"dest_offset": -1}, "negative destination"),
        ({"node_index": -1}, "negative node"),
    ],
)
def test_an_unusable_placement_is_rejected(
    kwargs: dict[str, object], match: str
) -> None:
    """A placement that names no node or no location cannot be fetched."""
    fields: dict[str, object] = {
        "chunk_id": 0,
        "object_group_id": 0,
        "node_index": 0,
        "dest_offset": 0,
    }
    fields.update(kwargs)
    with pytest.raises(ValueError, match=match):
        ChunkPlacement(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "node_names, match",
    [
        ((), "at least one node"),
        (("node-a", ""), "empty name"),
        (("node-a", "node-a"), "repeats a node name"),
    ],
)
def test_a_request_with_an_unusable_node_list_is_rejected(
    node_names: tuple[str, ...], match: str
) -> None:
    """Node indices are only meaningful against an unambiguous node list."""
    with pytest.raises(ValueError, match=match):
        PlanRequest(
            placements=(ChunkPlacement(0, 0, 0, 0),),
            node_names=node_names,
            max_record_bytes=4096,
        )


def test_a_placement_naming_a_node_the_fetch_does_not_have_is_rejected() -> None:
    """An out-of-range index would address the fetch to the wrong node."""
    with pytest.raises(ValueError, match="node index 2"):
        PlanRequest(
            placements=(ChunkPlacement(0, 0, 2, 0),),
            node_names=("node-a", "node-b"),
            max_record_bytes=4096,
        )


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"layer_ids": ()}, "at least one layer"),
        ({"layer_ids": (0, 0)}, "repeats a layer"),
        ({"kv_planes": 0}, "kv_planes"),
        ({"plane_bytes": 0}, "plane_bytes"),
    ],
)
def test_an_unplannable_kernel_group_is_rejected(
    kwargs: dict[str, object], match: str
) -> None:
    """Geometry that could never complete a layer is refused on construction."""
    fields: dict[str, object] = {
        "layer_ids": (0, 1),
        "kv_planes": 2,
        "plane_bytes": 64,
    }
    fields.update(kwargs)
    with pytest.raises(ValueError, match=match):
        KernelGroupGeometry(**fields)  # type: ignore[arg-type]


def test_an_empty_request_or_layout_is_rejected() -> None:
    """Neither an empty fetch nor an empty model can be planned."""
    with pytest.raises(ValueError, match="at least one chunk"):
        request_for((), max_record_bytes=4096)
    with pytest.raises(ValueError, match="at least one object group"):
        ModelLayout({})
    with pytest.raises(ValueError, match="no kernel groups"):
        ModelLayout({0: []})


def test_an_unknown_layer_is_reported_rather_than_guessed() -> None:
    """Asking about a layer outside the layout fails loudly."""
    layout = uniform_layout(num_layers=2)
    with pytest.raises(KeyError, match="no layer 9"):
        layout.layer_plane_ranges(9)
    with pytest.raises(KeyError, match="no object group 9"):
        layout.object_group_bytes(9)
