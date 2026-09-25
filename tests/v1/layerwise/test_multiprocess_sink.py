# SPDX-License-Identifier: Apache-2.0
"""Behaviour of Track B's :class:`MultiprocessLayerLoadSink`.

Tests the sink against its contract on CPU, with a recording launcher standing
in for the GPU side. The GPU side itself is covered by
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
    LayerNotInPlanError,
    LayerUnservableError,
    LayerwiseContractError,
    ScriptedLayerArrivalSource,
    StaleGenerationError,
    UnservableLayerArrivalSource,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.layerwise_sink import MultiprocessLayerLoadSink

# Local
from .conftest import RecordingLauncher, make_plan

_JOIN_TIMEOUT_SECONDS = 5.0

#: Hybrid layout: groups hold [0, 2] and [1, 3]; schedule order is 0, 1, 2, 3.
_HYBRID_GROUPS = [[0, 2], [1, 3]]
_SCHEDULE_ORDER = [0, 1, 2, 3]


def _make_sink() -> tuple[MultiprocessLayerLoadSink, RecordingLauncher]:
    """Build a sink over the hybrid schedule and its recording launcher."""
    launcher = RecordingLauncher()
    sink = MultiprocessLayerLoadSink(LayerwiseSchedule(_HYBRID_GROUPS), launcher)
    return sink, launcher


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
# Direct contract behaviour
# ---------------------------------------------------------------------------


def test_a_full_load_launches_every_layer_in_schedule_order() -> None:
    """A clean load runs setup once, then one launch per layer, in order."""
    sink, launcher = _make_sink()

    sink.begin_load(5, _SCHEDULE_ORDER)
    for layer_id in _SCHEDULE_ORDER:
        sink.load_layer(layer_id)
    sink.finish_load(5)

    assert launcher.calls == ["begin"] + [("launch", i) for i in _SCHEDULE_ORDER]


def test_begin_load_rejects_an_order_that_differs_from_the_schedule() -> None:
    """Group order (0, 2, 1, 3) is not schedule order and is refused."""
    sink, launcher = _make_sink()

    with pytest.raises(LayerwiseContractError, match="does not match"):
        sink.begin_load(1, [0, 2, 1, 3])
    assert launcher.calls == []


def test_begin_load_rejects_a_plan_missing_a_scheduled_layer() -> None:
    """Skipping layer 3 would leave its worker wait unsatisfiable."""
    sink, launcher = _make_sink()

    with pytest.raises(LayerwiseContractError, match="does not match"):
        sink.begin_load(1, [0, 1, 2])
    assert launcher.calls == []


def test_begin_load_rejects_the_reserved_generation() -> None:
    """Generation 0 means "no fetch" and must fail loudly."""
    sink, launcher = _make_sink()

    with pytest.raises(LayerwiseContractError, match="reserved"):
        sink.begin_load(0, _SCHEDULE_ORDER)
    assert launcher.calls == []


def test_a_second_begin_load_while_loading_is_rejected() -> None:
    """Only one load may be active."""
    sink, launcher = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)

    with pytest.raises(LayerwiseContractError, match="loading"):
        sink.begin_load(2, _SCHEDULE_ORDER)
    assert launcher.calls == ["begin"]


def test_the_sink_is_single_use() -> None:
    """A finished sink refuses a new load rather than republishing its retrieve."""
    sink, _ = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)
    for layer_id in _SCHEDULE_ORDER:
        sink.load_layer(layer_id)
    sink.finish_load(1)

    with pytest.raises(LayerwiseContractError, match="finished"):
        sink.begin_load(2, _SCHEDULE_ORDER)


def test_load_layer_before_begin_load_is_rejected() -> None:
    """Nothing is copied without an active load."""
    sink, launcher = _make_sink()

    with pytest.raises(LayerwiseContractError, match="unused"):
        sink.load_layer(0)
    assert launcher.calls == []


def test_an_unknown_layer_raises_layer_not_in_plan() -> None:
    """A layer the load does not cover is a plan error."""
    sink, launcher = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)

    with pytest.raises(LayerNotInPlanError):
        sink.load_layer(9)
    assert launcher.launched_layers() == []


def test_an_out_of_order_layer_is_rejected_without_copying() -> None:
    """Issuing layer 1 before 0 is an ordering error, not a plan error."""
    sink, launcher = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)

    with pytest.raises(LayerwiseContractError, match="expected layer 0") as info:
        sink.load_layer(1)
    assert not isinstance(info.value, LayerNotInPlanError)
    assert launcher.launched_layers() == []


def test_finish_load_rejects_a_stale_generation() -> None:
    """Finishing under the wrong generation means the caller lost track."""
    sink, _ = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)
    for layer_id in _SCHEDULE_ORDER:
        sink.load_layer(layer_id)

    with pytest.raises(StaleGenerationError):
        sink.finish_load(2)


def test_finish_load_rejects_unissued_layers() -> None:
    """A consumer would wait forever on a layer nobody copied."""
    sink, _ = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)
    sink.load_layer(0)

    with pytest.raises(LayerwiseContractError, match=r"\[1, 2, 3\] never issued"):
        sink.finish_load(1)


def test_abandon_fails_the_retrieve() -> None:
    """Abandoning a load marks the retrieve failed so waiters wake."""
    sink, launcher = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)
    sink.load_layer(0)

    sink.abandon_load(1)

    assert launcher.calls == ["begin", ("launch", 0), "failed"]


def test_abandon_after_a_refused_begin_still_fails_the_retrieve() -> None:
    """Waiters on this retrieve must wake even if begin_load never succeeded."""
    sink, launcher = _make_sink()
    with pytest.raises(LayerwiseContractError):
        sink.begin_load(1, [0, 1])

    sink.abandon_load(1)

    assert launcher.calls == ["failed"]


def test_abandon_after_a_clean_finish_does_nothing() -> None:
    """A finished load's data landed; abandoning it must not poison it."""
    sink, launcher = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)
    for layer_id in _SCHEDULE_ORDER:
        sink.load_layer(layer_id)
    sink.finish_load(1)

    sink.abandon_load(1)

    assert "failed" not in launcher.calls


def test_abandon_is_idempotent() -> None:
    """Error paths can unwind without tracking whether they already abandoned."""
    sink, launcher = _make_sink()
    sink.begin_load(1, _SCHEDULE_ORDER)

    sink.abandon_load(1)
    sink.abandon_load(1)

    assert launcher.calls.count("failed") == 1


# ---------------------------------------------------------------------------
# Driven by the real pump
# ---------------------------------------------------------------------------


def test_the_pump_copies_each_layer_only_after_it_arrives() -> None:
    """Layer 0 is copied while later layers have not arrived (B2 on CPU)."""
    sink, launcher = _make_sink()
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
    _wait_for(lambda: launcher.launched_layers() == [0], "layer 0 to launch")
    # Give the pump time to (wrongly) run ahead; it must still be waiting.
    time.sleep(0.05)
    assert launcher.launched_layers() == [0]

    for layer_id in _SCHEDULE_ORDER[1:]:
        source.deliver_layer(layer_id)
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not thread.is_alive(), "pump thread did not finish"
    assert errors == []
    assert launcher.launched_layers() == _SCHEDULE_ORDER
    assert "failed" not in launcher.calls
    assert len(source.finished_generations()) == 1


def test_an_unservable_fetch_fails_the_retrieve() -> None:
    """A declined layer surfaces as a failed retrieve, never a hang (B4)."""
    sink, launcher = _make_sink()
    pump = LayerArrivalPump(UnservableLayerArrivalSource(), sink)
    plan = make_plan({layer_id: 1 for layer_id in _SCHEDULE_ORDER})

    with pytest.raises(LayerUnservableError):
        pump.run(plan)

    assert launcher.calls == ["begin", "failed"]


def test_a_plan_that_does_not_match_the_schedule_fails_the_retrieve() -> None:
    """A mismatched plan is refused, and the pump's abandon still wakes waiters."""
    sink, launcher = _make_sink()
    source = ScriptedLayerArrivalSource()
    pump = LayerArrivalPump(source, sink)
    plan = make_plan({0: 1, 1: 1})

    with pytest.raises(LayerwiseContractError, match="does not match"):
        pump.run(plan)

    assert launcher.calls == ["failed"]
    assert len(source.abandoned_generations()) == 1
