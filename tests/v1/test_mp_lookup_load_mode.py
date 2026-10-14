# SPDX-License-Identifier: Apache-2.0
"""How the vLLM adapters carry the server's per-request load mode."""

# Standard
from collections.abc import Callable, Iterator
from typing import Any

# Third Party
import pytest
import zmq

# First Party
from lmcache.integration.vllm import vllm_multi_process_adapter as adapter_module
from lmcache.integration.vllm.vllm_multi_process_adapter import (
    LMCacheMPSchedulerAdapter,
    ParallelStrategy,
)
from lmcache.v1.multiprocess.custom_types import LoadMode, PrefetchStatusWithMode
from lmcache.v1.multiprocess.futures import MessagingFuture

REQUEST = "r"


def _ready(value: Any) -> MessagingFuture[Any]:
    future: MessagingFuture[Any] = MessagingFuture()
    future.set_result(value)
    return future


class _Client:
    """One server: answers the lookup with ``chunks`` and ``mode``."""

    def __init__(self, chunks: int, mode: LoadMode) -> None:
        self.chunks = chunks
        self.mode = mode
        self.plain_queries = 0
        self.mode_queries = 0

    def get_chunk_size(self) -> MessagingFuture[Any]:
        return _ready(64)

    def lookup(self, key: Any, tp_size: int) -> MessagingFuture[Any]:
        return _ready(None)

    def query_prefetch_status(self, request_id: str) -> MessagingFuture[Any]:
        self.plain_queries += 1
        return _ready(self.chunks)

    def query_prefetch_status_with_mode(self, request_id: str) -> MessagingFuture[Any]:
        self.mode_queries += 1
        return _ready(PrefetchStatusWithMode(self.chunks, self.mode))

    def free_lookup_locks(self, key: Any, tp_size: int) -> MessagingFuture[Any]:
        return _ready(None)

    def close(self) -> None:
        pass


SchedulerFactory = Callable[[list[_Client], bool], LMCacheMPSchedulerAdapter]


@pytest.fixture
def make_scheduler(monkeypatch: pytest.MonkeyPatch) -> Iterator[SchedulerFactory]:
    adapters: list[LMCacheMPSchedulerAdapter] = []
    context = zmq.Context()

    def create(
        clients: list[_Client], use_layerwise: bool
    ) -> LMCacheMPSchedulerAdapter:
        urls = {f"tcp://test-{i}": client for i, client in enumerate(clients)}
        monkeypatch.setattr(
            adapter_module.RequestClientFactory,
            "create",
            lambda url, **kwargs: urls[url],
        )
        n = len(clients)
        adapter = LMCacheMPSchedulerAdapter(
            list(urls),
            context,
            "model",
            16,
            ParallelStrategy(False, n, 0, n, 1, n),
            extra_config={"lmcache.mp.use_layerwise": use_layerwise},
        )
        adapters.append(adapter)
        return adapter

    yield create
    for adapter in adapters:
        adapter.shutdown()
    context.term()


def _finish_lookup(adapter: LMCacheMPSchedulerAdapter) -> int:
    adapter.maybe_submit_lookup_request(REQUEST, list(range(256)))
    for _ in range(5):
        result = adapter.check_lookup_result(REQUEST)
        if result is not None:
            return result
    raise AssertionError("the lookup did not finish")


@pytest.mark.parametrize("mode", [LoadMode.ASYNC, LoadMode.LAYERWISE])
def test_a_layerwise_scheduler_takes_the_servers_mode(
    make_scheduler: SchedulerFactory, mode: LoadMode
) -> None:
    client = _Client(chunks=3, mode=mode)
    adapter = make_scheduler([client], True)

    assert _finish_lookup(adapter) == 3 * 64
    assert adapter.lookup_load_mode(REQUEST) is mode
    assert (client.mode_queries, client.plain_queries) == (1, 0)


def test_a_scheduler_without_layerwise_keeps_the_plain_query(
    make_scheduler: SchedulerFactory,
) -> None:
    client = _Client(chunks=3, mode=LoadMode.LAYERWISE)
    adapter = make_scheduler([client], False)

    assert _finish_lookup(adapter) == 3 * 64
    assert adapter.lookup_load_mode(REQUEST) is LoadMode.ASYNC
    assert (client.mode_queries, client.plain_queries) == (0, 1)


def test_any_server_choosing_layerwise_makes_the_request_layerwise(
    make_scheduler: SchedulerFactory,
) -> None:
    """A server that deferred keys needs a layerwise retrieve to fetch them."""
    adapter = make_scheduler(
        [_Client(3, LoadMode.ASYNC), _Client(3, LoadMode.LAYERWISE)], True
    )

    _finish_lookup(adapter)

    assert adapter.lookup_load_mode(REQUEST) is LoadMode.LAYERWISE


def test_cleanup_forgets_the_mode(make_scheduler: SchedulerFactory) -> None:
    adapter = make_scheduler([_Client(3, LoadMode.LAYERWISE)], True)
    _finish_lookup(adapter)

    adapter.cleanup_lookup_result(REQUEST)

    assert adapter.lookup_load_mode(REQUEST) is LoadMode.ASYNC
