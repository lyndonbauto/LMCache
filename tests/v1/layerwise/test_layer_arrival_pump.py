# SPDX-License-Identifier: Apache-2.0
"""Tests for :class:`LayerArrivalPump`."""

# Standard
import threading
import time

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    LayerArrivalPump,
    LayerArrivalStatus,
    LayerArrivalTimeoutError,
    LayerFetchPlan,
    LayerUnservableError,
    LayerwiseContractError,
    LoadLeftOpenError,
    PlanTooLargeError,
    RecordingLayerLoadSink,
    ScriptedLayerArrivalSource,
    UnservableLayerArrivalSource,
)

# Local
from .conftest import make_plan

_PUMP_JOIN_TIMEOUT_SECONDS = 5.0


def _wait_until_fetch_active(
    source: ScriptedLayerArrivalSource, plan_layer: int
) -> None:
    """Deliver one layer once begin_fetch has started."""
    deadline = time.monotonic() + _PUMP_JOIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            source.deliver_layer(plan_layer)
            return
        except LayerwiseContractError:
            time.sleep(0.001)
    raise AssertionError("pump never started a fetch")


def test_pump_loads_layers_in_plan_order_when_arrivals_are_out_of_order() -> None:
    """The sink must see ascending layers even if the transport lands them backwards."""
    plan = make_plan({0: 1, 1: 1, 2: 1})
    layer_ids = plan.layer_ids()
    source = ScriptedLayerArrivalSource()
    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(source, sink)
    errors: list[BaseException] = []

    def run_pump() -> None:
        try:
            pump.run(plan)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()
    _wait_until_fetch_active(source, layer_ids[-1])
    for layer_id in reversed(layer_ids[:-1]):
        source.deliver_layer(layer_id)
    source.deliver_layer(layer_ids[0])
    thread.join(timeout=_PUMP_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive(), "pump thread did not finish"
    assert errors == []
    assert sink.loaded_layers() == layer_ids


def test_pump_success_finishes_both_sides_once_and_returns_generation() -> None:
    """A completed run must finish exactly once on source and sink, with no abandons."""
    plan = make_plan({3: 1})
    source = ScriptedLayerArrivalSource()
    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(source, sink)
    errors: list[BaseException] = []
    generations: list[int] = []

    def run_pump() -> None:
        try:
            generations.append(pump.run(plan))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()
    _wait_until_fetch_active(source, 3)
    thread.join(timeout=_PUMP_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive()
    assert errors == []
    assert len(generations) == 1
    generation = generations[0]
    assert generation != 0
    assert source.finished_generations() == (generation,)
    assert source.abandoned_generations() == ()
    assert sink.finished_generations() == (generation,)
    assert sink.abandoned_generations() == ()


def test_pump_abandons_both_sides_when_scripted_layer_is_declined() -> None:
    """A declined layer triggers fallback and abandons transport and loader."""
    plan = make_plan({0: 1, 1: 1})
    source = ScriptedLayerArrivalSource()
    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(source, sink)
    errors: list[BaseException] = []

    def run_pump() -> None:
        try:
            pump.run(plan)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()
    _wait_until_fetch_active(source, 1)
    source.decline_layer(0)
    thread.join(timeout=_PUMP_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LayerUnservableError)
    generation = 1
    assert source.abandoned_generations() == (generation,)
    assert source.finished_generations() == ()
    assert sink.abandoned_generations() == (generation,)
    assert sink.finished_generations() == ()


def test_pump_abandons_both_sides_when_transport_declines_every_layer() -> None:
    """UnservableLayerArrivalSource forces the real unservable fallback path."""
    plan = make_plan({0: 1})
    source = UnservableLayerArrivalSource()
    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(source, sink)

    with pytest.raises(LayerUnservableError):
        pump.run(plan)

    assert sink.abandoned_generations() == (1,)
    assert sink.finished_generations() == ()


def test_pump_times_out_when_a_layer_never_arrives() -> None:
    """A stuck layer raises LayerArrivalTimeoutError and abandons both sides."""
    plan = make_plan({0: 1})
    source = ScriptedLayerArrivalSource()
    sink = RecordingLayerLoadSink()
    clock_readings = [0.0, 5.0, 11.0]
    sleep_calls: list[float] = []

    def clock() -> float:
        if clock_readings:
            return clock_readings.pop(0)
        return 11.0

    def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    pump = LayerArrivalPump(
        source,
        sink,
        layer_timeout_seconds=10.0,
        poll_interval_seconds=0.01,
        clock=clock,
        sleep=sleep,
    )

    with pytest.raises(LayerArrivalTimeoutError):
        pump.run(plan)

    assert len(sleep_calls) == 1
    assert source.abandoned_generations() == (1,)
    assert sink.abandoned_generations() == (1,)


def test_pump_accepts_resident_layer_on_first_poll_after_deadline() -> None:
    """An already-landed layer is never rejected for lateness.

    The pump checks the deadline after polling, so a layer that is resident on
    the first poll succeeds even if the deadline has already passed.
    """
    plan = make_plan({0: 1})
    source = ScriptedLayerArrivalSource()
    sink = RecordingLayerLoadSink()

    def clock() -> float:
        return 1_000_000.0

    pump = LayerArrivalPump(
        source,
        sink,
        layer_timeout_seconds=1.0,
        clock=clock,
        sleep=lambda _seconds: None,
    )
    errors: list[BaseException] = []

    def run_pump() -> None:
        try:
            pump.run(plan)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()
    _wait_until_fetch_active(source, 0)
    thread.join(timeout=_PUMP_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive()
    assert errors == []
    assert sink.loaded_layers() == (0,)


def test_pump_abandons_both_sides_and_reraises_when_sink_fails_mid_load() -> None:
    """Loader failures propagate unchanged while both halves are abandoned."""
    copy_failure = RuntimeError("gpu copy failed")

    class FailingSink(RecordingLayerLoadSink):
        """Sink that fails on a chosen layer."""

        def load_layer(self, layer_id: int) -> None:
            if layer_id == 1:
                raise copy_failure
            super().load_layer(layer_id)

    plan = make_plan({0: 1, 1: 1, 2: 1})
    source = ScriptedLayerArrivalSource()
    sink = FailingSink()
    pump = LayerArrivalPump(source, sink)
    errors: list[BaseException] = []

    def run_pump() -> None:
        try:
            pump.run(plan)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()
    _wait_until_fetch_active(source, plan.layer_ids()[-1])
    for layer_id in plan.layer_ids():
        source.deliver_layer(layer_id)
    thread.join(timeout=_PUMP_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert errors[0] is copy_failure
    generation = 1
    assert source.abandoned_generations() == (generation,)
    assert sink.abandoned_generations() == (generation,)


def test_pump_passes_a_too_large_refusal_through_before_touching_the_sink() -> None:
    """The caller must see PlanTooLargeError itself, so it can split the request.

    Nothing was issued, so there is no load to abandon; and a caller that only
    knows how to fall back still catches it as a contract error.
    """
    refusal = PlanTooLargeError("plan exceeds the receive queue")

    class RefusingSource(ScriptedLayerArrivalSource):
        """Source that refuses every plan as too large."""

        def begin_fetch(self, plan: LayerFetchPlan) -> int:
            raise refusal

    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(RefusingSource(), sink)

    with pytest.raises(LayerwiseContractError) as caught:
        pump.run(make_plan({0: 1}))

    assert caught.value is refusal
    assert sink.abandoned_generations() == ()
    assert sink.finished_generations() == ()


def test_pump_constructor_rejects_negative_poll_interval() -> None:
    """Negative poll gaps make the wait loop meaningless."""
    source = ScriptedLayerArrivalSource()
    sink = RecordingLayerLoadSink()
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        LayerArrivalPump(source, sink, poll_interval_seconds=-0.1)


def test_pump_constructor_rejects_non_positive_layer_timeout() -> None:
    """A zero or negative per-layer timeout cannot bound waiting."""
    source = ScriptedLayerArrivalSource()
    sink = RecordingLayerLoadSink()
    with pytest.raises(ValueError, match="layer_timeout_seconds"):
        LayerArrivalPump(source, sink, layer_timeout_seconds=0.0)
    with pytest.raises(ValueError, match="layer_timeout_seconds"):
        LayerArrivalPump(source, sink, layer_timeout_seconds=-1.0)


# -- run_resumable: the load stays open when the transport fails ------------


class _FirstLayersSource:
    """Serves ``served`` layers at once and reports every other one ``failing``."""

    def __init__(self, served: set[int], failing: LayerArrivalStatus) -> None:
        self._served = served
        self._failing = failing
        self.finished: list[int] = []
        self.abandoned: list[int] = []

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        return 1

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        if layer_id in self._served:
            return LayerArrivalStatus.RESIDENT
        return self._failing

    def finish_fetch(self, generation: int) -> None:
        self.finished.append(generation)

    def abandon_fetch(self, generation: int) -> None:
        self.abandoned.append(generation)


class _FailingLoadSink(RecordingLayerLoadSink):
    """Fails to copy one layer."""

    def __init__(self, failing_layer: int) -> None:
        super().__init__()
        self._failing_layer = failing_layer

    def load_layer(self, layer_id: int) -> None:
        if layer_id == self._failing_layer:
            raise LayerwiseContractError("copy failed")
        super().load_layer(layer_id)


def test_a_declined_layer_leaves_the_load_open_for_the_caller() -> None:
    """Only the transport is abandoned; the error says what is left to load."""
    source = _FirstLayersSource({0}, LayerArrivalStatus.UNSERVABLE)
    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(source, sink)

    with pytest.raises(LoadLeftOpenError) as caught:
        pump.run_resumable(make_plan({0: 1, 1: 1, 2: 1}))

    error = caught.value
    assert error.generation == 1
    assert error.remaining_layers == (1, 2)
    assert isinstance(error.transport_error, LayerUnservableError)
    assert error.__cause__ is error.transport_error
    assert source.abandoned == [1] and source.finished == []
    assert sink.loaded_layers() == (0,)
    assert sink.abandoned_generations() == sink.finished_generations() == ()


def test_the_caller_can_finish_a_load_left_open() -> None:
    """The fallback continues the same generation, so the load completes."""
    source = _FirstLayersSource({0}, LayerArrivalStatus.UNSERVABLE)
    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(source, sink)

    with pytest.raises(LoadLeftOpenError) as caught:
        pump.run_resumable(make_plan({0: 1, 1: 1, 2: 1}))
    for layer_id in caught.value.remaining_layers:
        sink.load_layer(layer_id)
    sink.finish_load(caught.value.generation)

    assert sink.loaded_layers() == (0, 1, 2)
    assert sink.finished_generations() == (1,)
    assert sink.abandoned_generations() == ()


def test_a_timed_out_layer_leaves_the_load_open() -> None:
    """A late layer is a transport failure too."""
    source = _FirstLayersSource(set(), LayerArrivalStatus.PENDING)
    sink = RecordingLayerLoadSink()
    readings = iter([0.0, 11.0])
    pump = LayerArrivalPump(
        source,
        sink,
        layer_timeout_seconds=10.0,
        clock=lambda: next(readings),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(LoadLeftOpenError) as caught:
        pump.run_resumable(make_plan({0: 1, 1: 1}))

    assert caught.value.remaining_layers == (0, 1)
    assert isinstance(caught.value.transport_error, LayerArrivalTimeoutError)
    assert source.abandoned == [1]
    assert sink.abandoned_generations() == ()


def test_a_loader_failure_still_abandons_both_sides() -> None:
    """Nothing can continue a load the loader itself failed."""
    source = _FirstLayersSource({0, 1}, LayerArrivalStatus.UNSERVABLE)
    sink = _FailingLoadSink(failing_layer=1)
    pump = LayerArrivalPump(source, sink)

    with pytest.raises(LayerwiseContractError, match="copy failed") as caught:
        pump.run_resumable(make_plan({0: 1, 1: 1}))

    assert not isinstance(caught.value, LoadLeftOpenError)
    assert source.abandoned == [1]
    assert sink.abandoned_generations() == (1,)
