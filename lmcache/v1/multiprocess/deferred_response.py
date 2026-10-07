# SPDX-License-Identifier: Apache-2.0
"""A blocking-handler response that is sent when a future resolves.

A blocking handler normally holds its pool thread until it returns the
response. One annotated ``-> DeferredResponse[R]`` may instead return as soon
as the order-sensitive part of the request is done and finish the rest on
another thread; the transport sends ``R`` when that work resolves. The
affinity pool keeps one thread per client, so this is how a handler frees
the client's thread without reordering the client's requests.

Example::

    @request_handler(RequestType.RETRIEVE, HandlerType.BLOCKING,
                     requires_client_affinity=True)
    def retrieve(self, ...) -> DeferredResponse[tuple[bytes, bool]]:
        if not slow:
            return DeferredResponse.resolved((handle, True))
        return DeferredResponse(self._pool.submit(self._finish, ...))
"""

# Standard
from collections.abc import Callable
from concurrent.futures import Future
from typing import Generic, TypeVar, get_args, get_origin

T = TypeVar("T")


class DeferredResponse(Generic[T]):
    """A handler response delivered when its future resolves."""

    def __init__(self, future: "Future[T]") -> None:
        """Wrap the future whose result is the response.

        Args:
            future: Resolves to the response, or raises the handler's error.
        """
        self._future = future

    @classmethod
    def resolved(cls, value: T) -> "DeferredResponse[T]":
        """Return a response that is already available.

        Args:
            value: The response.

        Returns:
            A deferred response whose future is already done.
        """
        future: Future[T] = Future()
        future.set_result(value)
        return cls(future)

    def result(self, timeout: float | None = None) -> T:
        """Block until the response is available and return it.

        Args:
            timeout: Seconds to wait; ``None`` waits indefinitely.

        Returns:
            The response.

        Raises:
            concurrent.futures.TimeoutError: If ``timeout`` passed first.
            Exception: Whatever the deferred work raised.
        """
        return self._future.result(timeout)

    def add_done_callback(self, callback: Callable[["Future[T]"], None]) -> None:
        """Run ``callback`` with the resolved future, now if already done.

        Args:
            callback: Called once, with the future; read it with ``result()``.
        """
        self._future.add_done_callback(callback)


def response_annotation(annotation: object) -> object:
    """Return the response type a handler's return annotation sends.

    Args:
        annotation: A handler's return annotation.

    Returns:
        ``R`` for ``DeferredResponse[R]``, otherwise ``annotation`` unchanged.
    """
    if get_origin(annotation) is DeferredResponse:
        (response_type,) = get_args(annotation)
        return response_type
    return annotation
