# SPDX-License-Identifier: Apache-2.0
"""Behaviour of Track B's :class:`MultiprocessLayerLoadSink` beyond the suite.

``test_load_sink_conformance.py`` checks the contract every loader shares.
These tests cover what is specific to this loader: which layers it asks its
launcher to copy (including layers it steps over in a load with gaps), the
one-load-per-retrieve production shape, and how a gapped load reaches the
real worker waiter. They run on CPU with a recording launcher or the
real-record harness; the GPU side is covered by
``tests/v1/multiprocess/test_object_group_layerwise_transfer.py``.
"""

# Standard
from collections.abc import Callable
import threading
import time

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    LayerArrivalPump,
    LayerUnservableError,
    LayerWaitOutcome,
    LayerwiseContractError,
    ScriptedLayerArrivalSource,
    UnservableLayerArrivalSource,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.layerwise_sink import (
    LayerLauncher,
    MultiprocessLayerLoadSink,
)

# Local
from .conftest import RecordingLauncher, make_plan
from .multiprocess_sink_harness import multiprocess_sink_harness

_JOIN_TIMEOUT_SECONDS = 5.0

#: Hybrid layout: groups hold [0, 2] and [1, 3]; schedule order is 0, 1, 2, 3.
_HYBRID_GROUPS = [[0, 2], [1, 3]]
_SCHEDULE_ORDER = (0, 1, 2, 3)

READY = LayerWaitOutcome.READY
PENDING = LayerWaitOutcome.PENDING


class _Launchers:
    """A launcher factory that hands out a fresh recording launcher per load."""

    def __init__(self) -> None:
        """Start with no loads."""
        self.by_generation: dict[int, RecordingLauncher] = {}

    def __call__(self, generation: int) -> LayerLauncher:
        """Return a new launcher for the load starting ``generation``."""
        launcher = RecordingLauncher()
        self.by_generation[generation] = launcher
        return launcher


def _make_sink() -> tuple[MultiprocessLayerLoadSink, _Launchers]:
    """Build a reusable sink over the hybrid schedule."""
    launchers = _Launchers()
    return MultiprocessLayerLoadSink(
        LayerwiseSchedule(_HYBRID_GROUPS), launchers
    ), launchers


def _wait_for(condition_met: Callable[[], bool], description: str) -> None:
    """Poll ``condition_met()`` until true or fail after the join timeout.

    Args:
        condition_met: Zero-argument callable returning a bool.
        description: What is being waited for, used in the failure message.
    """
    deadline = time.monotonic() + _JOIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if condition_met():
            return
        time.sleep(0.001)
    raise AssertionError(f"timed out waiting for {description}")


def _deliver_when_active(source: ScriptedLayerArrivalSource, layer_id: int) -> None:
    """Deliver ``layer_id`` as soon as the pump has started a fetch.

    Args:
        source: The scripted transport the pump is polling.
        layer_id: Layer to mark resident.
    """

    def delivered() -> bool:
        try:
            source.deliver_layer(layer_id)
        except LayerwiseContractError:
            return False
        return True

    _wait_for(delivered, "the pump to start a fetch")


# ---------------------------------------------------------------------------
# What the launcher is asked to do
# ---------------------------------------------------------------------------


def test_a_full_load_launches_every_layer_in_schedule_order() -> None:
    """A clean load runs setup once, then one launch per layer, in order."""
    sink, launchers = _make_sink()

    sink.begin_load(5, _SCHEDULE_ORDER)
    for layer_id in _SCHEDULE_ORDER:
        sink.load_layer(layer_id)
    sink.finish_load(5)

    assert launchers.by_generation[5].calls == ["begin"] + [
        ("launch", layer) for layer in _SCHEDULE_ORDER
    ]


def test_layers_a_load_skips_are_launched_on_the_way_past() -> None:
    """Load (0, 2, 3): issuing layer 2 first launches the skipped layer 1.

    The watermark counts schedule positions, so layer 2 is only ready once
    every earlier scheduled layer has been launched too.
    """
    sink, launchers = _make_sink()
    sink.begin_load(5, (0, 2, 3))

    sink.load_layer(0)
    assert launchers.by_generation[5].launched_layers() == [0]
    sink.load_layer(2)
    assert launchers.by_generation[5].launched_layers() == [0, 1, 2]


def test_trailing_layers_a_load_omits_are_launched_at_finish() -> None:
    """Load (0, 1): finishing launches layers 2 and 3 so no wait on them hangs."""
    sink, launchers = _make_sink()
    sink.begin_load(5, (0, 1))
    sink.load_layer(0)
    sink.load_layer(1)
    assert launchers.by_generation[5].launched_layers() == [0, 1]

    sink.finish_load(5)

    assert launchers.by_generation[5].launched_layers() == [0, 1, 2, 3]


def test_a_layer_outside_the_schedule_is_refused_at_begin() -> None:
    """A load naming a layer the worker never waits on is a caller bug."""
    sink, launchers = _make_sink()

    with pytest.raises(LayerwiseContractError, match="not in the launch schedule"):
        sink.begin_load(5, (0, 7))
    assert launchers.by_generation == {}


def test_an_empty_load_is_refused() -> None:
    """A plan always covers at least one layer."""
    sink, launchers = _make_sink()

    with pytest.raises(LayerwiseContractError, match="at least one layer"):
        sink.begin_load(5, ())
    assert launchers.by_generation == {}


def test_the_reserved_generation_is_refused() -> None:
    """Generation 0 means "no fetch" and must fail loudly."""
    sink, launchers = _make_sink()

    with pytest.raises(LayerwiseContractError, match="reserved"):
        sink.begin_load(0, _SCHEDULE_ORDER)
    assert launchers.by_generation == {}


def test_a_launcher_setup_failure_leaves_the_sink_idle() -> None:
    """If setup raises, no load is active and the sink can take another."""

    class _FailingBegin(RecordingLauncher):
        def begin(self) -> None:
            raise ValueError("null memory object")

    attempts: list[int] = []

    def launchers(generation: int) -> LayerLauncher:
        attempts.append(generation)
        return _FailingBegin() if generation == 5 else RecordingLauncher()

    sink = MultiprocessLayerLoadSink(LayerwiseSchedule(_HYBRID_GROUPS), launchers)

    with pytest.raises(ValueError, match="null memory object"):
        sink.begin_load(5, _SCHEDULE_ORDER)
    with pytest.raises(LayerwiseContractError, match="no load is active"):
        sink.load_layer(0)

    sink.begin_load(6, _SCHEDULE_ORDER)
    assert attempts == [5, 6]


def test_an_abandon_marks_only_the_active_load_failed() -> None:
    """The active load's launcher is marked failed; nothing else is touched."""
    sink, launchers = _make_sink()
    sink.begin_load(5, _SCHEDULE_ORDER)
    sink.load_layer(0)

    sink.abandon_load(5)

    assert launchers.by_generation[5].calls == ["begin", ("launch", 0), "failed"]


def test_an_abandon_with_no_active_load_does_nothing() -> None:
    """Waking a worker whose load never began is the retrieve path's job."""
    sink, launchers = _make_sink()
    with pytest.raises(LayerwiseContractError):
        sink.begin_load(5, (2, 1))

    sink.abandon_load(5)

    assert launchers.by_generation == {}


# ---------------------------------------------------------------------------
# One load per retrieve (the production shape)
# ---------------------------------------------------------------------------


def test_a_retrieve_sink_serves_exactly_one_load() -> None:
    """Rerunning a retrieve's launcher would republish its generation."""
    launcher = RecordingLauncher()
    sink = MultiprocessLayerLoadSink.for_retrieve(
        LayerwiseSchedule(_HYBRID_GROUPS), launcher
    )
    sink.begin_load(5, _SCHEDULE_ORDER)
    for layer_id in _SCHEDULE_ORDER:
        sink.load_layer(layer_id)
    sink.finish_load(5)

    with pytest.raises(LayerwiseContractError, match="already ran"):
        sink.begin_load(6, _SCHEDULE_ORDER)
    assert launcher.calls.count("begin") == 1


def test_a_retrieve_sink_is_spent_by_an_abandoned_load_too() -> None:
    """An abandoned retrieve is not retried through the same launcher."""
    launcher = RecordingLauncher()
    sink = MultiprocessLayerLoadSink.for_retrieve(
        LayerwiseSchedule(_HYBRID_GROUPS), launcher
    )
    sink.begin_load(5, _SCHEDULE_ORDER)
    sink.abandon_load(5)

    with pytest.raises(LayerwiseContractError, match="already ran"):
        sink.begin_load(6, _SCHEDULE_ORDER)


# ---------------------------------------------------------------------------
# A gapped load, seen by the real worker waiter
# ---------------------------------------------------------------------------


def test_a_gapped_load_makes_each_layer_ready_when_it_is_loaded() -> None:
    """Load (0, 2, 3) end to end over the real launcher, record and waiter.

    Complements the shared suite, which stops after layer 0: here layer 2 is
    loaded past the gap and must become ready, with the gap layer 1 ahead of
    it, and finishing leaves every layer ready.
    """
    sink, observer = multiprocess_sink_harness()
    sink.begin_load(5, (0, 2, 3))

    sink.load_layer(0)
    assert [observer.wait_outcome(layer, 5) for layer in (0, 1, 2, 3)] == [
        READY,
        PENDING,
        PENDING,
        PENDING,
    ]
    sink.load_layer(2)
    assert [observer.wait_outcome(layer, 5) for layer in (0, 1, 2, 3)] == [
        READY,
        READY,
        READY,
        PENDING,
    ]
    sink.load_layer(3)
    sink.finish_load(5)
    assert [observer.wait_outcome(layer, 5) for layer in (0, 1, 2, 3)] == [READY] * 4


def test_a_load_missing_trailing_layers_still_readies_them_at_finish() -> None:
    """Load (0, 1) over the real waiter: layers 2 and 3 are ready after finish."""
    sink, observer = multiprocess_sink_harness()
    sink.begin_load(5, (0, 1))
    sink.load_layer(0)
    sink.load_layer(1)
    assert observer.wait_outcome(3, 5) is PENDING

    sink.finish_load(5)

    assert [observer.wait_outcome(layer, 5) for layer in (0, 1, 2, 3)] == [READY] * 4


# ---------------------------------------------------------------------------
# Driven by the real pump
# ---------------------------------------------------------------------------


def test_the_pump_copies_each_layer_only_after_it_arrives() -> None:
    """Layer 0 is copied while later layers have not arrived (B2 on CPU)."""
    sink, launchers = _make_sink()
    source = ScriptedLayerArrivalSource()
    pump = LayerArrivalPump(source, sink)
    plan = make_plan({layer_id: 1 for layer_id in _SCHEDULE_ORDER})
    errors: list[BaseException] = []

    def run_pump() -> None:
        try:
            pump.run(plan)
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()

    _deliver_when_active(source, 0)

    def launched() -> list[int]:
        loads = list(launchers.by_generation.values())
        return loads[0].launched_layers() if loads else []

    _wait_for(lambda: launched() == [0], "layer 0 to launch")
    # Give the pump time to (wrongly) run ahead; it must still be waiting.
    time.sleep(0.05)
    assert launched() == [0]

    for layer_id in _SCHEDULE_ORDER[1:]:
        source.deliver_layer(layer_id)
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not thread.is_alive(), "pump thread did not finish"
    assert errors == []
    assert launched() == list(_SCHEDULE_ORDER)
    assert len(source.finished_generations()) == 1


def test_an_unservable_fetch_fails_the_load() -> None:
    """A declined layer surfaces as a failed load, never a hang (B4)."""
    sink, launchers = _make_sink()
    pump = LayerArrivalPump(UnservableLayerArrivalSource(), sink)
    plan = make_plan({layer_id: 1 for layer_id in _SCHEDULE_ORDER})

    with pytest.raises(LayerUnservableError):
        pump.run(plan)

    (launcher,) = launchers.by_generation.values()
    assert launcher.calls == ["begin", "failed"]


def test_a_plan_covering_part_of_the_schedule_completes_every_ordinal() -> None:
    """The pump passes plan.layer_ids(); a subset still readies every layer."""
    sink, launchers = _make_sink()
    source = ScriptedLayerArrivalSource()
    pump = LayerArrivalPump(source, sink)
    plan = make_plan({0: 1, 2: 1})
    errors: list[BaseException] = []

    def run_pump() -> None:
        try:
            pump.run(plan)
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()
    _deliver_when_active(source, 0)
    source.deliver_layer(2)
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not thread.is_alive()
    assert errors == []
    (launcher,) = launchers.by_generation.values()
    assert launcher.launched_layers() == [0, 1, 2, 3]
