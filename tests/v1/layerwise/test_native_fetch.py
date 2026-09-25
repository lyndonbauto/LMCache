# SPDX-License-Identifier: Apache-2.0
"""Flattening a fetch plan into the native pipelined-fetch arguments."""

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    ChunkPlacement,
    FetchPlanner,
    KernelGroupGeometry,
    LayerFetchPlan,
    ModelLayout,
    PlanRequest,
    RecordKeys,
    chunk_fetch_arguments,
    pipelined_fetch_arguments,
)


def _two_chunk_fetch() -> tuple[PlanRequest, RecordKeys, FetchPlanner]:
    layout = ModelLayout(
        {0: [KernelGroupGeometry((0, 1), kv_planes=2, plane_bytes=4096)]}
    )
    request = PlanRequest(
        placements=(
            ChunkPlacement(chunk_id=0, object_group_id=0, node_index=1, dest_offset=0),
            ChunkPlacement(
                chunk_id=1, object_group_id=0, node_index=0, dest_offset=1 << 20
            ),
        ),
        node_names=("node-a", "node-b"),
        max_record_bytes=4096,
    )
    keys = RecordKeys(layout, 4096, {(0, 0): "key-a", (1, 0): "key-b"})
    return request, keys, FetchPlanner(layout)


def _one_chunk_on_two_nodes() -> tuple[PlanRequest, LayerFetchPlan]:
    """One chunk whose two object groups live on different nodes."""
    layout = ModelLayout(
        {
            0: [KernelGroupGeometry((0,), kv_planes=1, plane_bytes=64)],
            1: [KernelGroupGeometry((1,), kv_planes=1, plane_bytes=64)],
        }
    )
    request = PlanRequest(
        placements=(
            ChunkPlacement(0, 0, 0, 0),
            ChunkPlacement(0, 1, 1, 4096),
        ),
        node_names=("node-a", "node-b"),
        max_record_bytes=4096,
    )
    plan = FetchPlanner(layout).plan(
        request, RecordKeys(layout, 4096, {(0, 0): "g0", (0, 1): "g1"})
    )
    return request, plan


def test_every_slot_is_passed_as_given_in_plan_order() -> None:
    """Position is the slot number on the wire, so order and content are kept."""
    request, keys, planner = _two_chunk_fetch()
    plan = planner.plan(request, keys)

    arguments = pipelined_fetch_arguments(plan)

    assert arguments.node_names == ["node-a", "node-b"]
    assert arguments.slots == [
        (s.node_index, s.record_key, s.offset, s.length, s.layer_id) for s in plan.slots
    ]
    assert arguments.slots[0] == (1, "key-a|s|0", 0, 4096, 0)


def test_each_slot_resolves_to_the_node_the_plan_gave_it() -> None:
    """The native side looks nodes up by index, so the index must match the name."""
    request, keys, planner = _two_chunk_fetch()
    plan = planner.plan(request, keys)

    arguments = pipelined_fetch_arguments(plan)

    for slot, (node_index, *_rest) in zip(plan.slots, arguments.slots, strict=True):
        assert arguments.node_names[node_index] == plan.node_name_for(slot)


def test_one_chunk_on_two_nodes_is_expressible_per_slot() -> None:
    """Records of one chunk can sit on different nodes; each slot keeps its own."""
    _request, plan = _one_chunk_on_two_nodes()

    arguments = pipelined_fetch_arguments(plan)

    assert {arguments.node_names[s[0]] for s in arguments.slots} == {
        "node-a",
        "node-b",
    }


def test_chunk_call_keeps_slots_in_plan_order() -> None:
    """The chunk-level session numbers slots by position, as the plan does."""
    request, keys, planner = _two_chunk_fetch()
    plan = planner.plan(request, keys)

    arguments = chunk_fetch_arguments(plan, request.placements)

    assert arguments.slot_record_keys == [
        (s.chunk_id, s.layer_id, s.plane, s.piece, s.record_key) for s in plan.slots
    ]
    assert arguments.slot_record_keys[0] == (0, 0, 0, 0, "key-a|s|0")


def test_chunk_call_resolves_node_names_one_binding_per_chunk() -> None:
    """Node indices become names, one binding per chunk."""
    request, keys, planner = _two_chunk_fetch()
    plan = planner.plan(request, keys)

    arguments = chunk_fetch_arguments(plan, request.placements)

    assert arguments.placements == [(0, 0, 0), (1, 0, 1 << 20)]
    assert arguments.chunk_nodes == [(0, "node-b"), (1, "node-a")]


def test_chunk_call_cannot_express_one_chunk_on_two_nodes() -> None:
    """The chunk-level session binds a chunk to one node, so this is refused."""
    request, plan = _one_chunk_on_two_nodes()

    with pytest.raises(ValueError, match="binds a chunk to one node"):
        chunk_fetch_arguments(plan, request.placements)
