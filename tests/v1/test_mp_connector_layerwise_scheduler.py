# SPDX-License-Identifier: Apache-2.0
"""Scheduler-side layerwise behavior for LMCacheMPConnector."""

# Standard
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

# Third Party
import pytest

if TYPE_CHECKING:
    # First Party
    from lmcache.integration.vllm.lmcache_mp_connector import LMCacheMPConnector


def test_scheduler_reports_synchronous_load_when_layerwise_enabled() -> None:
    """Layerwise mode must not park requests in WAITING_FOR_REMOTE_KVS."""
    pytest.importorskip("vllm")

    # Third Party
    from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorRole
    from vllm.v1.request import RequestStatus

    # First Party
    from lmcache.integration.vllm.lmcache_mp_connector import LMCacheMPConnector
    from lmcache.integration.vllm.lmcache_mp_metadata import LMCacheMPRequestTracker

    class _Request:
        request_id = "req-layerwise"
        status = RequestStatus.WAITING
        num_computed_tokens = 0
        num_preemptions = 0
        cache_salt = ""
        prompt_token_ids = list(range(32))
        all_token_ids = list(range(32))
        mm_features: list[object] = []

    request = _Request()
    tracker = LMCacheMPRequestTracker(request)  # type: ignore[arg-type]

    connector = LMCacheMPConnector.__new__(LMCacheMPConnector)
    connector.use_layerwise = True
    connector._role = KVConnectorRole.SCHEDULER
    connector._hit_alignment_tokens = 1
    connector._connector_stats = MagicMock()
    connector.request_trackers = {request.request_id: tracker}
    connector.scheduler_adapter = MagicMock()
    connector.scheduler_adapter.lmcache_tokens_per_chunk = 16
    connector.scheduler_adapter.check_lookup_result.return_value = 32

    need_to_load, load_async = connector.get_num_new_matched_tokens(
        request,  # type: ignore[arg-type]
        num_computed_tokens=0,
    )

    assert need_to_load is not None and need_to_load > 0
    assert load_async is False


def _worker_connector(
    num_kv_cache_groups: int, wait_error: Exception
) -> tuple["LMCacheMPConnector", MagicMock]:
    """Build a layerwise worker connector whose layer waits raise *wait_error*.

    Returns the connector and its mocked worker adapter.
    """
    # First Party
    from lmcache.integration.vllm.lmcache_mp_connector import LMCacheMPConnector

    adapter = MagicMock(name="worker_adapter")
    adapter.wait_for_layer_load.side_effect = wait_error
    adapter.report_failed_layer_load.return_value = {7, 8}
    connector = LMCacheMPConnector.__new__(LMCacheMPConnector)
    connector.use_layerwise = True
    connector._layer_name_to_index = {"layers.0": 0}
    connector._num_vllm_kv_cache_groups = num_kv_cache_groups
    connector.worker_adapter = adapter
    return connector, adapter


@pytest.mark.parametrize(
    "error_name",
    [
        "LayerProgressRetrieveFailedError",
        "LayerProgressRetrieveGenerationTimeoutError",
        "LayerProgressRetrieveProgressTimeoutError",
        "LayerProgressStaleGenerationError",
    ],
)
def test_failed_layer_load_is_reported_for_recompute(error_name: str) -> None:
    """A load failure must reach vLLM as load errors, not crash or pass silently."""
    pytest.importorskip("vllm")

    # First Party
    from lmcache.v1.multiprocess import layer_progress

    connector, adapter = _worker_connector(
        1, getattr(layer_progress, error_name)("boom")
    )

    connector.wait_for_layer_load("layers.0")

    adapter.report_failed_layer_load.assert_called_once_with()


def test_failed_layer_load_raises_for_multiple_kv_cache_groups() -> None:
    """vLLM rejects block-level reports for hybrid models, so fail loudly."""
    pytest.importorskip("vllm")

    # First Party
    from lmcache.v1.multiprocess.layer_progress import (
        LayerProgressRetrieveFailedError,
    )

    connector, adapter = _worker_connector(2, LayerProgressRetrieveFailedError("boom"))

    with pytest.raises(RuntimeError, match="more than one KV cache group"):
        connector.wait_for_layer_load("layers.0")
    adapter.report_failed_layer_load.assert_not_called()


def test_non_load_layer_progress_error_propagates() -> None:
    """Configuration errors are not load failures and keep raising."""
    pytest.importorskip("vllm")

    # First Party
    from lmcache.v1.multiprocess.layer_progress import (
        LayerProgressIncompatibleWithCudaGraphError,
    )

    connector, adapter = _worker_connector(
        1, LayerProgressIncompatibleWithCudaGraphError("capture")
    )

    with pytest.raises(LayerProgressIncompatibleWithCudaGraphError):
        connector.wait_for_layer_load("layers.0")
    adapter.report_failed_layer_load.assert_not_called()


def test_mp_connector_requires_piecewise_when_layerwise_enabled() -> None:
    pytest.importorskip("vllm")

    # First Party
    from lmcache.integration.vllm.lmcache_mp_connector import LMCacheMPConnector

    assert not LMCacheMPConnector.requires_piecewise_for_cudagraph({})
    assert not LMCacheMPConnector.requires_piecewise_for_cudagraph(
        {"lmcache.mp.use_layerwise": False}
    )
    assert LMCacheMPConnector.requires_piecewise_for_cudagraph(
        {"lmcache.mp.use_layerwise": True}
    )
