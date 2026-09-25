# SPDX-License-Identifier: Apache-2.0
"""Track B's side of the layerwise contract: per-layer host-to-device load.

:class:`MultiprocessLayerLoadSink` presents the multiprocess layerwise loader
as a :class:`~lmcache.v1.layerwise.contract.LayerLoadSink`, so the arrival
pump can copy each layer to the GPU as soon as the transport reports it
resident, instead of copying the whole request at once.

The sink owns only the contract's bookkeeping -- layer order, generation, and
the finish/abandon rules. The GPU work sits behind :class:`LayerLauncher`,
implemented in production by
:class:`~lmcache.v1.multiprocess.object_group_transfer.LayerwiseH2DRetrieve`.
That split keeps all transfer code in one module and lets the sink be tested
on a machine with no GPU.

Two generations are in play and must not be confused:

- the *fetch generation*, assigned by the transport and passed to every
  contract method; the sink uses it only for the contract's own checks;
- the *retrieve generation*, assigned by the vLLM worker and published to the
  shared progress record; it is bound into the launcher before the sink is
  built.

A sink serves exactly one retrieve and accepts exactly one ``begin_load``.
Production wiring constructs it as::

    MultiprocessLayerLoadSink(schedule, LayerwiseH2DRetrieve(...))

``LayerwiseH2DRetrieve`` must use its default ``LayerStaging.PER_LAYER``
here: layers arrive one at a time, and whole-object staging would copy later
layers before they land.
"""

# Standard
from collections.abc import Sequence
from enum import Enum
from typing import Protocol

# First Party
from lmcache.v1.layerwise.contract import (
    NO_GENERATION,
    LayerNotInPlanError,
    LayerwiseContractError,
    StaleGenerationError,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule


class LayerLauncher(Protocol):
    """Performs the GPU side of one retrieve, one scheduled layer at a time.

    Implemented by
    :class:`~lmcache.v1.multiprocess.object_group_transfer.LayerwiseH2DRetrieve`.
    Calls come from a single thread.
    """

    def begin(self) -> None:
        """Do once-per-retrieve setup and publish the retrieve generation."""
        ...

    def launch_layer(self, layer_id: int) -> None:
        """Copy one layer to the GPU and publish that it has landed.

        Args:
            layer_id: Global layer index; the next one in schedule order.
        """
        ...

    def mark_failed(self) -> None:
        """Publish that the retrieve failed, waking every waiter on it."""
        ...


class _SinkState(Enum):
    """Lifecycle of one :class:`MultiprocessLayerLoadSink`."""

    #: No load has begun. ``begin_load`` is allowed.
    UNUSED = "unused"
    #: A load is active; layers are being issued.
    LOADING = "loading"
    #: Every layer was issued and the load finished cleanly.
    FINISHED = "finished"
    #: The load was abandoned and the retrieve marked failed.
    ABANDONED = "abandoned"


class MultiprocessLayerLoadSink:
    """Copies layers to the GPU as the transport reports them resident.

    Implements :class:`~lmcache.v1.layerwise.contract.LayerLoadSink`. See that
    protocol for the full contract; the notes here are the parts specific to
    this loader.

    Calls arrive from a single thread in a fixed sequence: :meth:`begin_load`,
    then :meth:`load_layer` once per layer in ascending order, then
    :meth:`finish_load` or :meth:`abandon_load`. Not thread-safe.

    The sink is bound to one retrieve and is single use: after it finishes or
    is abandoned, ``begin_load`` is refused.
    """

    def __init__(self, schedule: LayerwiseSchedule, launcher: LayerLauncher) -> None:
        """Bind the sink to one retrieve.

        Args:
            schedule: Launch order for this model layout. ``begin_load`` must be
                given exactly this order, because the worker waits on a
                watermark over these ordinals.
            launcher: Performs the copies for this retrieve. Must already be
                bound to the retrieve generation the worker is waiting on, and
                must not have begun.
        """
        self._launcher = launcher
        #: The only layer order ``begin_load`` accepts: schedule order.
        self._scheduled_layer_ids: tuple[int, ...] = tuple(
            launch.layer_id for launch in schedule.launches
        )
        #: Same layers as a set, for O(1) membership checks per layer.
        self._scheduled_layer_set = frozenset(self._scheduled_layer_ids)
        self._state = _SinkState.UNUSED
        #: Fetch generation of the active or finished load; ``NO_GENERATION``
        #: until ``begin_load`` succeeds.
        self._generation = NO_GENERATION
        #: Index into ``_scheduled_layer_ids`` of the next layer to issue.
        self._next_index = 0

    def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None:
        """Prepare to load ``layer_ids`` for one fetch.

        Runs the launcher's once-per-retrieve setup and publishes the retrieve
        generation, so worker waiters can start matching progress to it.

        Args:
            generation: The fetch generation these layers belong to. Must not
                be :data:`~lmcache.v1.layerwise.contract.NO_GENERATION`.
            layer_ids: Global layer indices in the order they will be loaded.
                Must equal the schedule's launch order exactly -- every
                scheduled layer, ascending.

        Raises:
            LayerwiseContractError: If a load is already in progress or this
                sink was already used, if ``generation`` is the reserved "no
                fetch" value, or if ``layer_ids`` differs from the schedule.
                The sink is left unused in the last two cases.
            ValueError: If the launcher's setup rejects the retrieve's memory
                objects. The sink is left unused.
        """
        if self._state is not _SinkState.UNUSED:
            raise LayerwiseContractError(
                f"this sink is {self._state.value}; each sink serves one load"
            )
        if generation == NO_GENERATION:
            raise LayerwiseContractError(
                f"generation {NO_GENERATION} is reserved for 'no fetch'"
            )
        requested = tuple(layer_ids)
        if requested != self._scheduled_layer_ids:
            raise LayerwiseContractError(
                f"layer order {list(requested)} does not match the schedule "
                f"{list(self._scheduled_layer_ids)}; every scheduled layer must "
                "be loaded, in schedule order, or a worker wait would hang or "
                "pass with no data behind it"
            )
        self._launcher.begin()
        self._generation = generation
        self._next_index = 0
        self._state = _SinkState.LOADING

    def load_layer(self, layer_id: int) -> None:
        """Copy one layer from the host buffer to the GPU.

        The launcher records the layer's completion event before advancing
        the progress watermark, so a waiter that observes the watermark never
        proceeds past an event that was not recorded.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If ``layer_id`` was not passed to
                :meth:`begin_load`.
            LayerwiseContractError: If called out of order, before
                :meth:`begin_load`, or after every layer was issued. Nothing is
                copied in these cases.
            RuntimeError: If the copy itself fails. The launcher has already
                marked the retrieve failed.
        """
        if self._state is not _SinkState.LOADING:
            raise LayerwiseContractError(
                f"cannot load layer {layer_id}: sink is {self._state.value}"
            )
        if layer_id not in self._scheduled_layer_set:
            raise LayerNotInPlanError(f"layer {layer_id} is not in this load")
        if self._next_index >= len(self._scheduled_layer_ids):
            raise LayerwiseContractError(
                f"layer {layer_id} issued after all "
                f"{len(self._scheduled_layer_ids)} layers"
            )
        expected = self._scheduled_layer_ids[self._next_index]
        if layer_id != expected:
            raise LayerwiseContractError(
                f"expected layer {expected} next, got {layer_id}; out-of-order "
                "issue would make an earlier layer look ready before its copy"
            )
        self._launcher.launch_layer(layer_id)
        self._next_index += 1

    def finish_load(self, generation: int) -> None:
        """Complete a load whose layers were all issued.

        Args:
            generation: The generation passed to :meth:`begin_load`.

        Raises:
            StaleGenerationError: If no load is active or ``generation`` is not
                the active load's.
            LayerwiseContractError: If some layer was never issued, since a
                consumer would then wait forever on a copy nobody made.
        """
        if self._state is not _SinkState.LOADING or generation != self._generation:
            raise StaleGenerationError(
                f"generation {generation} is not the active load "
                f"(sink is {self._state.value}, active is {self._generation})"
            )
        if self._next_index != len(self._scheduled_layer_ids):
            missing = self._scheduled_layer_ids[self._next_index :]
            raise LayerwiseContractError(
                f"load finished with layers {list(missing)} never issued"
            )
        self._state = _SinkState.FINISHED

    def abandon_load(self, generation: int) -> None:
        """Give up on a load, failing everything waiting on its layers.

        Marks the bound retrieve failed so every parked waiter wakes with an
        error. This is the path taken when the transport declines a layer, and
        a silent abandon would turn a recoverable cache miss into a hang
        inside vLLM's attention.

        Because this sink serves exactly one retrieve, ``generation`` is not
        matched: any abandon before a clean finish fails the retrieve,
        including one that follows a ``begin_load`` which was itself refused.
        Abandoning after a clean finish, or a second time, does nothing.

        Args:
            generation: The generation passed to :meth:`begin_load`. Accepted
                for contract compatibility; not used to decide anything.
        """
        del generation
        if self._state in (_SinkState.FINISHED, _SinkState.ABANDONED):
            return
        self._launcher.mark_failed()
        self._state = _SinkState.ABANDONED
