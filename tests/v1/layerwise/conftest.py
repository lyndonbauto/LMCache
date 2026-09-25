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


class RecordingLauncher:
    """A :class:`LayerLauncher` that records calls instead of touching a GPU.

    ``calls`` holds ``"begin"``, ``("launch", layer_id)`` and ``"failed"`` in
    the order they happened, so tests can assert exactly what the sink asked
    the GPU side to do.
    """

    def __init__(self) -> None:
        """Build a launcher with no calls recorded."""
        self.calls: list[object] = []

    def begin(self) -> None:
        """Record that setup ran."""
        self.calls.append("begin")

    def launch_layer(self, layer_id: int) -> None:
        """Record one layer launch.

        Args:
            layer_id: Global layer index.
        """
        self.calls.append(("launch", layer_id))

    def mark_failed(self) -> None:
        """Record that the retrieve was marked failed."""
        self.calls.append("failed")

    def launched_layers(self) -> list[int]:
        """Return the launched layer ids, in launch order.

        Returns:
            Layer ids from every recorded launch.
        """
        return [
            call[1]
            for call in self.calls
            if isinstance(call, tuple) and call[0] == "launch"
        ]
