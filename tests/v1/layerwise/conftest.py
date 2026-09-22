# SPDX-License-Identifier: Apache-2.0
"""Shared builders for layerwise contract tests."""

# First Party
from lmcache.v1.layerwise import LayerFetchPlan, SlotPlacement


def make_slot(
    layer_id: int,
    *,
    length: int = 64,
    chunk_id: int = 0,
) -> SlotPlacement:
    """Build one slot placement for tests."""
    return SlotPlacement(
        layer_id=layer_id,
        chunk_id=chunk_id,
        node_index=0,
        digest=b"digest",
        offset=0,
        length=length,
    )


def make_plan(layer_slot_counts: dict[int, int]) -> LayerFetchPlan:
    """Build a plan with the given number of slots per layer."""
    slots: list[SlotPlacement] = []
    for layer_id, count in layer_slot_counts.items():
        for chunk_id in range(count):
            slots.append(make_slot(layer_id, chunk_id=chunk_id))
    return LayerFetchPlan(tuple(slots))
