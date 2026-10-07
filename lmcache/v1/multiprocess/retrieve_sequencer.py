# SPDX-License-Identifier: Apache-2.0
"""Lets one worker's layerwise retrieves be in flight at the same time.

vLLM submits every retrieve of a step before the forward pass, and the
worker waits on the newest one only: its per-layer wait reads the shared
progress record (generation, watermark) and then the IPC event of the launch
ordinal. The record has room for one generation, and the event pool is
reused by every generation. Two rules keep that sound while several
retrieves of one worker fetch and launch at once:

- **Ordinal order across retrieves.** Retrieve ``g`` may enqueue launch
  ordinal ``k`` only once every older retrieve still in flight has enqueued
  ``k``. All launches share one transfer stream, so the event the newest
  retrieve records after ``k`` then also covers every older retrieve's
  ``k``.
- **One publisher.** Only the newest retrieve that has begun (the *owner*)
  records events and advances the watermark. Older ones enqueue without
  recording, so an event is never re-recorded under a generation the worker
  already stopped reading.

::

    ordinal:         0      1      2
    retrieve 5 (old) [L0]   [L1]          enqueues, never records
    retrieve 6 (own)    [L0]+ev0   [L1]+ev1 ...   waits for 5's L_k first

A failure stops every retrieve in flight before it is published: the worker
hands all of the step's blocks back to vLLM for recompute, so no copy may
land in them afterwards. The failure is published under the newest
generation the sequencer knows of, which is the one the worker reads when
that retrieve is the newest of its step.

Each launch is enqueued holding the cache context's
:class:`~lmcache.v1.platform.base.transfer_gate.TransferGate`, because other
threads enqueue transfers on the same stream and staging buffers.
"""

# Standard
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
import threading

# First Party
from lmcache.logging import init_logger
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
)
from lmcache.v1.platform.base.transfer_gate import StagingSlots, TransferGate

logger = init_logger(__name__)


class TransferStream(Protocol):
    """The part of a device stream the sequencer uses."""

    def synchronize(self) -> None:
        """Block until all work queued on the stream has finished."""
        ...


class RetrieveAbortedError(RuntimeError):
    """A failed retrieve of the same worker stopped this one.

    Raised by :class:`RetrieveLaunchSequencer` instead of letting the
    retrieve begin or launch another layer. The failure has been, or is
    being, published to the worker.
    """


@dataclass
class _InFlight:
    """One admitted retrieve that has neither completed nor failed."""

    #: Its token on the transfer gate.
    holder: int
    #: Launch ordinals enqueued so far; the next one is this value.
    launched: int = 0
    #: Whether :meth:`RetrieveLaunchSequencer.begin` ran for it.
    began: bool = False


class RetrieveLaunchSequencer:
    """Orders the layer launches of one worker's in-flight layerwise retrieves.

    The only writer of the worker's progress record and launch events. See
    the module docstring for the rules. Lifecycle of one retrieve, by
    generation ``g``:

    1. :meth:`admit` (optional): in the order the worker submitted, before
       any newer retrieve can begin. A retrieve that begins unadmitted is
       admitted then, which is only safe while no other retrieve is queued.
    2. :meth:`begin`, then :meth:`launch` once per ordinal in order, then
       :meth:`complete`; or :meth:`fail` at any point.
    3. :meth:`release`: once, when the retrieve's handler is done.

    Thread-safe. Lock order: the gate, then the sequencer's own lock.
    """

    def __init__(
        self,
        stream: TransferStream,
        progress: LayerProgressRecord,
        event_pool: DaemonLayerLaunchEventPool,
        gate: TransferGate,
    ) -> None:
        """Build a sequencer with nothing in flight.

        Args:
            stream: The cache context's transfer stream; launch events are
                recorded on it, and a failure waits for it.
            progress: The worker's shared progress record.
            event_pool: The worker's per-ordinal launch events.
            gate: The cache context's transfer gate.
        """
        self._stream = stream
        self._progress = progress
        self._event_pool = event_pool
        self._gate = gate
        self._cond = threading.Condition()
        #: Admitted retrieves that have neither completed nor failed.
        self._in_flight: dict[int, _InFlight] = {}
        #: Retrieves a failure stopped; they raise on their next call.
        self._aborted: set[int] = set()
        #: Newest generation admitted, begun or failed; 0 before any.
        self._newest = 0
        #: Newest generation published to the record; 0 before any.
        self._owner = 0

    def admit(self, generation: int) -> None:
        """Register a retrieve so newer ones wait for its launches.

        Call in the order the worker submitted the retrieves, before the
        retrieve is handed to a thread that may begin it.

        Args:
            generation: The retrieve's worker-assigned generation.

        Raises:
            ValueError: If ``generation`` is not positive, or not newer than
                every generation this sequencer has seen.
        """
        with self._cond:
            self._admit_locked(generation)

    def begin(self, generation: int) -> None:
        """Mark a retrieve begun, publishing it if it is now the newest.

        Publishing resets the record's watermark under ``generation``. An
        older retrieve that begins after a newer one publishes nothing.

        Args:
            generation: The retrieve's generation. Admitted if it is newer
                than every generation seen so far.

        Raises:
            RetrieveAbortedError: If a failure already stopped it.
            ValueError: If it is neither in flight nor newer than every
                generation seen, e.g. it already completed.
            RuntimeError: If it already launched an ordinal.
        """
        with self._cond:
            if generation in self._aborted:
                raise RetrieveAbortedError(
                    f"retrieve generation {generation} was stopped by a failure"
                )
            slot = self._in_flight.get(generation)
            if slot is None:
                self._admit_locked(generation)
                slot = self._in_flight[generation]
            if slot.launched:
                raise RuntimeError(
                    f"retrieve generation {generation} already launched "
                    f"{slot.launched} ordinal(s)"
                )
            slot.began = True
            if generation > self._owner:
                self._owner = generation
                self._progress.begin_retrieve(generation)

    def launch(
        self,
        generation: int,
        ordinal: int,
        enqueue: Callable[[StagingSlots], None],
    ) -> None:
        """Enqueue one launch ordinal once every older retrieve reached it.

        Blocks until each older in-flight retrieve has enqueued ``ordinal``,
        completed or failed. Then, holding the gate, runs ``enqueue`` and,
        if this retrieve owns the record, records the ordinal's event and
        advances the watermark past it.

        Args:
            generation: A begun, in-flight retrieve.
            ordinal: Its next launch ordinal.
            enqueue: Enqueues the ordinal's transfer on the current stream.
                Receives whether the staging buffers are as this retrieve
                left them.

        Raises:
            RetrieveAbortedError: If a failure stopped this retrieve, before
                or while it waited. Nothing was enqueued.
            RuntimeError: If the retrieve is not in flight or never began.
            ValueError: If ``ordinal`` is not its next ordinal.
            Exception: Whatever ``enqueue`` raises; nothing is published.
        """
        with self._cond:
            slot = self._check_launchable(generation)
            if ordinal != slot.launched:
                raise ValueError(
                    f"retrieve generation {generation} must launch ordinal "
                    f"{slot.launched} next, got {ordinal}"
                )
            self._cond.wait_for(
                lambda: generation in self._aborted
                or self._older_reached(generation, ordinal)
            )
            self._check_launchable(generation)
        with self._gate.hold(slot.holder) as slots:
            with self._cond:
                self._check_launchable(generation)
            enqueue(slots)
            with self._cond:
                slot.launched = ordinal + 1
                if generation == self._owner:
                    self._event_pool.record_ordinal(ordinal, self._stream)
                    self._progress.report_launch_recorded(ordinal + 1)
                self._cond.notify_all()

    def complete(self, generation: int) -> None:
        """Mark a retrieve done: every ordinal enqueued, newer ones need not wait.

        Args:
            generation: The retrieve's generation. Unknown generations are
                ignored.
        """
        with self._cond:
            if self._in_flight.pop(generation, None) is not None:
                self._cond.notify_all()

    def fail(self, generation: int) -> None:
        """Publish that a retrieve failed, once no copy can still land.

        If ``generation`` is in flight, or newer than every generation seen
        (it failed before it could be admitted), every in-flight retrieve is
        stopped: each raises :class:`RetrieveAbortedError` on its next call.
        Once no enqueue is under way and the stream has drained (skipped when
        none of them began), the failure is published under the newest of
        those generations and the owner.

        Otherwise the retrieve already completed or failed. Its copies are
        drained and the failure is published only if it still owns the
        record, so a late failure never fails or rewinds a newer retrieve.

        Safe to call more than once.

        Args:
            generation: The failed retrieve's generation; positive.

        Raises:
            ValueError: If ``generation`` is not positive.
        """
        if generation <= 0:
            raise ValueError(f"generation must be positive, got {generation}")
        with self._cond:
            stops_step = generation in self._in_flight or generation > self._newest
            if stops_step:
                # Retrieves that never began queued nothing to wait for.
                drain = any(slot.began for slot in self._in_flight.values())
                stopped = set(self._in_flight)
                stopped.discard(generation)
                self._aborted |= stopped
                target = max([generation, self._owner, *stopped])
                self._in_flight.clear()
                self._newest = max(self._newest, generation)
                self._cond.notify_all()
            else:
                drain = True
                target = generation
        if drain:
            if stops_step:
                # An enqueue that passed its abort check before the stop
                # holds the gate; taking it waits that enqueue out.
                with self._gate.hold():
                    pass
            self._stream.synchronize()
        with self._cond:
            self._progress.fail_retrieve(target)
            self._owner = max(self._owner, target)

    def release(self, generation: int) -> None:
        """Forget a retrieve whose handler is done.

        A retrieve released while still in flight stopped without completing
        or failing; it is failed here so newer retrieves do not wait for it
        forever.

        Args:
            generation: The retrieve's generation.
        """
        with self._cond:
            self._aborted.discard(generation)
            unfinished = generation in self._in_flight
        if unfinished:
            logger.warning(
                "Layerwise retrieve generation %d ended without completing; failing it",
                generation,
            )
            self.fail(generation)

    def _admit_locked(self, generation: int) -> None:
        """Admit ``generation``; the caller holds the sequencer's lock."""
        if generation <= 0:
            raise ValueError(f"generation must be positive, got {generation}")
        if generation <= self._newest:
            raise ValueError(
                f"retrieve generation {generation} is not newer than "
                f"generation {self._newest}"
            )
        self._newest = generation
        self._in_flight[generation] = _InFlight(holder=self._gate.new_holder())

    def _check_launchable(self, generation: int) -> _InFlight:
        """Return the slot of a begun, in-flight retrieve; lock held.

        Raises:
            RetrieveAbortedError: If a failure stopped it.
            RuntimeError: If it is not in flight or never began.
        """
        if generation in self._aborted:
            raise RetrieveAbortedError(
                f"retrieve generation {generation} was stopped by a failure"
            )
        slot = self._in_flight.get(generation)
        if slot is None or not slot.began:
            raise RuntimeError(
                f"retrieve generation {generation} is not a begun retrieve in flight"
            )
        return slot

    def _older_reached(self, generation: int, ordinal: int) -> bool:
        """Whether every older in-flight retrieve enqueued ``ordinal``; lock held."""
        return all(
            slot.launched > ordinal
            for other, slot in self._in_flight.items()
            if other < generation
        )
