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
import hashlib

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
    PlanRequest,
    plane_segment_bytes,
)

#: Chunk objects are spaced far enough apart that two chunks cannot overlap
#: by accident, so a coverage failure is always a planning bug.
OBJECT_STRIDE = 1 << 20


class FakeDigests:
    """Names a distinct record for every ``(chunk, layer, plane, piece)``.

    The real write side stores one record per slot and derives its digest
    from that record's key. This reproduces only the property the planner
    depends on -- that the identity is four-valued and the digests are
    distinct -- so that a slot given another slot's digest is detectable.
    """

    def digest_for(self, chunk_id: int, layer_id: int, plane: int, piece: int) -> bytes:
        """Return a distinct 20-byte digest for one record identity."""
        key = f"{chunk_id}:{layer_id}:{plane}:{piece}".encode()
        return hashlib.blake2b(key, digest_size=20).digest()


#: Shared because it is stateless; a test needing different behaviour builds
#: its own.
DIGESTS = FakeDigests()


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
        DIGESTS,
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
        request_for(placements, max_record_bytes=4096), DIGESTS
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
    plan = FetchPlanner(uniform_layout()).plan(request_for(placements), DIGESTS)

    for slot in plan.slots:
        assert slot.node_index == by_chunk[slot.chunk_id].node_index
        assert plan.node_name_for(slot) == f"node-{slot.node_index}"


def test_each_slot_carries_the_digest_of_the_record_it_names() -> None:
    """A slot's digest is looked up by its own four-part record identity.

    A per-chunk or per-layer digest would be wrong in the same silent way a
    coverage gap is: the transport would fetch a real record into the right
    address, and the model would read another piece's bytes. So every slot
    is checked against the source rather than against its neighbours.
    """
    plan = FetchPlanner(uniform_layout(num_layers=3, kv_planes=2)).plan(
        request_for(place([0, 1]), max_record_bytes=1024), DIGESTS
    )

    for slot in plan.slots:
        assert slot.digest == DIGESTS.digest_for(
            slot.chunk_id, slot.layer_id, slot.plane, slot.piece
        )
    assert len({slot.digest for slot in plan.slots}) == len(plan.slots)


def test_a_source_that_cannot_name_a_record_fails_the_whole_plan() -> None:
    """A missing record is refused rather than planned around.

    Planning the rest would produce a fetch that can never complete, since
    the layer with the missing piece would stay pending forever.
    """

    class MissingOnePiece:
        def digest_for(
            self, chunk_id: int, layer_id: int, plane: int, piece: int
        ) -> bytes:
            if layer_id == 1 and plane == 1:
                raise KeyError("no record")
            return DIGESTS.digest_for(chunk_id, layer_id, plane, piece)

    with pytest.raises(KeyError):
        FetchPlanner(uniform_layout(num_layers=3)).plan(
            request_for(place([0])), MissingOnePiece()
        )


def test_a_source_returning_an_empty_digest_is_rejected() -> None:
    """An empty digest names no record, so it cannot reach the transport."""

    class EmptyDigests:
        def digest_for(
            self, chunk_id: int, layer_id: int, plane: int, piece: int
        ) -> bytes:
            return b""

    with pytest.raises(ValueError, match="empty digest"):
        FetchPlanner(uniform_layout()).plan(request_for(place([0])), EmptyDigests())


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
        DIGESTS,
    )
    assert plan.layer_ids() == (2, 3)


def test_a_request_placing_no_covered_group_is_rejected() -> None:
    """An empty plan is refused where the cause is still visible."""
    layout = uniform_layout(object_group_id=0)
    with pytest.raises(ValueError, match="no writes"):
        FetchPlanner(layout).plan(
            request_for(place([0], object_group_id=9), max_record_bytes=4096),
            DIGESTS,
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
        DIGESTS,
    )
    assert [slot.layer_id for slot in plan.slots] == [0, 1, 2, 3]


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
        request_for(place([0]), max_record_bytes=4096), DIGESTS
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
        request_for(place([0]), max_record_bytes=4096), DIGESTS
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
        request_for(place([0]), max_record_bytes=4096), DIGESTS
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
        DIGESTS,
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
        DIGESTS,
    )
    layer_sequence = [slot.layer_id for slot in plan.slots]
    assert layer_sequence == sorted(layer_sequence)


def test_planning_the_same_request_twice_gives_identical_slot_order() -> None:
    """Slot position is the wire identity, so it cannot vary between runs."""
    planner = FetchPlanner(uniform_layout())
    request = request_for(place([0, 1]), max_record_bytes=4096)
    assert planner.plan(request, DIGESTS).slots == planner.plan(request, DIGESTS).slots


def test_slot_count_per_layer_matches_what_the_transport_will_wait_for() -> None:
    """A layer is resident once every slot carrying part of it has landed.

    The transport checks its own accounting against this, so an off-by-one
    here becomes a fetch that hangs or one that completes early.
    """
    layout = uniform_layout(num_layers=2, kv_planes=2, plane_bytes=10_000)
    placements = place([0, 1])
    plan = FetchPlanner(layout).plan(
        request_for(placements, max_record_bytes=4096),
        DIGESTS,
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
        request_for(place([0]), max_record_bytes=4096), DIGESTS
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
        request_for(place([0]), max_record_bytes=4096), DIGESTS
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
        DIGESTS,
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
            DIGESTS,
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
    plan = planner.plan(request_for(place(windowed), max_record_bytes=4096), DIGESTS)
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
