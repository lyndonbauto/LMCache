# SPDX-License-Identifier: Apache-2.0
"""Flatten a :class:`LayerFetchPlan` into native pipelined-fetch calls.

The native session takes lists of plain tuples rather than pybind classes, so
Python can build them without importing RDMA-only bindings and a build without
RDMA can still be tested against the same shapes.

:func:`pipelined_fetch_arguments` is the plan as given, one entry per slot.
The native session takes it without re-planning, so the plan is the only
source of truth for what is fetched, and each slot goes to the node the plan
names for it.
"""

# Standard
from dataclasses import dataclass

# Local
from .contract import LayerFetchPlan


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
