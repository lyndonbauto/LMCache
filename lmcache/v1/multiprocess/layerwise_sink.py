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
  shared progress record; it is bound into each launcher before the launcher
  reaches the sink.

Readiness is a watermark over positions in the worker's launch schedule, so a
load may cover any strictly ascending subset of the scheduled layers. Layers
the load skips are launched on the way past -- copying whatever their memory
objects hold, or nothing for groups the retrieve does not serve -- and trailing
ones at ``finish_load``, so the watermark always reaches the layer a waiter
asked for.

Production wiring builds one sink per retrieve::

    MultiprocessLayerLoadSink.for_retrieve(schedule, LayerwiseH2DRetrieve(...))

``LayerwiseH2DRetrieve`` must use its default ``LayerStaging.PER_LAYER``
here: layers arrive one at a time, and whole-object staging would copy later
layers before they land.
"""

# Standard
from collections.abc import Callable, Sequence
from itertools import pairwise
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
    """Performs the GPU side of one load, one scheduled layer at a time.

    Implemented by
    :class:`~lmcache.v1.multiprocess.object_group_transfer.LayerwiseH2DRetrieve`.
    Layers are launched in schedule order, every scheduled layer exactly once.
    Calls come from a single thread.
    """

    def begin(self) -> None:
        """Do once-per-load setup and publish the retrieve generation."""
        ...

    def launch_layer(self, layer_id: int) -> None:
        """Copy one layer to the GPU and publish that it has landed.

        Args:
            layer_id: Global layer index; the next one in schedule order.
        """
        ...

    def mark_failed(self) -> None:
        """Publish that the load failed, waking every waiter on it."""
        ...


#: Returns the launcher for the load that ``begin_load(generation, ...)`` is
#: starting. Must return a launcher that has not begun, or raise
#: :class:`LayerwiseContractError` if it cannot serve another load.
LauncherFactory = Callable[[int], LayerLauncher]


class MultiprocessLayerLoadSink:
    """Copies layers to the GPU as the transport reports them resident.

    Implements :class:`~lmcache.v1.layerwise.contract.LayerLoadSink`. See that
    protocol for the full contract; the notes here are the parts specific to
    this loader.

    Calls arrive from a single thread in a fixed sequence: :meth:`begin_load`,
    then :meth:`load_layer` once per layer in ascending order, then
    :meth:`finish_load` or :meth:`abandon_load`. Not thread-safe. After a
    load finishes or is abandoned, the sink is free for the next one; each
    load gets a fresh launcher from the factory.
    """

    def __init__(self, schedule: LayerwiseSchedule, launchers: LauncherFactory) -> None:
        """Build a sink over one model layout.

        Args:
            schedule: The worker's launch order for this layout. Every load
                must be a strictly ascending subset of it.
            launchers: Supplies one launcher per load; see
                :data:`LauncherFactory`.
        """
        self._launchers = launchers
        #: Every scheduled layer, in launch (ascending global layer) order.
        self._scheduled_layer_ids: tuple[int, ...] = tuple(
            launch.layer_id for launch in schedule.launches
        )
        #: Same layers as a set, for O(1) membership checks per layer.
        self._scheduled_layer_set = frozenset(self._scheduled_layer_ids)
        #: Launcher of the active load; ``None`` when no load is active.
        self._launcher: LayerLauncher | None = None
        #: Fetch generation of the active load, ``NO_GENERATION`` when idle.
        self._generation = NO_GENERATION
        #: The active load's layers, strictly ascending.
        self._load_layer_ids: tuple[int, ...] = ()
        #: Same layers as a set, for O(1) membership checks per layer.
        self._load_layer_set: frozenset[int] = frozenset()
        #: Index into ``_load_layer_ids`` of the next layer to issue.
        self._next_load_index = 0
        #: Index into ``_scheduled_layer_ids`` of the next layer to launch.
        self._next_schedule_index = 0

    @classmethod
    def for_retrieve(
        cls, schedule: LayerwiseSchedule, launcher: LayerLauncher
    ) -> "MultiprocessLayerLoadSink":
        """Build a sink that serves exactly one load, with ``launcher``.

        The production shape: one sink per retrieve. A second ``begin_load``
        is refused, because running the same retrieve's launcher twice would
        republish its generation and reset its watermark under waiters that
        may still be parked on it.

        Args:
            schedule: The worker's launch order for this layout.
            launcher: The retrieve's launcher, not yet begun.

        Returns:
            A sink whose first load uses ``launcher``.
        """
        unused = [launcher]

        def one_launcher(generation: int) -> LayerLauncher:
            if not unused:
                raise LayerwiseContractError(
                    f"this retrieve's loader already ran; generation "
                    f"{generation} needs a new retrieve"
                )
            return unused.pop()

        return cls(schedule, one_launcher)

    def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None:
        """Prepare to load ``layer_ids`` for one fetch.

        Obtains this load's launcher and runs its setup, which publishes the
        retrieve generation so worker waiters can start matching progress.

        Args:
            generation: The fetch generation these layers belong to. Must not
                be :data:`~lmcache.v1.layerwise.contract.NO_GENERATION`.
            layer_ids: Global layer indices, strictly ascending, each one in
                the schedule. Scheduled layers not listed are launched when
                the load passes them (see the module docstring).

        Raises:
            LayerwiseContractError: If a load is already active, if
                ``generation`` is the reserved "no fetch" value, if
                ``layer_ids`` is empty, not strictly ascending, or names a
                layer outside the schedule, or if the factory cannot supply a
                launcher. Nothing is issued and any active load is untouched.
            ValueError: If the launcher's setup rejects the retrieve's memory
                objects. No load becomes active.
        """
        if self._launcher is not None:
            raise LayerwiseContractError(
                f"load for generation {self._generation} is still active"
            )
        if generation == NO_GENERATION:
            raise LayerwiseContractError(
                f"generation {NO_GENERATION} is reserved for 'no fetch'"
            )
        requested = tuple(layer_ids)
        if not requested:
            raise LayerwiseContractError("a load must cover at least one layer")
        if any(later <= earlier for earlier, later in pairwise(requested)):
            raise LayerwiseContractError(
                f"layer order {list(requested)} is not strictly ascending; a "
                "watermark over schedule positions would report a layer ready "
                "before its copy was queued"
            )
        unscheduled = [
            layer for layer in requested if layer not in self._scheduled_layer_set
        ]
        if unscheduled:
            raise LayerwiseContractError(
                f"layers {unscheduled} are not in the launch schedule"
            )
        launcher = self._launchers(generation)
        launcher.begin()
        self._launcher = launcher
        self._generation = generation
        self._load_layer_ids = requested
        self._load_layer_set = frozenset(requested)
        self._next_load_index = 0
        self._next_schedule_index = 0

    def load_layer(self, layer_id: int) -> None:
        """Copy one layer from the host buffer to the GPU.

        Any scheduled layers between the previous one and ``layer_id`` are
        launched first, so the watermark reaches ``layer_id``'s own schedule
        position rather than counting copies. The launcher records each
        layer's completion event before advancing the watermark.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If ``layer_id`` was not passed to
                :meth:`begin_load`.
            LayerwiseContractError: If called out of order, repeated, before
                :meth:`begin_load`, or after every layer was issued. Nothing
                is copied in these cases.
            RuntimeError: If a copy itself fails. The launcher has already
                marked the load failed.
        """
        launcher = self._launcher
        if launcher is None:
            raise LayerwiseContractError(
                f"cannot load layer {layer_id}: no load is active"
            )
        if layer_id not in self._load_layer_set:
            raise LayerNotInPlanError(f"layer {layer_id} is not in this load")
        if self._next_load_index >= len(self._load_layer_ids):
            raise LayerwiseContractError(
                f"layer {layer_id} issued after all "
                f"{len(self._load_layer_ids)} layers of this load"
            )
        expected = self._load_layer_ids[self._next_load_index]
        if layer_id != expected:
            raise LayerwiseContractError(
                f"expected layer {expected} next, got {layer_id}; out-of-order "
                "issue would make an earlier layer look ready before its copy"
            )
        self._launch_through(launcher, layer_id)
        self._next_load_index += 1

    def finish_load(self, generation: int) -> None:
        """Complete a load whose layers were all issued.

        Launches any scheduled layers after the load's last one, so no worker
        wait on them is left short of the watermark.

        Args:
            generation: The generation passed to :meth:`begin_load`.

        Raises:
            StaleGenerationError: If no load is active or ``generation`` is not
                the active load's. The active load is left as it was.
            LayerwiseContractError: If some layer was never issued, since a
                consumer would then wait forever on a copy nobody made. The
                active load is left as it was.
        """
        launcher = self._launcher
        if launcher is None or generation != self._generation:
            raise StaleGenerationError(
                f"generation {generation} is not the active load "
                f"(active is {self._generation})"
            )
        if self._next_load_index != len(self._load_layer_ids):
            missing = self._load_layer_ids[self._next_load_index :]
            raise LayerwiseContractError(
                f"load finished with layers {list(missing)} never issued"
            )
        if self._next_schedule_index < len(self._scheduled_layer_ids):
            self._launch_through(launcher, self._scheduled_layer_ids[-1])
        self._reset()

    def abandon_load(self, generation: int) -> None:
        """Give up on a load, failing everything waiting on its layers.

        Marks the active load failed so every parked waiter wakes with an
        error. This is the path taken when the transport declines a layer, and
        a silent abandon would turn a recoverable cache miss into a hang
        inside vLLM's attention.

        Only the active load is affected. With no load active, or for any
        other generation -- one already finished, abandoned, or never begun --
        nothing happens: a finished load's data has landed and must not be
        revoked, and a stale abandon must not fail a newer load. Waking a
        worker whose load never began (for example because ``begin_load`` was
        refused) is the retrieve path's job: it falls back to a whole-object
        load or reports the retrieve failed.

        Args:
            generation: The generation passed to :meth:`begin_load`.
        """
        launcher = self._launcher
        if launcher is None or generation != self._generation:
            return
        try:
            launcher.mark_failed()
        finally:
            self._reset()

    def _launch_through(self, launcher: LayerLauncher, layer_id: int) -> None:
        """Launch every scheduled layer up to and including ``layer_id``.

        Args:
            launcher: The active load's launcher.
            layer_id: The last scheduled layer to launch.
        """
        while True:
            scheduled = self._scheduled_layer_ids[self._next_schedule_index]
            launcher.launch_layer(scheduled)
            self._next_schedule_index += 1
            if scheduled == layer_id:
                return

    def _reset(self) -> None:
        """Return to idle, ready for the next load."""
        self._launcher = None
        self._generation = NO_GENERATION
        self._load_layer_ids = ()
        self._load_layer_set = frozenset()
        self._next_load_index = 0
        self._next_schedule_index = 0
