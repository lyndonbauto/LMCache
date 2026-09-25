# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for distributed tests."""

# Standard
from collections.abc import Sequence

# First Party
from lmcache.v1.platform import create_event_notifier, current_device_spec


def should_use_lazy_alloc() -> bool:
    """Return whether the current platform supports lazy L1 allocation."""
    return current_device_spec.is_pin_supported


class PipelinedNativeClientStub:
    """A native client with the pipelined RDMA binding, and nothing behind it.

    Has the methods ``NativeConnectorL2Adapter`` needs to run, plus the
    pipelined surface ``lmcache_aerospike.LMCacheAerospikeClient`` exposes
    when built with ``LMCACHE_AEROSPIKE_RDMA``. Like the native client it
    runs several fetches at once, each named by generation. Issues get
    generations counting up from ``generation``; ``ready_layers`` maps a
    generation to its landed layers.
    """

    def __init__(self, generation: int = 7) -> None:
        self._efd = create_event_notifier()
        self.next_generation = generation
        self.active: set[int] = set()
        self.ready_layers: dict[int, set[int]] = {}
        self.issued: list[tuple[list[str], list[tuple[int, str, int, int, int]]]] = []
        self.finished: list[int] = []
        self.abandoned: list[int] = []

    def event_fd(self) -> int:
        return self._efd.fileno()

    def drain_completions(self) -> list[tuple[int, bool, str, list[bool] | None]]:
        return []

    def close(self) -> None:
        self._efd.close()

    def pipelined_fetch_ready(self) -> bool:
        return True

    def pipelined_fetch_init_error(self) -> str:
        return ""

    def pipelined_max_slots_per_request(self) -> int:
        return 0

    def issue_pipelined_fetch_by_slots(
        self,
        node_names: Sequence[str],
        slots: Sequence[tuple[int, str, int, int, int]],
    ) -> int:
        self.issued.append((list(node_names), list(slots)))
        generation = self.next_generation
        self.next_generation += 1
        self.active.add(generation)
        return generation

    def is_pipelined_layer_ready(self, layer_id: int, request_generation: int) -> bool:
        return request_generation in self.active and layer_id in self.ready_layers.get(
            request_generation, set()
        )

    def pipelined_unservable_layers(self, generation: int) -> list[int]:
        return []

    def finish_pipelined_fetch(self, generation: int) -> None:
        if generation not in self.active:
            raise RuntimeError(f"generation {generation} is not an active fetch")
        self.active.discard(generation)
        self.finished.append(generation)

    def abandon_pipelined_fetch(self, generation: int) -> None:
        if generation in self.active:
            self.active.discard(generation)
            self.abandoned.append(generation)
