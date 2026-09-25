# SPDX-License-Identifier: Apache-2.0
"""Flatten a :class:`LayerFetchPlan` into native pipelined-fetch calls.

The native session takes lists of plain tuples rather than pybind classes, so
Python can build them without importing RDMA-only bindings and a build without
RDMA can still be tested against the same shapes.

Two shapes exist while the native side moves from chunk-level to slot-level
issue:

- :func:`pipelined_fetch_arguments` is the plan as given, one entry per slot.
  The native session takes it without re-planning, so the plan is the only
  source of truth for what is fetched, and each slot goes to the node that
  owns its own record.
- :func:`chunk_fetch_arguments` feeds ``issue_pipelined_fetch_by_keys``, which
  re-expands chunk placements natively and binds a whole chunk to one node.
  It goes away with that entry point.
"""

# Standard
from collections.abc import Sequence
from dataclasses import dataclass

# Local
from .contract import LayerFetchPlan
from .planner import ChunkPlacement


@dataclass(frozen=True)
class PipelinedFetchArguments:
    """The inputs of a slot-level native issue, in the plan's own terms.

    Attributes:
        node_names: The plan's nodes, indexed by each slot's node index.
        slots: One ``(node_index, record_key, dest_offset, length,
            layer_id)`` per slot, in plan order. A slot's position in this
            list is its slot number on the wire, so the native side must
            number slots by position and must not reorder them.
    """

    node_names: list[str]
    slots: list[tuple[int, str, int, int, int]]


@dataclass(frozen=True)
class ChunkFetchArguments:
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


def pipelined_fetch_arguments(plan: LayerFetchPlan) -> PipelinedFetchArguments:
    """Flatten a plan into one entry per slot.

    Everything the native side needs is on the plan, so no placements are
    needed: the destination offset and node of every slot were fixed when the
    plan was built.

    Args:
        plan: The slots the fetch expects, in the order they are numbered.

    Returns:
        The slot-level native call's arguments.
    """
    return PipelinedFetchArguments(
        node_names=list(plan.node_names),
        slots=[
            (s.node_index, s.record_key, s.offset, s.length, s.layer_id)
            for s in plan.slots
        ],
    )


def chunk_fetch_arguments(
    plan: LayerFetchPlan, placements: Sequence[ChunkPlacement]
) -> ChunkFetchArguments:
    """Flatten a plan and its placements for the chunk-level native call.

    Args:
        plan: The slots the fetch expects, in the order they are numbered.
        placements: The placements the plan was built from.

    Returns:
        The arguments of ``issue_pipelined_fetch_by_keys``.

    Raises:
        ValueError: If one chunk's objects are placed on different nodes. The
            chunk-level call binds a whole chunk to one node, so such a
            request cannot be expressed to it.
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
    return ChunkFetchArguments(
        placements=[(p.chunk_id, p.object_group_id, p.dest_offset) for p in placements],
        chunk_nodes=sorted(chunk_nodes.items()),
        slot_record_keys=[
            (s.chunk_id, s.layer_id, s.plane, s.piece, s.record_key) for s in plan.slots
        ],
    )
