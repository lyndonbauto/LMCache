# SPDX-License-Identifier: Apache-2.0
"""A layerwise worker refuses a per-layer wait the server could outlast.

The server reports its layer publish budget when the worker registers: the
longest it may take to publish a retrieve's next layer, or its failure. A
worker that gives up sooner stops the engine while the server may still copy
into its blocks, so registration fails instead. These tests drive the real
``LMCacheDrivenTransferContext.register`` over real shared memory, with the
server's reply faked.
"""

# Standard
from collections.abc import Iterator
from multiprocessing import shared_memory
from types import SimpleNamespace
from unittest.mock import MagicMock
import os
import random

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.layerwise.deferral import PipelinedFetchConfig, SharedKeyPolicy
from lmcache.v1.multiprocess.custom_types import RegisterKvCacheResponse
from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.layer_progress import layer_progress_shm_name
from lmcache.v1.multiprocess.transfer_context import worker_transfer

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX shared memory semantics"
)

_LAYERS = [0, 1]
_MARGIN = worker_transfer.LAYERWISE_WAIT_MARGIN_SECONDS


class _FakeEventBackend:
    """Event backend with opaque events and no device work."""

    device_type = "fake"

    def check_event_support(self, device: object) -> None:
        return None

    def create_event(self, device: object) -> object:
        return object()

    def export_event(self, event: object, device: object) -> bytes:
        return b"completion-handle"

    def import_event(self, handle: bytes, device: object) -> object:
        return ("remote", handle)

    def record_event(self, event: object, stream: object) -> None:
        return None

    def wait_event(self, event: object, stream: object) -> None:
        return None

    def query_event(self, event: object) -> bool:
        return True

    def synchronize_event(self, event: object, device: object) -> None:
        return None


def _segment_exists(instance_id: int) -> bool:
    try:
        shared_memory.SharedMemory(name=layer_progress_shm_name(instance_id)).close()
    except FileNotFoundError:
        return False
    return True


@pytest.fixture
def instance_id() -> Iterator[int]:
    """A worker instance id whose segment name no other test uses."""
    iid = random.randint(10**8, 10**9)
    yield iid
    try:
        leftover = shared_memory.SharedMemory(name=layer_progress_shm_name(iid))
    except FileNotFoundError:
        return
    leftover.close()
    leftover.unlink()


@pytest.fixture(autouse=True)
def _no_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_transfer, "get_event_ipc_backend", lambda device: _FakeEventBackend()
    )
    monkeypatch.setattr(
        worker_transfer, "wrap_kv_caches", lambda kv_caches: list(kv_caches.values())
    )


def _register(
    instance_id: int,
    *,
    budget_seconds: float,
    wait_seconds: float,
    use_layerwise: bool = True,
) -> worker_transfer.LMCacheDrivenTransferContext:
    """Register a worker whose server reports ``budget_seconds``."""
    future: MessagingFuture[object] = MessagingFuture()
    future.set_result(
        RegisterKvCacheResponse(
            server_use_layerwise=use_layerwise,
            layer_event_ipc_handles=[b"handle"] * len(_LAYERS) if use_layerwise else [],
            layer_publish_budget_seconds=budget_seconds,
        )
    )
    client = MagicMock()
    client.register_kv_cache.return_value = future
    context = worker_transfer.LMCacheDrivenTransferContext(instance_id, client)
    context.register(
        {"layer_0": torch.empty(1)},
        "model",
        1,
        1,
        1.0,
        engine_group_infos=[SimpleNamespace(layer_indices=_LAYERS)],
        use_layerwise=use_layerwise,
        layerwise_wait_timeout_seconds=wait_seconds,
    )
    return context


def test_a_wait_shorter_than_the_budget_plus_margin_is_refused(
    instance_id: int,
) -> None:
    with pytest.raises(ValueError, match="layerwise_wait_timeout_seconds"):
        _register(instance_id, budget_seconds=3.0, wait_seconds=3.0 + _MARGIN - 0.01)

    assert not _segment_exists(instance_id), "a refused registration left its segment"


def test_a_wait_of_exactly_the_budget_plus_margin_registers(instance_id: int) -> None:
    context = _register(instance_id, budget_seconds=3.0, wait_seconds=3.0 + _MARGIN)

    assert _segment_exists(instance_id)
    context.close()


def test_a_server_that_never_waits_accepts_any_wait(instance_id: int) -> None:
    """A budget of 0 is a server without the pipelined fetch, or one that
    predates the field; the worker has nothing to check."""
    context = _register(instance_id, budget_seconds=0.0, wait_seconds=0.01)

    context.close()


def test_a_worker_without_layerwise_ignores_the_budget(instance_id: int) -> None:
    """Such a worker never waits per layer, so no budget can outlast it."""
    context = _register(
        instance_id, budget_seconds=100.0, wait_seconds=0.01, use_layerwise=False
    )

    assert not _segment_exists(instance_id)
    context.close()


def test_the_defaults_fit_the_default_worker_wait() -> None:
    """With every default, the worst policy still registers against vLLM's
    default ``lmcache.mp.layerwise_wait_timeout_seconds``."""
    adapter = pytest.importorskip("lmcache.integration.vllm.vllm_multi_process_adapter")
    worker_wait = adapter.ExtraConfigDefault.layerwise_wait_timeout_seconds.default
    worst = PipelinedFetchConfig(enabled=True, shared_keys=SharedKeyPolicy.WAIT)

    assert worst.layer_publish_budget_seconds + _MARGIN <= worker_wait


def test_the_budget_is_every_wait_before_a_layer_is_published() -> None:
    config = PipelinedFetchConfig(
        enabled=True,
        shared_keys=SharedKeyPolicy.WAIT,
        shared_wait_seconds=0.25,
        layer_timeout_seconds=1.0,
        whole_load_timeout_seconds=2.0,
    )

    assert config.layer_publish_budget_seconds == 3.25


def test_the_shared_key_wait_counts_only_when_the_policy_waits() -> None:
    config = PipelinedFetchConfig(
        enabled=True,
        shared_keys=SharedKeyPolicy.RECOMPUTE,
        shared_wait_seconds=0.25,
        layer_timeout_seconds=1.0,
        whole_load_timeout_seconds=2.0,
    )

    assert config.layer_publish_budget_seconds == 3.0


def test_a_disabled_pipelined_fetch_has_no_budget() -> None:
    assert PipelinedFetchConfig(enabled=False).layer_publish_budget_seconds == 0.0


@pytest.mark.parametrize(
    "field", ["layer_timeout_seconds", "whole_load_timeout_seconds"]
)
@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_each_timeout_must_be_positive(field: str, seconds: float) -> None:
    with pytest.raises(ValueError, match=field):
        PipelinedFetchConfig(**{field: seconds})
