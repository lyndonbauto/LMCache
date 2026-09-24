# SPDX-License-Identifier: Apache-2.0
"""Flatten a :class:`LayerFetchPlan` into the native pipelined-fetch call.

The native session takes three lists of plain tuples rather than pybind
classes, so Python can build them without importing RDMA-only bindings and a
build without RDMA can still be tested against the same shapes.
"""

# Standard
from collections.abc import Sequence
from dataclasses import dataclass

# Local
from .contract import LayerFetchPlan
from .planner import ChunkPlacement


@dataclass(frozen=True)
class PipelinedFetchArguments:
    """The three inputs of ``issue_pipelined_fetch_by_keys``.

    Attributes:
        placements: One ``(chunk_id, object_group_id, dest_offset)`` per
            placed object, in the order given.
        chunk_nodes: One ``(chunk_id, node_name)`` per chunk, ascending by
            chunk id.
        slot_record_keys: One ``(chunk_id, layer_id, plane, piece,
            record_key)`` per slot, in plan order, so the native side's slot
            numbering matches the plan's.
    """

    placements: list[tuple[int, int, int]]
    chunk_nodes: list[tuple[int, str]]
    slot_record_keys: list[tuple[int, int, int, int, str]]


def pipelined_fetch_arguments(
    plan: LayerFetchPlan, placements: Sequence[ChunkPlacement]
) -> PipelinedFetchArguments:
    """Flatten a plan and the placements it was built from.

    Args:
        plan: The slots the fetch expects, in the order they are numbered.
        placements: The placements the plan was built from.

    Returns:
        The native call's arguments.

    Raises:
        ValueError: If one chunk's objects are placed on different nodes. The
            native session binds a whole chunk to one node, so such a request
            cannot be expressed to it.
    """
    chunk_nodes: dict[int, str] = {}
    for placement in placements:
        node = plan.node_names[placement.node_index]
        bound = chunk_nodes.setdefault(placement.chunk_id, node)
        if bound != node:
            raise ValueError(
                f"chunk {placement.chunk_id} is placed on both {bound!r} and "
                f"{node!r}, but the native session binds a chunk to one node"
            )
    return PipelinedFetchArguments(
        placements=[(p.chunk_id, p.object_group_id, p.dest_offset) for p in placements],
        chunk_nodes=sorted(chunk_nodes.items()),
        slot_record_keys=[
            (s.chunk_id, s.layer_id, s.plane, s.piece, s.record_key) for s in plan.slots
        ],
    )
