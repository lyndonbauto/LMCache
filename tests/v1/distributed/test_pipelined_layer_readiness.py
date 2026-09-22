# SPDX-License-Identifier: Apache-2.0
"""Tests for pipelined layer readiness threading through the L2 stack."""

# First Party
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface
from lmcache.v1.distributed.l2_adapters.mock_l2_adapter import (
    MockL2Adapter,
    MockL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
)
from lmcache.v1.platform import create_event_notifier


class _PipelinedReadyClient:
    """Minimal native client stub with pipelined readiness."""

    def __init__(self, ready_layers: set[int]) -> None:
        self._ready_layers = ready_layers
        self._efd = create_event_notifier()

    def event_fd(self) -> int:
        return self._efd.fileno()

    def is_pipelined_layer_ready(
        self, layer_id: int, request_generation: int = 0
    ) -> bool:
        del request_generation
        return layer_id in self._ready_layers


class _PlainClient:
    """Native client without pipelined readiness."""

    def __init__(self) -> None:
        self._efd = create_event_notifier()

    def event_fd(self) -> int:
        return self._efd.fileno()


def test_default_l2_adapter_reports_not_ready() -> None:
    """Backends without pipelined fetch must not claim layers are ready."""
    adapter = MockL2Adapter(MockL2AdapterConfig(max_size_gb=1.0, mock_bandwidth_gb=1.0))
    assert adapter.is_pipelined_layer_ready(0) is False


def test_native_adapter_forwards_when_client_supports_it() -> None:
    """The Aerospike path delegates to the native binding when present."""
    adapter = NativeConnectorL2Adapter(
        native_client=_PipelinedReadyClient({2, 5}),
        type_name="test",
    )
    assert adapter.is_pipelined_layer_ready(2) is True
    assert adapter.is_pipelined_layer_ready(1) is False


def test_native_adapter_without_binding_returns_false() -> None:
    """Connectors compiled without RDMA do not expose the method."""
    adapter = NativeConnectorL2Adapter(
        native_client=_PlainClient(),
        type_name="test",
    )
    assert adapter.is_pipelined_layer_ready(0) is False


def test_native_adapter_forwards_pipelined_fetch_lifecycle() -> None:
    """Begin, finish, and init-error surface delegate to the native client."""

    class _PipelinedClient:
        def __init__(self) -> None:
            self._efd = create_event_notifier()
            self.layouts: dict | None = None
            self.finished = False
            self.abandoned = False
            self.generation = 7

        def event_fd(self) -> int:
            return self._efd.fileno()

        def pipelined_fetch_init_error(self) -> str:
            return "verbs init failed"

        def set_object_group_layouts(self, layouts: dict) -> None:
            self.layouts = layouts

        def issue_pipelined_fetch(
            self,
            placements: list[object],
            chunk_nodes: list[object],
            digests: list[object],
        ) -> int:
            del placements, chunk_nodes, digests
            return self.generation

        def finish_pipelined_fetch(self) -> None:
            self.finished = True

        def abandon_pipelined_fetch(self) -> None:
            self.abandoned = True

    client = _PipelinedClient()
    adapter = NativeConnectorL2Adapter(native_client=client, type_name="test")
    assert adapter.pipelined_fetch_init_error() == "verbs init failed"
    assert adapter.begin_pipelined_fetch([], [], []) == 7
    adapter.finish_pipelined_fetch()
    assert client.finished is True
    adapter.abandon_pipelined_fetch()
    assert client.abandoned is True


def test_readiness_is_scoped_to_request_generation() -> None:
    """The base contract accepts a generation handle for pipelined queries."""

    class _Scoped(L2AdapterInterface):
        def is_pipelined_layer_ready(
            self, layer_id: int, request_generation: int = 0
        ) -> bool:
            return request_generation == 3 and layer_id == 7

        def get_store_event_fd(self) -> int:
            return 0

        def get_lookup_and_lock_event_fd(self) -> int:
            return 0

        def get_load_event_fd(self) -> int:
            return 0

        def submit_store_task(self, keys, objects):
            raise NotImplementedError

        def pop_completed_store_tasks(self):
            raise NotImplementedError

        def submit_lookup_and_lock_task(self, keys):
            raise NotImplementedError

        def query_lookup_and_lock_result(self, task_id):
            raise NotImplementedError

        def submit_load_task(self, keys, memory_objs):
            raise NotImplementedError

        def query_load_result(self, task_id):
            raise NotImplementedError

        def submit_unlock(self, keys):
            raise NotImplementedError

        def close(self) -> None:
            return None

    adapter = _Scoped()
    assert adapter.is_pipelined_layer_ready(7, request_generation=3) is True
    assert adapter.is_pipelined_layer_ready(7, request_generation=2) is False
