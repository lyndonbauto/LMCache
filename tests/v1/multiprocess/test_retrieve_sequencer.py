# SPDX-License-Identifier: Apache-2.0
"""``RetrieveLaunchSequencer``: one worker's retrieves in flight at once.

The worker waits on its newest retrieve only, reading one shared progress
record and one reused event per launch ordinal. These tests check the rules
that keep that sound with several retrieves launching from their own threads:
a newer retrieve's ordinal waits for every older one's, only the newest
begun retrieve publishes, and a failure stops every retrieve in flight before
it is published.
"""

# Standard
from collections.abc import Callable
import threading

# Third Party
import pytest

# First Party
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
)
from lmcache.v1.multiprocess.retrieve_sequencer import (
    RetrieveAbortedError,
    RetrieveLaunchSequencer,
)
from lmcache.v1.platform.base.transfer_gate import StagingSlots, TransferGate

LAUNCHES = 3
#: Long enough for a blocked thread to have reached its wait.
SETTLE_SECONDS = 0.2
JOIN_SECONDS = 5.0


class _Stream:
    """Counts drains, optionally running a check at each."""

    def __init__(self) -> None:
        self.drains = 0
        self.on_drain: Callable[[], None] = lambda: None

    def synchronize(self) -> None:
        self.on_drain()
        self.drains += 1


class _EventBackend:
    """Records which ordinal was recorded, in order."""

    def __init__(self) -> None:
        self.recorded: list[int] = []

    def record_event(self, event: int, stream: object) -> None:
        self.recorded.append(event)


class _Fixture:
    def __init__(self) -> None:
        self.stream = _Stream()
        self.record = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
        self.events = _EventBackend()
        self.gate = TransferGate()
        events: list[object] = list(range(LAUNCHES))
        self.sequencer = RetrieveLaunchSequencer(
            self.stream,
            self.record,
            DaemonLayerLaunchEventPool(
                events,
                self.events,  # type: ignore[arg-type]
                LAUNCHES,
            ),
            self.gate,
        )
        self.enqueued: list[tuple[int, int]] = []

    def enqueue_for(self, generation: int, ordinal: int) -> Callable[..., None]:
        def enqueue(_slots: StagingSlots) -> None:
            self.enqueued.append((generation, ordinal))

        return enqueue

    def launch(self, generation: int, ordinal: int) -> None:
        self.sequencer.launch(
            generation, ordinal, self.enqueue_for(generation, ordinal)
        )

    def published(self) -> tuple[int, int, bool]:
        snapshot = self.record.read()
        return snapshot.generation, snapshot.watermark, snapshot.retrieve_failed


def _in_thread(
    target: Callable[[], None],
) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def test_one_retrieve_publishes_each_ordinal_after_recording_it() -> None:
    f = _Fixture()
    f.sequencer.begin(4)
    assert f.published() == (4, 0, False)

    for ordinal in range(LAUNCHES):
        f.launch(4, ordinal)
        assert f.published() == (4, ordinal + 1, False)
    f.sequencer.complete(4)

    assert f.events.recorded == [0, 1, 2]
    assert f.enqueued == [(4, 0), (4, 1), (4, 2)]


def test_a_newer_retrieve_waits_for_the_older_ones_ordinal() -> None:
    f = _Fixture()
    f.sequencer.admit(4)
    f.sequencer.admit(5)
    f.sequencer.begin(5)

    thread, errors = _in_thread(lambda: f.launch(5, 0))
    thread.join(SETTLE_SECONDS)
    assert thread.is_alive(), "retrieve 5 launched before retrieve 4 reached ordinal 0"
    assert f.enqueued == []

    f.sequencer.begin(4)
    f.launch(4, 0)
    thread.join(JOIN_SECONDS)

    assert not errors
    assert f.enqueued == [(4, 0), (5, 0)]
    # Retrieve 5 owns the record: its event covers both copies of ordinal 0.
    assert f.events.recorded == [0]
    assert f.published() == (5, 1, False)


def test_an_older_retrieve_stops_publishing_once_a_newer_one_began() -> None:
    f = _Fixture()
    f.sequencer.admit(4)
    f.sequencer.begin(4)
    f.launch(4, 0)
    assert f.published() == (4, 1, False)

    f.sequencer.begin(5)
    f.launch(4, 1)

    assert f.published() == (5, 0, False)
    assert f.events.recorded == [0]


def test_an_older_retrieve_that_begins_late_does_not_rewind_the_record() -> None:
    f = _Fixture()
    f.sequencer.admit(4)
    f.sequencer.admit(5)
    f.sequencer.begin(5)

    f.sequencer.begin(4)

    assert f.published() == (5, 0, False)


def test_a_completed_older_retrieve_no_longer_holds_newer_ones_back() -> None:
    f = _Fixture()
    f.sequencer.admit(4)
    f.sequencer.begin(4)
    for ordinal in range(LAUNCHES):
        f.launch(4, ordinal)
    f.sequencer.complete(4)

    f.sequencer.begin(5)
    for ordinal in range(LAUNCHES):
        f.launch(5, ordinal)

    assert f.published() == (5, LAUNCHES, False)


def test_a_failure_stops_every_retrieve_in_flight_and_flags_the_newest() -> None:
    f = _Fixture()
    f.sequencer.admit(4)
    f.sequencer.admit(5)
    f.sequencer.begin(4)
    f.sequencer.begin(5)
    f.launch(4, 0)
    f.launch(5, 0)
    waiting, errors = _in_thread(lambda: f.launch(5, 1))
    waiting.join(SETTLE_SECONDS)
    assert waiting.is_alive()

    f.sequencer.fail(4)
    waiting.join(JOIN_SECONDS)

    assert [type(e) for e in errors] == [RetrieveAbortedError]
    assert f.published() == (5, 1, True)
    assert f.stream.drains == 1
    with pytest.raises(RetrieveAbortedError):
        f.launch(5, 1)


def test_a_failure_before_admission_stops_older_retrieves_in_flight() -> None:
    """The worker reads the newest generation: when it fails, the worker hands
    every block of the step back, so older retrieves must stop copying."""
    f = _Fixture()
    f.sequencer.admit(4)
    f.sequencer.begin(4)
    f.launch(4, 0)

    f.sequencer.fail(5)

    assert f.published() == (5, 0, True)
    with pytest.raises(RetrieveAbortedError):
        f.launch(4, 1)


def test_the_failure_is_published_only_after_the_stream_drains() -> None:
    f = _Fixture()
    f.sequencer.begin(4)
    f.launch(4, 0)
    flagged_at_drain: list[bool] = []
    f.stream.on_drain = lambda: flagged_at_drain.append(f.published()[2])

    f.sequencer.fail(4)

    assert flagged_at_drain == [False]
    assert f.published()[2]


def test_a_failure_waits_out_an_enqueue_already_under_way() -> None:
    """An enqueue that passed its check before the stop must finish before
    the stream is drained, or its copy could land after the flag."""
    f = _Fixture()
    f.sequencer.begin(4)
    inside = threading.Event()
    finish = threading.Event()

    def slow_enqueue(_slots: StagingSlots) -> None:
        inside.set()
        finish.wait(JOIN_SECONDS)
        f.enqueued.append((4, 0))

    launching, _ = _in_thread(lambda: f.sequencer.launch(4, 0, slow_enqueue))
    assert inside.wait(JOIN_SECONDS)
    drained_after_enqueue: list[bool] = []
    f.stream.on_drain = lambda: drained_after_enqueue.append(bool(f.enqueued))
    failing, _ = _in_thread(lambda: f.sequencer.fail(4))
    failing.join(SETTLE_SECONDS)
    assert failing.is_alive(), "the failure did not wait for the enqueue"

    finish.set()
    launching.join(JOIN_SECONDS)
    failing.join(JOIN_SECONDS)

    assert drained_after_enqueue == [True]
    assert f.published()[2]


def test_a_retrieve_that_never_began_fails_without_draining() -> None:
    f = _Fixture()

    f.sequencer.fail(4)

    assert f.stream.drains == 0
    assert f.published() == (4, 0, True)


def test_a_late_failure_leaves_a_newer_retrieve_untouched() -> None:
    f = _Fixture()
    f.sequencer.begin(4)
    for ordinal in range(LAUNCHES):
        f.launch(4, ordinal)
    f.sequencer.complete(4)
    f.sequencer.begin(5)
    f.launch(5, 0)

    f.sequencer.fail(4)

    assert f.published() == (5, 1, False)
    f.launch(5, 1)


def test_releasing_an_unfinished_retrieve_fails_it_and_frees_newer_ones() -> None:
    f = _Fixture()
    f.sequencer.admit(4)
    f.sequencer.admit(5)
    f.sequencer.begin(5)
    waiting, errors = _in_thread(lambda: f.launch(5, 0))
    waiting.join(SETTLE_SECONDS)
    assert waiting.is_alive()

    f.sequencer.release(4)
    waiting.join(JOIN_SECONDS)

    assert [type(e) for e in errors] == [RetrieveAbortedError]
    assert f.published() == (5, 0, True)


def test_releasing_a_completed_retrieve_changes_nothing() -> None:
    f = _Fixture()
    f.sequencer.begin(4)
    for ordinal in range(LAUNCHES):
        f.launch(4, ordinal)
    f.sequencer.complete(4)

    f.sequencer.release(4)

    assert f.published() == (4, LAUNCHES, False)


def test_retrieves_are_admitted_in_generation_order() -> None:
    f = _Fixture()
    f.sequencer.admit(5)

    with pytest.raises(ValueError, match="not newer"):
        f.sequencer.admit(4)
    with pytest.raises(ValueError, match="positive"):
        f.sequencer.admit(0)


def test_a_retrieve_that_already_ran_cannot_begin_again() -> None:
    f = _Fixture()
    f.sequencer.begin(4)
    f.launch(4, 0)

    with pytest.raises(RuntimeError, match="already launched"):
        f.sequencer.begin(4)
    for ordinal in range(1, LAUNCHES):
        f.launch(4, ordinal)
    f.sequencer.complete(4)
    with pytest.raises(ValueError, match="not newer"):
        f.sequencer.begin(4)


def test_ordinals_launch_in_order() -> None:
    f = _Fixture()
    f.sequencer.begin(4)

    with pytest.raises(ValueError, match="ordinal 0 next"):
        f.launch(4, 1)


def test_an_enqueue_that_raises_publishes_nothing() -> None:
    f = _Fixture()
    f.sequencer.begin(4)

    def broken(_slots: StagingSlots) -> None:
        raise RuntimeError("kernel launch failed")

    with pytest.raises(RuntimeError, match="kernel launch failed"):
        f.sequencer.launch(4, 0, broken)

    assert f.published() == (4, 0, False)
    assert f.events.recorded == []


def test_staging_is_intact_only_while_nobody_else_held_the_gate() -> None:
    gate = TransferGate()
    mine = gate.new_holder()

    with gate.hold(mine) as slots:
        assert slots is StagingSlots.OVERWRITTEN
    with gate.hold(mine) as slots:
        assert slots is StagingSlots.INTACT
    with gate.hold():
        pass
    with gate.hold(mine) as slots:
        assert slots is StagingSlots.OVERWRITTEN
    with gate.hold() as slots:
        assert slots is StagingSlots.OVERWRITTEN


def test_the_gate_admits_one_holder_at_a_time() -> None:
    gate = TransferGate()
    inside = threading.Event()
    leave = threading.Event()
    order: list[str] = []

    def first() -> None:
        with gate.hold():
            inside.set()
            leave.wait(JOIN_SECONDS)
            order.append("first")

    holder, _ = _in_thread(first)
    assert inside.wait(JOIN_SECONDS)
    second, _ = _in_thread(lambda: _hold_and_note(gate, order))
    second.join(SETTLE_SECONDS)
    assert second.is_alive()

    leave.set()
    holder.join(JOIN_SECONDS)
    second.join(JOIN_SECONDS)

    assert order == ["first", "second"]


def _hold_and_note(gate: TransferGate, order: list[str]) -> None:
    with gate.hold():
        order.append("second")
