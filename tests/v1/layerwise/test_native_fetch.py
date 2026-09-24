# SPDX-License-Identifier: Apache-2.0
"""Flattening a fetch plan into the native pipelined-fetch arguments."""

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    ChunkPlacement,
    FetchPlanner,
    KernelGroupGeometry,
    ModelLayout,
    PlanRequest,
    RecordKeys,
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


def test_slots_keep_plan_order_so_native_slot_numbers_match() -> None:
    """The native session numbers slots by position, as the plan does."""
    request, keys, planner = _two_chunk_fetch()
    plan = planner.plan(request, keys)

    arguments = pipelined_fetch_arguments(plan, request.placements)

    assert arguments.slot_record_keys == [
        (s.chunk_id, s.layer_id, s.plane, s.piece, s.record_key) for s in plan.slots
    ]
    assert arguments.slot_record_keys[0] == (0, 0, 0, 0, "key-a|s|0")


def test_placements_and_chunk_nodes_resolve_node_names() -> None:
    """Node indices become names, one binding per chunk."""
    request, keys, planner = _two_chunk_fetch()
    plan = planner.plan(request, keys)

    arguments = pipelined_fetch_arguments(plan, request.placements)

    assert arguments.placements == [(0, 0, 0), (1, 0, 1 << 20)]
    assert arguments.chunk_nodes == [(0, "node-b"), (1, "node-a")]


def test_one_chunk_on_two_nodes_cannot_be_expressed() -> None:
    """The native session binds a chunk to one node, so this is refused."""
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

    with pytest.raises(ValueError, match="binds a chunk to one node"):
        pipelined_fetch_arguments(plan, request.placements)
