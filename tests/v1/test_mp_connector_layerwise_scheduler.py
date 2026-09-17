# SPDX-License-Identifier: Apache-2.0
"""Scheduler-side layerwise behavior for LMCacheMPConnector."""

# Standard
from unittest.mock import MagicMock

# Third Party
import pytest


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
    connector.role = KVConnectorRole.SCHEDULER  # type: ignore[misc]
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
