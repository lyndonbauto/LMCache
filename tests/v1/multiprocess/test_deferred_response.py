# SPDX-License-Identifier: Apache-2.0
"""A blocking handler returning ``DeferredResponse`` frees its pool thread.

The ZMQ server sends the response when the deferred work resolves, so a
client whose requests share one affinity thread gets later requests answered
while an earlier one is still pending.
"""

# Standard
from concurrent.futures import Future

# Third Party
import pytest
import zmq

# First Party
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.deferred_response import (
    DeferredResponse,
    response_annotation,
)
from lmcache.v1.multiprocess.mq import MessageQueueClient, MessageQueueServer
from lmcache.v1.multiprocess.protocol import RequestType
from lmcache.v1.multiprocess.transport.zmq_impl.server import add_handler_helper

SERVER_URL = "tcp://127.0.0.1:16040"


def _key() -> IPCCacheServerKey:
    return IPCCacheServerKey.from_token_ids(
        "model", 1, 0, [0] * 256, start=0, end=256, request_id="req"
    )


def test_a_deferred_response_is_its_futures_result() -> None:
    pending: Future[int] = Future()
    deferred = DeferredResponse(pending)
    seen: list[int] = []
    deferred.add_done_callback(lambda fut: seen.append(fut.result()))

    pending.set_result(3)

    assert deferred.result(timeout=1) == 3
    assert seen == [3]
    assert DeferredResponse.resolved(5).result(timeout=0) == 5


def test_the_response_type_of_a_deferred_annotation_is_its_argument() -> None:
    assert (
        response_annotation(DeferredResponse[tuple[bytes, bool]])
        == (tuple[bytes, bool])
    )
    assert response_annotation(tuple[bytes, bool]) == tuple[bytes, bool]


def test_a_later_request_is_answered_while_a_deferred_one_is_pending() -> None:
    release_first: Future[tuple[bytes, bool]] = Future()

    def retrieve(
        key: IPCCacheServerKey,
        gpu_id: int,
        gpu_block_ids: list[list[int]],
        event_handle: bytes,
        skip_first_n_tokens: int = 0,
        retrieve_generation: int = 0,
    ) -> DeferredResponse[tuple[bytes, bool]]:
        if retrieve_generation == 1:
            return DeferredResponse(release_first)
        return DeferredResponse.resolved((b"second", True))

    context = zmq.Context.instance()
    server = MessageQueueServer(SERVER_URL, context)
    add_handler_helper(server, RequestType.RETRIEVE, retrieve)
    # One thread per client: without deferral the first request would hold it.
    server.add_affinity_thread_pool([RequestType.RETRIEVE], max_workers=1)
    server.start()
    client = MessageQueueClient(SERVER_URL, context)
    try:
        first = client.submit_request(
            RequestType.RETRIEVE, [_key(), 0, [[0]], b"", 0, 1]
        )
        second = client.submit_request(
            RequestType.RETRIEVE, [_key(), 0, [[0]], b"", 0, 2]
        )

        assert second.result(timeout=5) == (b"second", True)
        with pytest.raises(TimeoutError):
            first.result(timeout=0.2)

        release_first.set_result((b"first", False))
        assert first.result(timeout=5) == (b"first", False)
    finally:
        client.close()
        server.close()
