# SPDX-License-Identifier: Apache-2.0
"""A packing :class:`ChunkPlacer` for tests, standing in for Track A's."""

# Standard
from collections.abc import Callable, Sequence

# First Party
from lmcache.v1.layerwise import LayerwiseContractError, PlanTooLargeError
from lmcache.v1.layerwise.request_fetch import (
    ChunkLocation,
    LeaseOutcome,
    ObjectToPlace,
)


def alternate_nodes(chunk_id: int, _object_group_id: int) -> str:
    """Chunks alternate between two nodes."""
    return f"node-{chunk_id % 2}"


class PackingLease:
    """A lease whose objects were packed back to back; records its release."""

    def __init__(
        self,
        window_bytes: int,
        locations: dict[tuple[int, int], ChunkLocation],
        window_start: int = 0,
    ) -> None:
        self._window_start = window_start
        self._window_bytes = window_bytes
        self.locations = locations
        self.outcomes: list[LeaseOutcome] = []

    def window_start(self) -> int:
        return self._window_start

    def window_bytes(self) -> int:
        return self._window_bytes

    def locate(self, chunk_id: int, object_group_id: int) -> ChunkLocation:
        return self.locations[(chunk_id, object_group_id)]

    def release(self, outcome: LeaseOutcome) -> None:
        self.outcomes.append(outcome)


class PackingPlacer:
    """Packs a request's objects back to back, ``first_offset`` into the window.

    The window starts ``window_start`` bytes into the registration, and the
    locations it hands out are registration offsets, as the real placer's
    are. Refuses like the real placer: ``PlanTooLargeError`` when the objects
    do not fit the window, and a plain contract error while ``busy`` is set.
    """

    def __init__(
        self,
        window_bytes: int = 1 << 30,
        first_offset: int = 4096,
        node_of: Callable[[int, int], str] = alternate_nodes,
        window_start: int = 0,
    ) -> None:
        self.window_start = window_start
        self.window_bytes = window_bytes
        self.first_offset = first_offset
        self.node_of = node_of
        self.busy = False
        self.requests: list[tuple[ObjectToPlace, ...]] = []
        self.leases: list[PackingLease] = []

    def lease(self, objects: Sequence[ObjectToPlace]) -> PackingLease:
        self.requests.append(tuple(objects))
        if self.busy:
            raise LayerwiseContractError("no window is free")
        needed = self.first_offset + sum(o.object_bytes for o in objects)
        if needed > self.window_bytes:
            raise PlanTooLargeError(
                f"request needs {needed} bytes; the window has {self.window_bytes}"
            )
        locations: dict[tuple[int, int], ChunkLocation] = {}
        offset = self.window_start + self.first_offset
        for obj in objects:
            key = (obj.chunk_id, obj.object_group_id)
            locations[key] = ChunkLocation(self.node_of(*key), offset)
            offset += obj.object_bytes
        lease = PackingLease(self.window_bytes, locations, self.window_start)
        self.leases.append(lease)
        return lease
