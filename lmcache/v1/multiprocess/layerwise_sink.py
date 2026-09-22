# SPDX-License-Identifier: Apache-2.0
"""Track B's side of the layerwise contract: per-layer host-to-device load.

This is the skeleton the multiprocess loader fills in. The shape is fixed --
it is one half of the frozen contract in
:mod:`lmcache.v1.layerwise.contract` -- so that Track C can write the pump
and Track A can write the transport before any of this works.

Every method raises :class:`NotImplementedError` today. Replace them with real
behaviour; do not change the signatures without agreement, because two other
people are building against them.

Most of the machinery already exists:
:class:`~lmcache.v1.multiprocess.layerwise_schedule.LayerwiseSchedule` decides
launch order, ``transfer_kv_layerwise_h2d`` issues the copies, and
:mod:`lmcache.v1.multiprocess.layer_progress` carries per-layer progress
across the process boundary. What is missing is the adapter that presents all
of it as a :class:`~lmcache.v1.layerwise.contract.LayerLoadSink`, which is
this file.
"""

# Standard
from collections.abc import Sequence


class MultiprocessLayerLoadSink:
    """Copies layers to the GPU as the transport reports them resident.

    Implements :class:`~lmcache.v1.layerwise.contract.LayerLoadSink`. See that
    protocol for the full contract; the notes here are the parts specific to
    this loader.

    Calls arrive from a single thread in a fixed sequence: :meth:`begin_load`,
    then :meth:`load_layer` once per layer in ascending order, then
    :meth:`finish_load` or :meth:`abandon_load`.
    """

    def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None:
        """Prepare to load ``layer_ids`` for one fetch.

        Implementation notes for whoever fills this in: this is where the
        :class:`~lmcache.v1.multiprocess.layerwise_schedule.LayerwiseSchedule`
        and the shared-memory progress record are established for the
        generation. The worker must hold the ``SharedMemory`` object for the
        record's whole lifetime -- dropping the handle early caused a
        use-after-free of the memoryview mid-retrieve.

        Args:
            generation: The fetch generation these layers belong to.
            layer_ids: Global layer indices in the order they will be loaded.

        Raises:
            LayerwiseContractError: If a load is already in progress.
        """
        raise NotImplementedError(
            "Track B: establish the schedule and progress record for this load"
        )

    def load_layer(self, layer_id: int) -> None:
        """Copy one layer from the host buffer to the GPU.

        Implementation notes for whoever fills this in: record the per-ordinal
        CUDA event **before** bumping the progress watermark, or a waiter can
        observe the watermark and proceed past an event that was never
        recorded. Copies share a stream, so issuing out of order makes an
        earlier layer appear ready before its copy was queued -- wrong output,
        no crash.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If ``layer_id`` was not passed to
                :meth:`begin_load`, or is not the next one expected.
            LayerwiseContractError: If called before :meth:`begin_load`.
        """
        raise NotImplementedError("Track B: launch the H2D copy for this layer")

    def finish_load(self, generation: int) -> None:
        """Complete a load whose layers were all issued.

        Args:
            generation: The generation passed to :meth:`begin_load`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active load.
            LayerwiseContractError: If some layer was never issued, since a
                consumer would then wait forever on a copy nobody made.
        """
        raise NotImplementedError("Track B: retire the completed load")

    def abandon_load(self, generation: int) -> None:
        """Give up on a load, failing everything waiting on its layers.

        Every parked waiter must be woken with a failure. This is the path
        taken when the transport declines a layer, and a silent abandon turns
        a recoverable cache miss into a hang inside vLLM's attention.

        Must tolerate being called when no load is active.

        Args:
            generation: The generation passed to :meth:`begin_load`.
        """
        raise NotImplementedError(
            "Track B: fail every waiter and tear down the load"
        )
