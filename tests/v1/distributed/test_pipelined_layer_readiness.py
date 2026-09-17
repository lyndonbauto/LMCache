# SPDX-License-Identifier: Apache-2.0
"""Tests for pipelined layer readiness threading through the L2 stack."""

# Standard
from typing import cast

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
)
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.platform import create_event_notifier


class _PipelinedReadyClient:
    """Minimal native client stub with pipelined readiness."""

    def __init__(self, ready_layers: set[int]) -> None:
        self._ready_layers = ready_layers
        self._efd = create_event_notifier()

    def event_fd(self) -> int:
        return self._efd.fileno()

    def is_pipelined_layer_ready(self, layer_id: int) -> bool:
        return layer_id in self._ready_layers


class _PlainClient:
    """Native client without pipelined readiness."""

    def __init__(self) -> None:
        self._efd = create_event_notifier()

    def event_fd(self) -> int:
        return self._efd.fileno()


def test_default_l2_adapter_reports_not_ready() -> None:
    """Backends without pipelined fetch must not claim layers are ready."""

    class _Dummy:
        is_pipelined_layer_ready = L2AdapterInterface.is_pipelined_layer_ready

    dummy = cast(L2AdapterInterface, _Dummy())
    assert dummy.is_pipelined_layer_ready(0) is False


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


def test_storage_manager_or_across_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any adapter reporting ready satisfies the storage manager query."""
    manager = StorageManager.__new__(StorageManager)
    manager._adapters_lock = __import__("threading").Lock()
    adapters: dict[int, L2AdapterInterface] = {
        0: NativeConnectorL2Adapter(
            native_client=_PlainClient(),
            type_name="a",
        ),
        1: NativeConnectorL2Adapter(
            native_client=_PipelinedReadyClient({7}),
            type_name="b",
        ),
    }
    manager._l2_adapters = adapters
    assert manager.is_pipelined_layer_ready(7) is True
    assert manager.is_pipelined_layer_ready(0) is False
