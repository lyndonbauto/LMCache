# SPDX-License-Identifier: Apache-2.0
"""Conformance-suite harness for Track B's :class:`MultiprocessLayerLoadSink`.

Registered in ``SINK_HARNESS_FACTORIES`` from ``conftest.py`` and imported
only when the factory runs, so the layerwise package's own tests do not
depend on multiprocess code.

What runs is production code wherever it matters to the suite: the real sink,
the real :class:`LayerwiseH2DRetrieve` launcher (per-layer staging, event
before watermark, generation-guarded failure), the real
:class:`LayerProgressRecord`, and -- for the observer -- the real worker-side
:class:`LayerProgressWaiter`. Only the cache context and the event backend are
stand-ins, and the retrieve has no memory objects, so no bytes move: the suite
checks ordering, readiness and failure, which is what it is for. Byte
correctness of per-layer staging is covered by
``tests/v1/multiprocess/test_object_group_layerwise_transfer.py``.

Each load gets its own progress record. In production one record per worker
is reused by successive retrieves, and a waiter on a finished generation would
see the next one's; the suite instead asks what each load's own waiters see,
which a record per load answers directly. Keeping the shared record safe
across retrieves is covered separately (``mark_failed`` never touches a newer
generation, and the waiter ignores an older generation's failure flag).
"""

# Standard
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

# Third Party
import torch

# First Party
from lmcache.v1.layerwise import LayerWaitOutcome
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerLaunchEventPool,
    LayerProgressError,
    LayerProgressRecord,
    LayerProgressRetrieveFailedError,
    LayerProgressWaiter,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.layerwise_sink import (
    LayerLauncher,
    MultiprocessLayerLoadSink,
)
from lmcache.v1.multiprocess.object_group_transfer import LayerwiseH2DRetrieve

#: Hybrid layout: kernel groups hold global layers [0, 2] and [1, 3], so the
#: schedule interleaves them as 0, 1, 2, 3. Covers every layer the suite uses.
_KERNEL_GROUP_LAYERS = [[0, 2], [1, 3]]


class _NoopEventBackend:
    """Event backend whose events do nothing; no device is involved."""

    device_type = "fake"

    def check_event_support(self, device: object) -> None:
        return None

    def create_event(self, device: object) -> object:
        return object()

    def export_event(self, event: object, device: object) -> bytes:
        return b""

    def import_event(self, handle: bytes, device: object) -> object:
        return handle

    def record_event(self, event: object, stream: object) -> None:
        return None

    def wait_event(self, event: object, stream: object) -> None:
        return None

    def query_event(self, event: object) -> bool:
        return True

    def synchronize_event(self, event: object, device: object) -> None:
        return None


def _cache_context() -> MagicMock:
    """A cache context for one object group of two kernel groups."""
    cache_context = MagicMock()
    cache_context.lmcache_tokens_per_chunk = 16
    cache_context.max_batch_size = 4
    cache_context.device = torch.device("cpu")
    cache_context.stream = MagicMock(name="transfer_stream")
    cache_context.get_temp_kernel_group_buffer = lambda _slot, _kernel_group: (
        torch.zeros((2, 2, 4, 8))
    )
    manager = MagicMock()
    manager.object_groups = [SimpleNamespace(kernel_group_indices=[0, 1])]
    attention = MagicMock()
    attention.is_full_attention = MagicMock(return_value=True)
    attention.num_chunks_in_sw = [-1]
    manager.get_attn_desc = MagicMock(return_value=attention)
    cache_context.kv_layer_groups_manager = manager
    return cache_context


@dataclass(frozen=True)
class _LoadRecord:
    """What the observer needs to answer for one load."""

    record: LayerProgressRecord
    retrieve_generation: int


class _ProgressRecordObserver:
    """Answers what a worker waiting on a load's layers would see.

    Implements :class:`~lmcache.v1.layerwise.fakes.LoadObserver` by asking the
    real :class:`LayerProgressWaiter` with a near-zero timeout, so the answer
    comes from the same code the worker runs.
    """

    def __init__(self, schedule: LayerwiseSchedule) -> None:
        """Build an observer over one schedule.

        Args:
            schedule: The launch order the sink and its launchers share.
        """
        self._schedule = schedule
        self._loads: dict[int, _LoadRecord] = {}
        self._load_layers: dict[int, frozenset[int]] = {}

    def track(self, fetch_generation: int, load: _LoadRecord) -> None:
        """Associate a fetch generation with the record its load publishes to.

        Args:
            fetch_generation: The generation passed to ``begin_load``.
            load: That load's record and retrieve generation.
        """
        self._loads[fetch_generation] = load

    def track_layers(self, fetch_generation: int, layer_ids: tuple[int, ...]) -> None:
        """Record the layers a load was begun with.

        Args:
            fetch_generation: The generation passed to ``begin_load``.
            layer_ids: The layers passed to ``begin_load``.
        """
        self._load_layers[fetch_generation] = frozenset(layer_ids)

    def issued_layers(self, generation: int) -> tuple[int, ...]:
        """Return the load's layers whose copies have been published, in order.

        The sink also launches scheduled layers a load skips, so the watermark
        tracks schedule position; those are not layers of the load and are
        left out here.

        Args:
            generation: The generation passed to ``begin_load``.

        Returns:
            The load's layers covered by its watermark.
        """
        load = self._loads.get(generation)
        if load is None:
            return ()
        snapshot = load.record.read()
        if snapshot.generation != load.retrieve_generation:
            return ()
        load_layers = self._load_layers.get(generation, frozenset())
        launches = self._schedule.launches[: snapshot.watermark]
        return tuple(
            launch.layer_id for launch in launches if launch.layer_id in load_layers
        )

    def wait_outcome(self, layer_id: int, generation: int) -> LayerWaitOutcome:
        """Return what a waiter on ``layer_id`` would see now, without blocking.

        Args:
            layer_id: Global layer index in the model.
            generation: The generation passed to ``begin_load``.

        Returns:
            ``READY`` if the waiter would proceed, ``FAILED`` if it would be
            told the load failed, ``PENDING`` if it would still be waiting.
        """
        load = self._loads.get(generation)
        if load is None:
            return LayerWaitOutcome.PENDING
        waiter = LayerProgressWaiter(
            load.record,
            LayerLaunchEventPool(),
            poll_interval_seconds=1e-6,
            wait_timeout_seconds=1e-6,
        )
        try:
            waiter.wait_for_layer(load.retrieve_generation, layer_id, self._schedule)
        except LayerProgressRetrieveFailedError:
            return LayerWaitOutcome.FAILED
        except LayerProgressError:
            return LayerWaitOutcome.PENDING
        return LayerWaitOutcome.READY


class _ObservedSink(MultiprocessLayerLoadSink):
    """The real sink, telling the observer which layers each load covers."""

    def __init__(
        self,
        schedule: LayerwiseSchedule,
        launchers: Callable[[int], LayerLauncher],
        observer: _ProgressRecordObserver,
    ) -> None:
        """Build the sink and remember the observer to tell.

        Args:
            schedule: The launch order the sink and its launchers share.
            launchers: Launcher factory, as for the real sink.
            observer: Told each accepted load's layers.
        """
        super().__init__(schedule, launchers)
        self._observer = observer

    def begin_load(self, generation: int, layer_ids: tuple[int, ...]) -> None:
        """Begin the load, then record its layers with the observer."""
        super().begin_load(generation, layer_ids)
        self._observer.track_layers(generation, layer_ids)


def multiprocess_sink_harness() -> tuple[
    MultiprocessLayerLoadSink, _ProgressRecordObserver
]:
    """Build Track B's sink and an observer reading its progress records.

    Returns:
        ``(sink, observer)`` for a ``SinkHarness``.
    """
    schedule = LayerwiseSchedule(_KERNEL_GROUP_LAYERS)
    observer = _ProgressRecordObserver(schedule)
    retrieve_generations = iter(range(1, 1 << 30))

    def launcher_for(fetch_generation: int) -> LayerLauncher:
        retrieve_generation = next(retrieve_generations)
        record = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
        observer.track(fetch_generation, _LoadRecord(record, retrieve_generation))
        launch_count = schedule.launch_count()
        return LayerwiseH2DRetrieve(
            _cache_context(),
            [torch.tensor([0]), torch.tensor([0])],
            [[]],
            0,
            schedule,
            record,
            DaemonLayerLaunchEventPool(
                [object()] * launch_count, _NoopEventBackend(), launch_count
            ),
            retrieve_generation,
        )

    return _ObservedSink(schedule, launcher_for, observer), observer
