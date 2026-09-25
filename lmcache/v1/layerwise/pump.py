# SPDX-License-Identifier: Apache-2.0
"""The junction between layer arrival and layer loading.

This is the only component that holds both sides of the layerwise contract.
Everything else depends on one side and fakes the other, so this file is where
an integration mistake between the two workstreams will show up.

The pump exists because the transport and the loader have no reason to know
about each other. The transport knows when bytes land in host memory; the
loader knows how to get host memory onto the GPU. Neither should own the
policy of how long to wait or what to do when a layer never arrives, because
that policy is the same regardless of which transport or loader is in use.
"""

# Standard
from collections.abc import Callable
import time

# Local
from .contract import (
    LayerArrivalSource,
    LayerArrivalStatus,
    LayerArrivalTimeoutError,
    LayerFetchPlan,
    LayerLoadSink,
    LayerUnservableError,
)

#: Default gap between arrival polls. Deliberately short: a layer copy is tens
#: of microseconds, so a millisecond-scale poll would dominate the very
#: latency layerwise loading exists to remove.
DEFAULT_POLL_INTERVAL_SECONDS = 0.0001

#: Default ceiling on waiting for a single layer. It must stay below the
#: worker's per-layer wait (``lmcache.mp.layerwise_wait_timeout_seconds``,
#: 5 s by default) so the pump gives up first: a worker that times out first
#: leaves attention while the daemon may still copy layers into GPU blocks
#: vLLM no longer expects to be written. The pump waits for a layer from
#: before the worker does, so an equal timeout still expires first; the
#: margin covers the time the abandon takes to reach the worker.
DEFAULT_LAYER_TIMEOUT_SECONDS = 2.5


class LayerArrivalPump:
    """Drives layers from the transport to the loader as they arrive.

    For each layer in plan order the pump waits for the transport to report
    the layer resident, then asks the loader to copy it. Any failure abandons
    both sides, so a caller that catches the exception can fall back to a
    whole-request load without first having to unwind either half.
    """

    def __init__(
        self,
        source: LayerArrivalSource,
        sink: LayerLoadSink,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        layer_timeout_seconds: float = DEFAULT_LAYER_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build a pump over one transport and one loader.

        Args:
            source: Reports which layers have landed in host memory.
            sink: Copies landed layers to the GPU.
            poll_interval_seconds: Gap between arrival polls.
            layer_timeout_seconds: Longest the pump waits for any one layer
                before abandoning the fetch.
            clock: Monotonic time source, in seconds. Injectable so tests can
                exercise the timeout path without actually waiting.
            sleep: Blocking sleep, in seconds. Injectable for the same reason.

        Raises:
            ValueError: If ``poll_interval_seconds`` is negative or
                ``layer_timeout_seconds`` is not positive, either of which
                would make the wait loop meaningless.
        """
        if poll_interval_seconds < 0.0:
            raise ValueError(
                f"poll_interval_seconds must not be negative, got "
                f"{poll_interval_seconds}"
            )
        if layer_timeout_seconds <= 0.0:
            raise ValueError(
                f"layer_timeout_seconds must be positive, got {layer_timeout_seconds}"
            )
        self._source = source
        self._sink = sink
        self._poll_interval_seconds = poll_interval_seconds
        self._layer_timeout_seconds = layer_timeout_seconds
        self._clock = clock
        self._sleep = sleep

    def run(self, plan: LayerFetchPlan) -> int:
        """Fetch ``plan`` and load each layer as soon as it lands.

        Blocks until every layer of the plan has been handed to the loader.

        Args:
            plan: The slots to fetch and the layers to load.

        Returns:
            The generation the transport assigned to this fetch, so a caller
            can correlate it with transport-side diagnostics.

        Raises:
            LayerUnservableError: If the transport reports that some layer
                will never arrive. Both sides have been abandoned.
            LayerArrivalTimeoutError: If some layer did not arrive within
                ``layer_timeout_seconds``. Both sides have been abandoned.
            LayerwiseContractError: If either side reports a contract
                violation. Both sides have been abandoned.
        """
        layer_ids = plan.layer_ids()
        generation = self._source.begin_fetch(plan)
        try:
            self._sink.begin_load(generation, layer_ids)
            for layer_id in layer_ids:
                self._await_layer(layer_id, generation)
                self._sink.load_layer(layer_id)
            self._sink.finish_load(generation)
        except BaseException:
            # Abandon rather than finish: outstanding writes may still be in
            # flight, and the loader may have waiters parked on layers that
            # will now never be issued.
            self._sink.abandon_load(generation)
            self._source.abandon_fetch(generation)
            raise
        self._source.finish_fetch(generation)
        return generation

    def _await_layer(self, layer_id: int, generation: int) -> None:
        """Block until ``layer_id`` is resident in host memory.

        Args:
            layer_id: Global layer index in the model.
            generation: The active fetch generation.

        Raises:
            LayerUnservableError: If the layer will never arrive.
            LayerArrivalTimeoutError: If the layer did not arrive in time.
        """
        deadline = self._clock() + self._layer_timeout_seconds
        while True:
            status = self._source.poll_layer(layer_id, generation)
            if status is LayerArrivalStatus.RESIDENT:
                return
            if status is LayerArrivalStatus.UNSERVABLE:
                raise LayerUnservableError(
                    f"layer {layer_id} of generation {generation} will never "
                    f"arrive; fall back to a whole-request load"
                )
            # The deadline is checked after polling so that a layer which is
            # already resident is never rejected for lateness.
            if self._clock() >= deadline:
                raise LayerArrivalTimeoutError(
                    f"layer {layer_id} of generation {generation} did not "
                    f"arrive within {self._layer_timeout_seconds} seconds"
                )
            self._sleep(self._poll_interval_seconds)
