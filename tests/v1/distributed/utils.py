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
    when built with ``LMCACHE_AEROSPIKE_RDMA``. Records what was issued and
    reports the layers in ``ready_layers`` as landed.
    """

    def __init__(self, generation: int = 7) -> None:
        self._efd = create_event_notifier()
        self.generation = generation
        self.ready_layers: set[int] = set()
        self.issued: list[tuple[list[str], list[tuple[int, str, int, int, int]]]] = []
        self.finished = 0
        self.abandoned = 0

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
        return self.generation

    def is_pipelined_layer_ready(self, layer_id: int, request_generation: int) -> bool:
        return request_generation == self.generation and layer_id in self.ready_layers

    def pipelined_unservable_layers(self) -> list[int]:
        return []

    def finish_pipelined_fetch(self) -> None:
        self.finished += 1

    def abandon_pipelined_fetch(self) -> None:
        self.abandoned += 1
