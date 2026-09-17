# SPDX-License-Identifier: Apache-2.0
"""Cross-process per-layer retrieve progress for multiprocess mode.

The daemon enqueues H2D work on its transfer stream and records a CUDA IPC
event after each per-layer launch. The vLLM worker must not run attention on
layer *L* until that launch has completed. Because the processes do not share
a stream, progress is published through a fixed shared-memory record (generation
and watermark) plus a pool of IPC events indexed by launch ordinal.

The generation plays the same role as in the RDMA layer pipeline: it tags one
retrieve so a worker never waits on an event that still holds a previous
retrieve's recording. The watermark counts how many ordinals have been enqueued
and recorded; on a single transfer stream, that count is a tight bound for
layer-order waits when launches follow :class:`LayerwiseSchedule`.
"""

# Standard
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# First Party
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule

_RECORD_STRUCT = struct.Struct("<QQI")
"""Layout: generation (uint64), watermark (uint64), flags (uint32)."""

_FLAG_RETRIEVE_FAILED = 1 << 0


class LayerProgressError(Exception):
    """Base error for layer-progress synchronization failures."""


class LayerProgressRetrieveFailedError(LayerProgressError):
    """The daemon reported that the retrieve failed before this layer landed.

    Waiters raise this after a bounded poll when the failure flag is set, or
    when the watermark cannot reach the required ordinal within the timeout.
    """


class LayerProgressStaleGenerationError(LayerProgressError):
    """Shared memory carries a generation that does not match this wait.

    This prevents acting on progress from a different retrieve after a timeout,
    cancellation, or a newly started load reused the same worker slot.
    """


class LayerProgressLayerNotScheduledError(LayerProgressError):
    """``wait_for_layer`` was called for a layer the schedule does not cover."""


@dataclass(frozen=True)
class LayerProgressSnapshot:
    """One read of the shared progress record."""

    generation: int
    """Retrieve generation the daemon last published."""

    watermark: int
    """Launch ordinals recorded through the current generation."""

    retrieve_failed: bool
    """True when the daemon marked the retrieve as failed partway."""


class LayerProgressRecord:
    """Generation and watermark bookkeeping backed by shared memory.

    This class is device-free and safe to unit test with any writable buffer
    of :data:`RECORD_SIZE` bytes.
    """

    RECORD_SIZE: int = _RECORD_STRUCT.size

    def __init__(self, buffer: memoryview | bytearray | bytes) -> None:
        """Attach to a fixed-size shared progress buffer.

        Args:
            buffer: At least :attr:`RECORD_SIZE` bytes, writable for writers.

        Raises:
            ValueError: If ``buffer`` is too short or not writable when writes
                are attempted.
        """
        if len(buffer) < self.RECORD_SIZE:
            raise ValueError(
                f"layer progress record requires {self.RECORD_SIZE} bytes, "
                f"got {len(buffer)}"
            )
        self._buffer = buffer

    def read(self) -> LayerProgressSnapshot:
        """Return the current generation, watermark, and failure flag.

        Returns:
            A snapshot of the shared record.
        """
        generation, watermark, flags = _RECORD_STRUCT.unpack_from(self._buffer, 0)
        return LayerProgressSnapshot(
            generation=generation,
            watermark=watermark,
            retrieve_failed=bool(flags & _FLAG_RETRIEVE_FAILED),
        )

    def begin_retrieve(self, generation: int) -> None:
        """Start a new retrieve generation and reset progress.

        Args:
            generation: Strictly positive identifier for this retrieve. Must
                differ from the generation workers still polling for.

        Raises:
            ValueError: If ``generation`` is not positive.
        """
        if generation <= 0:
            raise ValueError("generation must be positive")
        _RECORD_STRUCT.pack_into(self._buffer, 0, generation, 0, 0)

    def report_launch_recorded(self, watermark: int) -> None:
        """Publish that launches through ``watermark`` are enqueued and recorded.

        Args:
            watermark: Count of completed launch ordinals (inclusive), matching
                :meth:`LayerwiseSchedule.wait_ordinal`.

        Raises:
            ValueError: If ``watermark`` is negative.
        """
        if watermark < 0:
            raise ValueError("watermark must be non-negative")
        generation, _, flags = _RECORD_STRUCT.unpack_from(self._buffer, 0)
        _RECORD_STRUCT.pack_into(self._buffer, 0, generation, watermark, flags)

    def mark_retrieve_failed(self) -> None:
        """Signal that the retrieve failed before all layers were reported."""
        generation, watermark, flags = _RECORD_STRUCT.unpack_from(self._buffer, 0)
        flags |= _FLAG_RETRIEVE_FAILED
        _RECORD_STRUCT.pack_into(self._buffer, 0, generation, watermark, flags)


class LayerLaunchEventPool(Protocol):
    """CUDA IPC events indexed by launch ordinal (injectable in tests)."""

    def wait_on_ordinal(self, ordinal: int, wait_ordinal: int) -> None:
        """Make the worker compute stream wait through ``wait_ordinal``.

        Args:
            ordinal: Zero-based launch ordinal whose recording must be waited on.
            wait_ordinal: Inclusive count from the schedule; equals
                ``ordinal + 1`` for a tight wait on that ordinal alone.

        Raises:
            LayerProgressError: If the underlying event wait fails.
        """


class LayerProgressWaiter:
    """Worker-side wait for one layer's KV to land on the paged buffer."""

    def __init__(
        self,
        record: LayerProgressRecord,
        event_pool: LayerLaunchEventPool,
        *,
        poll_interval_seconds: float = 0.0001,
        wait_timeout_seconds: float = 600.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind a shared record and event pool for retrieve waits.

        Args:
            record: Shared progress record for this worker instance.
            event_pool: Pool of IPC events recorded by the daemon per ordinal.
            poll_interval_seconds: Sleep between shared-memory polls while
                waiting for the watermark.
            wait_timeout_seconds: Maximum time to wait for progress before
                raising :class:`LayerProgressRetrieveFailedError`.
            monotonic: Clock for timeout measurement (injectable in tests).
            sleep: Sleep callable (injectable in tests).
        """
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if wait_timeout_seconds <= 0:
            raise ValueError("wait_timeout_seconds must be positive")
        self._record = record
        self._event_pool = event_pool
        self._poll_interval_seconds = poll_interval_seconds
        self._wait_timeout_seconds = wait_timeout_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    def wait_for_layer(
        self,
        generation: int,
        layer_id: int,
        schedule: LayerwiseSchedule,
    ) -> None:
        """Block until the retrieve has landed ``layer_id`` on the worker GPU.

        Polls shared memory until the generation matches this retrieve and the
        watermark reaches :meth:`LayerwiseSchedule.wait_ordinal`, then waits on
        the IPC event for that ordinal on the compute stream (via the pool).

        Args:
            generation: Retrieve generation the worker received with this load.
            layer_id: Global layer index vLLM is about to compute.
            schedule: Launch order for this model layout.

        Raises:
            LayerProgressLayerNotScheduledError: If ``layer_id`` is not in
                ``schedule``.
            LayerProgressStaleGenerationError: If shared memory shows a
                different generation while this wait is in progress.
            LayerProgressRetrieveFailedError: If the retrieve failed or progress
                stalled beyond ``wait_timeout_seconds``.
        """
        if layer_id not in schedule:
            raise LayerProgressLayerNotScheduledError(
                f"layer {layer_id} is not covered by the layerwise schedule"
            )
        wait_ordinal = schedule.wait_ordinal(layer_id)
        launch_ordinal = wait_ordinal - 1
        self._wait_for_watermark(generation, wait_ordinal)
        self._event_pool.wait_on_ordinal(launch_ordinal, wait_ordinal)

    def _wait_for_watermark(self, generation: int, wait_ordinal: int) -> None:
        deadline = self._monotonic() + self._wait_timeout_seconds
        while True:
            snapshot = self._record.read()
            if snapshot.retrieve_failed:
                raise LayerProgressRetrieveFailedError(
                    "retrieve failed before layer data was reported complete; "
                    "check daemon logs for the transfer error"
                )
            if snapshot.generation != generation:
                raise LayerProgressStaleGenerationError(
                    f"expected retrieve generation {generation}, "
                    f"shared memory shows {snapshot.generation}"
                )
            if snapshot.watermark >= wait_ordinal:
                return
            if self._monotonic() >= deadline:
                raise LayerProgressRetrieveFailedError(
                    f"timed out after {self._wait_timeout_seconds}s waiting for "
                    f"launch ordinal {wait_ordinal} (watermark "
                    f"{snapshot.watermark})"
                )
            self._sleep(self._poll_interval_seconds)


def layer_progress_shm_name(instance_id: int) -> str:
    """Return the POSIX shared-memory name for a worker's progress record.

    Args:
        instance_id: Worker GPU instance identifier from registration.

    Returns:
        Name suitable for :class:`multiprocessing.shared_memory.SharedMemory`.
    """
    if instance_id < 0:
        raise ValueError("instance_id must be non-negative")
    return f"lmcache_mp_layer_progress_{instance_id}"


class WorkerComputeLayerLaunchEventPool:
    """Worker-side IPC event pool waited on from the compute stream."""

    def __init__(
        self,
        events: list[object],
        event_backend: object,
        compute_stream: object,
    ) -> None:
        """Hold imported IPC events indexed by launch ordinal.

        Args:
            events: Imported events, length ``schedule.launch_count()``.
            event_backend: Platform :class:`EventIPCBackend` instance.
            compute_stream: Stream vLLM uses for attention (worker compute).
        """
        if not events:
            raise ValueError("events must be non-empty for layerwise mode")
        self._events = events
        self._event_backend = event_backend
        self._compute_stream = compute_stream

    def wait_on_ordinal(self, ordinal: int, wait_ordinal: int) -> None:
        """Wait on the event recorded for ``ordinal``."""
        if ordinal < 0 or ordinal >= len(self._events):
            raise ValueError(
                f"ordinal {ordinal} out of range for pool size {len(self._events)}"
            )
        if wait_ordinal != ordinal + 1:
            raise ValueError(
                "wait_ordinal must equal ordinal + 1 for a tight layer wait"
            )
        self._event_backend.wait_event(self._events[ordinal], self._compute_stream)


class DaemonLayerLaunchEventPool:
    """Daemon-side pool that re-records ordinals on each retrieve."""

    def __init__(
        self,
        events: list[object],
        event_backend: object,
    ) -> None:
        """Hold IPC-imported worker events for recording on the transfer stream.

        Args:
            events: Events imported from the worker export at registration.
            event_backend: Platform :class:`EventIPCBackend` instance.
        """
        if not events:
            raise ValueError("events must be non-empty for layerwise mode")
        self._events = events
        self._event_backend = event_backend

    def record_ordinal(self, ordinal: int, stream: object) -> None:
        """Record completion of the launch enqueued for ``ordinal``."""
        if ordinal < 0 or ordinal >= len(self._events):
            raise ValueError(
                f"ordinal {ordinal} out of range for pool size {len(self._events)}"
            )
        self._event_backend.record_event(self._events[ordinal], stream)

    @property
    def launch_capacity(self) -> int:
        """Return how many ordinals this pool can record."""
        return len(self._events)
