# SPDX-License-Identifier: Apache-2.0
"""Tests for reaching a layer arrival source through the storage manager."""

# Standard
from collections.abc import Iterator
from contextlib import contextmanager

# Third Party
import pytest

# First Party
from lmcache.v1.distributed import storage_manager as storage_manager_module
from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.distributed.l2_adapters.config import (
    L2AdapterConfigBase,
    L2AdaptersConfig,
)
from lmcache.v1.distributed.l2_adapters.layerwise_source import (
    AerospikeLayerArrivalSource,
)
from lmcache.v1.distributed.l2_adapters.mock_l2_adapter import (
    MockL2Adapter,
    MockL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
)
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.layerwise import (
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerwiseContractError,
    SlotPlacement,
)
from lmcache.v1.platform import create_event_notifier
from tests.v1.distributed.utils import PipelinedNativeClientStub


class _PlainClient:
    """A native client built without the pipelined path, like Redis."""

    def __init__(self) -> None:
        self._efd = create_event_notifier()

    def event_fd(self) -> int:
        return self._efd.fileno()

    def drain_completions(self) -> list[tuple[int, bool, str, list[bool] | None]]:
        return []

    def close(self) -> None:
        self._efd.close()


class _KeysOnlyClient(_PlainClient):
    """Has readiness but only the retired by-keys issue entry point."""

    def pipelined_fetch_ready(self) -> bool:
        return True

    def pipelined_fetch_init_error(self) -> str:
        return ""

    def is_pipelined_layer_ready(self, layer_id: int, request_generation: int) -> bool:
        return False

    def pipelined_unservable_layers(self) -> list[int]:
        return []

    def finish_pipelined_fetch(self) -> None:
        return None

    def abandon_pipelined_fetch(self) -> None:
        return None

    def issue_pipelined_fetch_by_keys(
        self,
        placements: list[tuple[int, int, int]],
        chunk_nodes: list[tuple[int, str]],
        slot_record_keys: list[tuple[int, int, int, int, str]],
    ) -> int:
        return 1


@contextmanager
def _native_adapter(client: object) -> Iterator[NativeConnectorL2Adapter]:
    adapter = NativeConnectorL2Adapter(native_client=client, type_name="test")
    try:
        yield adapter
    finally:
        adapter.close()


@contextmanager
def _storage_manager(
    adapters: list[L2AdapterConfigBase],
) -> Iterator[StorageManager]:
    sm = StorageManager(
        StorageManagerConfig(
            l1_manager_config=L1ManagerConfig(
                memory_config=L1MemoryManagerConfig(
                    size_in_bytes=16 << 20,
                    use_lazy=False,
                    init_size_in_bytes=16 << 20,
                    align_bytes=0x1000,
                ),
                write_ttl_seconds=600,
                read_ttl_seconds=300,
            ),
            eviction_config=EvictionConfig(eviction_policy="LRU"),
            l2_adapter_config=L2AdaptersConfig(adapters=adapters),
        )
    )
    try:
        yield sm
    finally:
        sm.close()


def _mock_adapter_config() -> MockL2AdapterConfig:
    return MockL2AdapterConfig(max_size_gb=0.01, mock_bandwidth_gb=10.0)


def _two_layer_plan() -> LayerFetchPlan:
    return LayerFetchPlan(
        slots=(
            SlotPlacement(0, 3, 1, "obj|s|0", 0, 0, 4096, 64),
            SlotPlacement(1, 3, 0, "obj|s|1", 0, 0, 4160, 64),
        ),
        node_names=("node-a", "node-b"),
    )


def test_default_adapter_has_no_source() -> None:
    """A backend without a pipelined path refuses, so retrieve falls back."""
    adapter = MockL2Adapter(MockL2AdapterConfig(max_size_gb=1.0, mock_bandwidth_gb=1.0))
    try:
        with pytest.raises(LayerwiseContractError, match="MockL2Adapter"):
            adapter.layer_arrival_source()
    finally:
        adapter.close()


def test_native_adapter_without_binding_has_no_source() -> None:
    """A native client built without RDMA has no source."""
    with _native_adapter(_PlainClient()) as adapter:
        with pytest.raises(LayerwiseContractError, match="no pipelined fetch path"):
            adapter.layer_arrival_source()


def test_native_adapter_without_slot_issue_has_no_source() -> None:
    """The source issues slot for slot; a client that cannot is refused."""
    with _native_adapter(_KeysOnlyClient()) as adapter:
        with pytest.raises(LayerwiseContractError):
            adapter.layer_arrival_source()


def test_native_adapter_returns_one_source() -> None:
    """Every call returns the same source, which tracks the one fetch."""
    with _native_adapter(PipelinedNativeClientStub()) as adapter:
        source = adapter.layer_arrival_source()
        assert isinstance(source, AerospikeLayerArrivalSource)
        assert adapter.layer_arrival_source() is source


def test_source_drives_the_native_client() -> None:
    """Begin, poll and finish reach the adapter's native client."""
    client = PipelinedNativeClientStub(generation=7)
    with _native_adapter(client) as adapter:
        source = adapter.layer_arrival_source()
        generation = source.begin_fetch(_two_layer_plan())
        assert generation == 7
        assert client.issued == [
            (
                ["node-a", "node-b"],
                [(1, "obj|s|0", 4096, 64, 0), (0, "obj|s|1", 4160, 64, 1)],
            )
        ]
        client.ready_layers.add(0)
        assert source.poll_layer(0, generation) is LayerArrivalStatus.RESIDENT
        assert source.poll_layer(1, generation) is LayerArrivalStatus.PENDING
        source.finish_fetch(generation)
        assert client.finished == 1


def test_source_refuses_a_second_concurrent_fetch() -> None:
    """Two retrieves share the adapter's source, so the second is refused."""
    with _native_adapter(PipelinedNativeClientStub()) as adapter:
        generation = adapter.layer_arrival_source().begin_fetch(_two_layer_plan())
        with pytest.raises(LayerwiseContractError, match="still active"):
            adapter.layer_arrival_source().begin_fetch(_two_layer_plan())
        adapter.layer_arrival_source().abandon_fetch(generation)


def test_storage_manager_without_adapters_refuses() -> None:
    """With no L2 adapter there is nothing to fetch layer by layer."""
    with _storage_manager([]) as sm:
        with pytest.raises(LayerwiseContractError, match="no L2 adapters"):
            sm.layer_arrival_source()


def test_storage_manager_refusal_names_each_adapter() -> None:
    """The refusal carries each adapter's reason."""
    with _storage_manager([_mock_adapter_config()]) as sm:
        with pytest.raises(LayerwiseContractError, match="MockL2Adapter"):
            sm.layer_arrival_source()


def test_storage_manager_returns_the_pipelined_adapters_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter with a pipelined path supplies one stable source."""
    client = PipelinedNativeClientStub()
    monkeypatch.setattr(
        storage_manager_module,
        "create_l2_adapter",
        lambda config, l1_memory_desc: NativeConnectorL2Adapter(
            native_client=client, type_name="stub"
        ),
    )
    with _storage_manager([_mock_adapter_config()]) as sm:
        source = sm.layer_arrival_source()
        assert isinstance(source, AerospikeLayerArrivalSource)
        assert sm.layer_arrival_source() is source
