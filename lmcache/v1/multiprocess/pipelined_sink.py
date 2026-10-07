# SPDX-License-Identifier: Apache-2.0
"""Track B's sink for the pipelined retrieve (C9 hand-off).

The daemon's pipelined retrieve (``pipelined_loading.fetch_deferred_objects``)
builds one sink per retrieve through a
:class:`~lmcache.v1.multiprocess.pipelined_loading.PipelinedSinkFactory`.
:class:`MultiprocessPipelinedSinkFactory` is that factory. Its sink is
:class:`~lmcache.v1.multiprocess.layerwise_sink.MultiprocessLayerLoadSink`
over one :class:`~lmcache.v1.multiprocess.object_group_transfer.LayerwiseH2DRetrieve`,
plus :meth:`PipelinedRetrieveSink.wait_for_copies`, which the retrieve calls
before it releases the window the copies read from.

How it meets the retrieve's requirements:

- **Objects are read when a layer loads.** The launcher reads the request's
  ``ObjectTable`` at each launch, so whole objects the fallback swaps in are
  the ones the remaining layers copy from.
- **Staging is per layer.** Window objects are incomplete until their last
  layer lands, so only the launched layer's planes are copied.
- **Generations.** The pump's fetch generation drives the sink's contract
  checks; the launcher publishes the request's ``retrieve_generation``, which
  is what the worker waits on. The two are numbered independently.

Kept apart from ``layerwise_sink.py`` so that module stays free of GPU
imports.
"""

# Standard
from collections.abc import Sequence

# First Party
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.layerwise_sink import MultiprocessLayerLoadSink
from lmcache.v1.multiprocess.object_group_transfer import LayerwiseH2DRetrieve
from lmcache.v1.multiprocess.pipelined_loading import PipelinedLoadRequest


class PipelinedRetrieveSink:
    """The sink of one pipelined retrieve, with a wait for its GPU copies.

    Implements :class:`~lmcache.v1.multiprocess.pipelined_loading.PipelinedSink`.
    The load methods are :class:`MultiprocessLayerLoadSink`'s, serving exactly
    one load (``for_retrieve``); a load that pauses after a transport failure
    and resumes with ``load_layer`` for the remaining layers is still that one
    load. Not thread-safe: calls come from the retrieve's thread.
    """

    def __init__(
        self, schedule: LayerwiseSchedule, launcher: LayerwiseH2DRetrieve
    ) -> None:
        """Build the sink over one retrieve's launcher.

        Args:
            schedule: The worker's registered launch order.
            launcher: The retrieve's launcher, not yet begun.
        """
        self._sink = MultiprocessLayerLoadSink.for_retrieve(schedule, launcher)
        self._launcher = launcher

    def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None:
        """Begin the load; see :meth:`MultiprocessLayerLoadSink.begin_load`."""
        self._sink.begin_load(generation, layer_ids)

    def load_layer(self, layer_id: int) -> None:
        """Load one layer; see :meth:`MultiprocessLayerLoadSink.load_layer`."""
        self._sink.load_layer(layer_id)

    def finish_load(self, generation: int) -> None:
        """Finish the load; see :meth:`MultiprocessLayerLoadSink.finish_load`."""
        self._sink.finish_load(generation)

    def abandon_load(self, generation: int) -> None:
        """Abandon the load; see :meth:`MultiprocessLayerLoadSink.abandon_load`."""
        self._sink.abandon_load(generation)

    def wait_for_copies(self) -> None:
        """Block until every copy issued so far has finished reading host memory.

        Safe after a finish or an abandon, and before any load began.
        """
        self._launcher.wait_for_copies()


class MultiprocessPipelinedSinkFactory:
    """Builds Track B's sink for each pipelined retrieve.

    Implements
    :class:`~lmcache.v1.multiprocess.pipelined_loading.PipelinedSinkFactory`.
    Stateless, so one instance serves every retrieve of the daemon.
    """

    def build(self, request: PipelinedLoadRequest) -> PipelinedRetrieveSink:
        """Build a sink over one retrieve, with no load begun.

        Args:
            request: The retrieve's destination, objects and progress.

        Returns:
            A sink whose launcher stages per layer from ``request.objects``
            and publishes ``request.retrieve_generation``.

        Raises:
            ValueError: If ``request.retrieve_generation`` is not positive.
        """
        launcher = LayerwiseH2DRetrieve(
            request.cache_context,
            request.block_ids_gpu,
            request.objects,
            request.skip_first_n_tokens,
            request.schedule,
            request.sequencer,
            request.retrieve_generation,
        )
        return PipelinedRetrieveSink(request.schedule, launcher)
