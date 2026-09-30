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
    wait_error: Exception,
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
    connector.worker_adapter = adapter
    return connector, adapter


@pytest.mark.parametrize("lazy_offload", [False, True])
@pytest.mark.parametrize("use_layerwise", [True, False])
def test_layerwise_loads_are_never_reported_as_finished_receiving(
    use_layerwise: bool, lazy_offload: bool
) -> None:
    """A synchronous load's request is running; vLLM asserts on a recv report."""
    pytest.importorskip("vllm")

    connector, adapter = _worker_connector(RuntimeError("unused"))
    connector.use_layerwise = use_layerwise
    connector.lazy_offload = lazy_offload
    adapter.get_finished.return_value = ({"stored"}, {"loaded"})
    adapter.get_finished_with_lazy_offload.return_value = (None, {"loaded"})

    finished_sending, finished_recving = connector.get_finished({"stored"})

    assert finished_sending == (None if lazy_offload else {"stored"})
    assert finished_recving == (None if use_layerwise else {"loaded"})


def test_failed_retrieve_is_reported_for_recompute() -> None:
    """A reported failure reaches vLLM as load errors, not an engine crash."""
    pytest.importorskip("vllm")

    # First Party
    from lmcache.v1.multiprocess.layer_progress import (
        LayerProgressRetrieveFailedError,
    )

    connector, adapter = _worker_connector(LayerProgressRetrieveFailedError("boom"))

    connector.wait_for_layer_load("layers.0")

    adapter.report_failed_layer_load.assert_called_once_with()


@pytest.mark.parametrize(
    "error_name",
    [
        "LayerProgressRetrieveGenerationTimeoutError",
        "LayerProgressRetrieveProgressTimeoutError",
        "LayerProgressStaleGenerationError",
        "LayerProgressIncompatibleWithCudaGraphError",
    ],
)
def test_other_layer_progress_errors_propagate(error_name: str) -> None:
    """The daemon may still write these blocks, so they are not handed back.

    A never-published generation is no longer ignored either.
    """
    pytest.importorskip("vllm")

    # First Party
    from lmcache.v1.multiprocess import layer_progress

    error_type = getattr(layer_progress, error_name)
    connector, adapter = _worker_connector(error_type("boom"))

    with pytest.raises(error_type):
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


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [
        (True, True),
        ("true", True),
        ("1", True),
        ("on", True),
        (False, False),
        ("false", False),
        ("0", False),
        ("off", False),
    ],
)
def test_connector_and_adapter_parse_use_layerwise_alike(
    raw: object, enabled: bool
) -> None:
    """A CLI passthrough delivers strings; ``"false"`` must mean off on the
    connector exactly as on the worker adapter, or the connector would run
    synchronous loads while the worker never waits for a layer."""
    pytest.importorskip("vllm")

    # First Party
    from lmcache.integration.vllm.lmcache_mp_connector import LMCacheMPConnector
    from lmcache.integration.vllm.vllm_multi_process_adapter import (
        is_layerwise_enabled,
    )

    extra_config = {"lmcache.mp.use_layerwise": raw}
    assert is_layerwise_enabled(extra_config) is enabled
    assert LMCacheMPConnector.requires_piecewise_for_cudagraph(extra_config) is enabled
