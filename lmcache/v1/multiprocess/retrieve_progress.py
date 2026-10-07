# SPDX-License-Identifier: Apache-2.0
"""Publish several in-flight retrieves of one worker through its progress record.

A pipelined retrieve runs its network fetch off the request thread, so one
worker can have several retrieves loading at once. Three things on the daemon
side assume one retrieve at a time, and this module covers all three:

- **The progress record** holds one generation and one watermark, and the
  worker waits only on the generation of its latest retrieve. So the record
  shows the latest generation with the *smallest* watermark of every retrieve
  still loading: when the worker sees layer *L* ready, every retrieve of its
  step has launched layer *L*.
- **The per-ordinal launch events** are shared. Each launch records its
  ordinal's event on the one transfer stream, after the copy, so the last
  recording of an ordinal covers every launch of it queued before. The
  watermark passes an ordinal only after every loading retrieve launched it.
- **The cache context's GPU staging buffers** are shared. Each launch stages
  and runs its kernel inside :meth:`RetrieveProgress.launching`, under one
  reentrant lock per worker, so two launches never interleave there. Stores
  take the same lock through :meth:`ConcurrentRetrieveProgress.staging`.

A failure stops every retrieve still loading: the worker's connector
already reports all of the step's retrieves for recompute when one wait
fails, and a retrieve left running would keep copying into blocks vLLM has
taken back. The failure is published only after the transfer stream drains,
so no copy lands after the worker sees it::

    progress = ConcurrentRetrieveProgress(record, schedule.launch_count(), drain)
    retrieve = progress.register(generation)   # on the request thread, in order
    ...                                        # launches, on any thread
    retrieve.close()                           # always; unfinished = failed
"""

# Standard
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import Enum
import threading

# First Party
from lmcache.logging import init_logger
from lmcache.v1.multiprocess.layer_progress import (
    LayerProgressRecord,
    LayerProgressSnapshot,
)

logger = init_logger(__name__)


class RetrieveStoppedError(RuntimeError):
    """The retrieve was stopped before this launch, so it must not copy.

    Raised by :meth:`RetrieveProgress.launching` and
    :meth:`RetrieveProgress.begin_retrieve` once another retrieve of the same
    worker failed, or the worker is unregistering. The failure is already
    published; the retrieve only has to release what it holds.
    """


class RetrieveState(Enum):
    """Where one registered retrieve is."""

    #: Registered, not every ordinal launched yet.
    LOADING = "loading"
    #: Every ordinal launched.
    DONE = "done"
    #: Failed, or stopped because another retrieve of its step failed.
    STOPPED = "stopped"


@dataclass
class _RetrieveSlot:
    """One registered retrieve's progress, guarded by the owner's lock."""

    generation: int
    watermark: int = 0
    state: RetrieveState = RetrieveState.LOADING


class RetrieveProgress:
    """The progress publisher of one retrieve in a :class:`ConcurrentRetrieveProgress`.

    Implements
    :class:`~lmcache.v1.multiprocess.layer_progress.LayerProgressPublisher`
    for :class:`~lmcache.v1.multiprocess.object_group_transfer.LayerwiseH2DRetrieve`.
    Built by :meth:`ConcurrentRetrieveProgress.register`; thread-safe.
    """

    def __init__(
        self,
        slot: _RetrieveSlot,
        report: Callable[[_RetrieveSlot, int], None],
        fail: Callable[[_RetrieveSlot], None],
        launching: Callable[[_RetrieveSlot], AbstractContextManager[None]],
    ) -> None:
        """Bind to one registered retrieve.

        Args:
            slot: The retrieve's progress, owned by the shared progress.
            report: Records a watermark and publishes the smallest one.
            fail: Publishes a failure, stopping the step's other retrieves.
            launching: Holds the staging lock for one launch.
        """
        self._slot = slot
        self._report = report
        self._fail = fail
        self._launching = launching

    @property
    def generation(self) -> int:
        """This retrieve's generation."""
        return self._slot.generation

    @property
    def state(self) -> RetrieveState:
        """Whether this retrieve is loading, done or stopped."""
        return self._slot.state

    def read(self) -> LayerProgressSnapshot:
        """Return this retrieve's own progress, not the record's.

        Returns:
            This retrieve's generation and watermark; ``retrieve_failed`` is
            set once it was stopped.
        """
        return LayerProgressSnapshot(
            generation=self._slot.generation,
            watermark=self._slot.watermark,
            retrieve_failed=self._slot.state is RetrieveState.STOPPED,
        )

    def begin_retrieve(self, generation: int) -> None:
        """Check that the launcher begins this retrieve; registering published it.

        Args:
            generation: The launcher's generation.

        Raises:
            ValueError: If ``generation`` is not this retrieve's.
            RetrieveStoppedError: If the retrieve was already stopped.
        """
        self._check_generation(generation)
        if self._slot.state is RetrieveState.STOPPED:
            raise RetrieveStoppedError(f"retrieve generation {generation} was stopped")

    def report_launch_recorded(self, watermark: int) -> None:
        """Record that this retrieve's launches through ``watermark`` are recorded.

        The record's watermark moves only when this was the slowest loading
        retrieve.

        Args:
            watermark: This retrieve's launched-ordinal count.

        Raises:
            ValueError: If ``watermark`` is below an earlier report.
        """
        self._report(self._slot, watermark)

    def fail_retrieve(self, generation: int) -> None:
        """Publish this retrieve's failure, stopping its step's other retrieves.

        Args:
            generation: This retrieve's generation.

        Raises:
            ValueError: If ``generation`` is not this retrieve's.
        """
        self._check_generation(generation)
        self._fail(self._slot)

    def launching(self) -> AbstractContextManager[None]:
        """Hold the worker's GPU staging for one launch.

        Returns:
            A context manager holding the worker's staging lock. Entering it
            raises :class:`RetrieveStoppedError` if the retrieve was stopped.
        """
        return self._launching(self._slot)

    def close(self) -> None:
        """End the retrieve; one that did not launch every ordinal has failed.

        Safe to call more than once and in any state.
        """
        if self._slot.state is RetrieveState.LOADING:
            self._fail(self._slot)

    def _check_generation(self, generation: int) -> None:
        if generation != self._slot.generation:
            raise ValueError(
                f"generation {generation} does not match registered retrieve "
                f"{self._slot.generation}"
            )


class ConcurrentRetrieveProgress:
    """One worker's progress record, shared by every retrieve it has in flight.

    Thread-safe. :meth:`register` must be called on the request thread, in
    the order the worker numbered its retrieves; everything else may be
    called from any thread.
    """

    def __init__(
        self,
        record: LayerProgressRecord,
        launch_count: int,
        drain: Callable[[], None],
    ) -> None:
        """Wrap a worker's record.

        Args:
            record: The worker's shared progress record.
            launch_count: Launch ordinals in the worker's schedule; a
                retrieve whose watermark reaches it is done.
            drain: Blocks until every copy queued on the worker's transfer
                stream has finished. Called before a failure is published.

        Raises:
            ValueError: If ``launch_count`` is not positive.
        """
        if launch_count <= 0:
            raise ValueError(f"launch_count must be positive, got {launch_count}")
        self._record = record
        self._launch_count = launch_count
        self._drain = drain
        self._lock = threading.RLock()
        self._published_generation = 0
        self._published_watermark = 0
        self._loading: dict[int, _RetrieveSlot] = {}

    def register(self, generation: int) -> RetrieveProgress:
        """Start retrieve ``generation`` and publish it as the latest.

        The record shows ``generation`` from now on, with a watermark of 0
        until every loading retrieve has launched its first ordinal.

        Args:
            generation: The worker's generation for this retrieve; strictly
                greater than any registered before.

        Returns:
            The retrieve's publisher. Close it when the retrieve ends.

        Raises:
            ValueError: If ``generation`` is not greater than the last one.
        """
        with self._lock:
            if generation <= self._published_generation:
                raise ValueError(
                    f"retrieve generation {generation} is not newer than "
                    f"{self._published_generation}"
                )
            self._published_generation = generation
            self._published_watermark = 0
            self._record.begin_retrieve(generation)
            slot = _RetrieveSlot(generation)
            self._loading[generation] = slot
            return RetrieveProgress(slot, self._report, self._fail, self._launching)

    def fail_generation(self, generation: int) -> None:
        """Publish that retrieve ``generation`` failed, registered or not.

        For a retrieve that failed before it registered. A generation newer
        than any registered is registered and failed; a loading one fails as
        :meth:`RetrieveProgress.fail_retrieve` does; an older, finished one
        is left alone.

        Args:
            generation: The failed retrieve's generation; strictly positive.

        Raises:
            ValueError: If ``generation`` is not positive.
        """
        if generation <= 0:
            raise ValueError("generation must be positive")
        with self._lock:
            slot = self._loading.get(generation)
            if slot is None and generation > self._published_generation:
                self.register(generation)
                slot = self._loading[generation]
            if slot is not None:
                self._fail(slot)

    def stop_all(self) -> int:
        """Stop every loading retrieve and publish the failure.

        For unregistering the worker: no retrieve may copy into its KV cache
        afterwards.

        Returns:
            How many retrieves were stopped.
        """
        with self._lock:
            stopped = len(self._loading)
            if stopped:
                self._stop_loading_and_publish()
            return stopped

    def loading_count(self) -> int:
        """Return how many registered retrieves have not finished or failed."""
        with self._lock:
            return len(self._loading)

    def staging(self) -> AbstractContextManager[bool]:
        """Hold the worker's GPU staging buffers for a transfer.

        For transfers that are not a retrieve's launch, such as a store's
        device-to-host copy. Reentrant.

        Returns:
            The lock, as a context manager.
        """
        return self._lock

    def _report(self, slot: _RetrieveSlot, watermark: int) -> None:
        """Record ``slot``'s watermark and publish the smallest loading one."""
        with self._lock:
            if slot.state is not RetrieveState.LOADING:
                return
            if watermark < slot.watermark:
                raise ValueError(
                    f"watermark {watermark} of retrieve {slot.generation} "
                    f"is below its previous {slot.watermark}"
                )
            slot.watermark = watermark
            if watermark >= self._launch_count:
                slot.state = RetrieveState.DONE
                del self._loading[slot.generation]
            published = min(
                (s.watermark for s in self._loading.values()),
                default=self._launch_count,
            )
            if published != self._published_watermark:
                self._published_watermark = published
                self._record.report_launch_recorded(published)

    def _fail(self, slot: _RetrieveSlot) -> None:
        """Publish ``slot``'s failure, stopping every retrieve of its step.

        Retrieves of different steps never load at once (a step's forward
        pass waits for all of its retrieves), so the loading ones are this
        step's. A retrieve that already finished is failed only while it is
        still part of the published step: it is the published one, or others
        are loading.
        """
        with self._lock:
            if slot.state is RetrieveState.STOPPED:
                return
            if slot.state is RetrieveState.DONE and not (
                self._loading or slot.generation == self._published_generation
            ):
                return
            slot.state = RetrieveState.STOPPED
            self._loading.pop(slot.generation, None)
            self._stop_loading_and_publish()

    def _stop_loading_and_publish(self) -> None:
        """Stop the loading retrieves, drain the stream, publish the failure."""
        if self._loading:
            logger.warning(
                "Stopping %d other in-flight retrieve(s) of generation <= %d",
                len(self._loading),
                self._published_generation,
            )
        for slot in self._loading.values():
            slot.state = RetrieveState.STOPPED
        self._loading.clear()
        self._drain()
        self._record.fail_retrieve(self._published_generation)

    @contextmanager
    def _launching(self, slot: _RetrieveSlot) -> Iterator[None]:
        """Hold the staging lock for one of ``slot``'s launches."""
        with self._lock:
            if slot.state is RetrieveState.STOPPED:
                raise RetrieveStoppedError(
                    f"retrieve generation {slot.generation} was stopped"
                )
            yield
