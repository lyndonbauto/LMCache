# SPDX-License-Identifier: Apache-2.0
"""Conformance suite for :class:`LayerArrivalSource` implementations.

Every test runs once per entry in ``SOURCE_HARNESS_FACTORIES``, using only
the contract's methods and an :class:`ArrivalDriver`. A new transport passes
this suite before it is wired into the retrieve path.
"""

# Standard
from collections.abc import Sequence
import threading

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    NO_GENERATION,
    LayerArrivalPump,
    LayerArrivalSource,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerNotInPlanError,
    LayerUnservableError,
    LayerwiseContractError,
    RecordingLayerLoadSink,
    StaleGenerationError,
)

# Local
from .conftest import SourceHarness, make_plan

_JOIN_TIMEOUT_SECONDS = 5.0

PENDING = LayerArrivalStatus.PENDING
RESIDENT = LayerArrivalStatus.RESIDENT
UNSERVABLE = LayerArrivalStatus.UNSERVABLE


def _plan() -> LayerFetchPlan:
    """Three layers; layers 0 and 2 need two slots, layer 1 needs one."""
    return make_plan({0: 2, 1: 1, 2: 2})


def _slots_of(plan: LayerFetchPlan, layer_id: int) -> list[int]:
    return [i for i, slot in enumerate(plan.slots) if slot.layer_id == layer_id]


def _statuses(
    source: LayerArrivalSource, plan: LayerFetchPlan, generation: int
) -> dict[int, LayerArrivalStatus]:
    return {
        layer_id: source.poll_layer(layer_id, generation)
        for layer_id in plan.layer_ids()
    }


class _GenerationTap:
    """Passes calls through to a source, publishing the generation it begins.

    The pump begins the fetch itself, so a test driving arrivals from another
    thread learns the generation from here.
    """

    def __init__(self, source: LayerArrivalSource) -> None:
        self._source = source
        self._begun = threading.Event()
        self._generation = NO_GENERATION

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        self._generation = self._source.begin_fetch(plan)
        self._begun.set()
        return self._generation

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        return self._source.poll_layer(layer_id, generation)

    def finish_fetch(self, generation: int) -> None:
        self._source.finish_fetch(generation)

    def abandon_fetch(self, generation: int) -> None:
        self._source.abandon_fetch(generation)

    def wait_for_generation(self) -> int:
        if not self._begun.wait(_JOIN_TIMEOUT_SECONDS):
            raise AssertionError("the pump never began a fetch")
        return self._generation


def _run_pump_while_driving(
    harness: SourceHarness,
    plan: LayerFetchPlan,
    drive: Sequence[tuple[str, int]],
) -> tuple[RecordingLayerLoadSink, list[BaseException]]:
    """Run a pump on a thread and apply ``drive`` once its fetch has begun.

    Args:
        harness: The source under test.
        plan: The plan the pump fetches.
        drive: ``("land" | "decline", slot_index)`` steps, applied in order.

    Returns:
        The sink the pump loaded into, and whatever the pump raised.
    """
    tap = _GenerationTap(harness.source)
    sink = RecordingLayerLoadSink()
    pump = LayerArrivalPump(tap, sink, poll_interval_seconds=0.001)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            pump.run(plan)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="conformance-pump")
    thread.start()
    generation = tap.wait_for_generation()
    for action, slot_index in drive:
        if action == "land":
            harness.driver.land_slot(slot_index, generation)
        else:
            harness.driver.decline_slot(slot_index, generation)
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive(), "pump thread did not finish"
    return sink, errors


def test_the_source_satisfies_the_protocol(source_harness: SourceHarness) -> None:
    """The implementation is a structural LayerArrivalSource."""
    assert isinstance(source_harness.source, LayerArrivalSource)


def test_each_fetch_gets_a_fresh_non_zero_generation(
    source_harness: SourceHarness,
) -> None:
    """A defaulted generation of 0 must never alias a real fetch."""
    source = source_harness.source
    first = source.begin_fetch(_plan())
    source.abandon_fetch(first)
    second = source.begin_fetch(_plan())

    assert NO_GENERATION not in (first, second)
    assert first != second


def test_a_second_fetch_is_refused_while_one_is_active(
    source_harness: SourceHarness,
) -> None:
    """A source owns at most one fetch at a time."""
    source = source_harness.source
    source.begin_fetch(_plan())

    with pytest.raises(LayerwiseContractError):
        source.begin_fetch(_plan())


def test_every_layer_starts_pending(source_harness: SourceHarness) -> None:
    """Nothing is resident before any slot lands."""
    plan = _plan()
    generation = source_harness.source.begin_fetch(plan)

    assert set(_statuses(source_harness.source, plan, generation).values()) == {PENDING}


def test_a_layer_stays_pending_until_its_last_slot_lands(
    source_harness: SourceHarness,
) -> None:
    """One slot of a two-slot layer is not the layer; nor does it move others."""
    plan = _plan()
    source, driver = source_harness.source, source_harness.driver
    generation = source.begin_fetch(plan)
    first, last = _slots_of(plan, 0)

    driver.land_slot(first, generation)
    assert _statuses(source, plan, generation) == {0: PENDING, 1: PENDING, 2: PENDING}

    driver.land_slot(last, generation)
    assert _statuses(source, plan, generation) == {0: RESIDENT, 1: PENDING, 2: PENDING}


def test_slots_landing_in_reverse_order_all_count(
    source_harness: SourceHarness,
) -> None:
    """Arrival order is not plan order; every landed slot is credited."""
    plan = _plan()
    source, driver = source_harness.source, source_harness.driver
    generation = source.begin_fetch(plan)

    for slot_index in reversed(range(len(plan.slots))):
        driver.land_slot(slot_index, generation)

    assert set(_statuses(source, plan, generation).values()) == {RESIDENT}


def test_a_declined_slot_makes_only_its_layer_unservable(
    source_harness: SourceHarness,
) -> None:
    """A decline is final for its layer, even if the layer's other slot lands."""
    plan = _plan()
    source, driver = source_harness.source, source_harness.driver
    generation = source.begin_fetch(plan)
    declined, other = _slots_of(plan, 2)

    driver.decline_slot(declined, generation)
    driver.land_slot(other, generation)
    for slot_index in _slots_of(plan, 0):
        driver.land_slot(slot_index, generation)

    assert _statuses(source, plan, generation) == {
        0: RESIDENT,
        1: PENDING,
        2: UNSERVABLE,
    }


def test_polls_quoting_another_generation_are_refused(
    source_harness: SourceHarness,
) -> None:
    """A caller on a stale or reserved generation has lost track of its fetch."""
    source = source_harness.source
    generation = source.begin_fetch(_plan())

    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, generation + 1)
    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, NO_GENERATION)


def test_a_poll_for_a_layer_outside_the_plan_is_refused(
    source_harness: SourceHarness,
) -> None:
    """An uncovered layer would otherwise read as forever pending."""
    source = source_harness.source
    generation = source.begin_fetch(_plan())

    with pytest.raises(LayerNotInPlanError):
        source.poll_layer(99, generation)


def test_late_arrivals_from_an_abandoned_fetch_are_not_credited(
    source_harness: SourceHarness,
) -> None:
    """Writes tagged with an old generation must not move the new fetch."""
    plan = _plan()
    source, driver = source_harness.source, source_harness.driver
    old = source.begin_fetch(plan)
    source.abandon_fetch(old)
    new = source.begin_fetch(plan)
    (layer_1_slot,) = _slots_of(plan, 1)
    layer_2_slot = _slots_of(plan, 2)[0]

    driver.land_slot(layer_1_slot, old)
    driver.decline_slot(layer_2_slot, old)
    assert _statuses(source, plan, new) == {0: PENDING, 1: PENDING, 2: PENDING}

    driver.land_slot(layer_1_slot, new)
    assert source.poll_layer(1, new) == RESIDENT


def test_finishing_ends_the_fetch_and_refuses_a_stale_generation(
    source_harness: SourceHarness,
) -> None:
    """Finish names the active fetch, and afterwards nothing does."""
    plan = make_plan({0: 1})
    source, driver = source_harness.source, source_harness.driver
    generation = source.begin_fetch(plan)
    driver.land_slot(0, generation)

    with pytest.raises(StaleGenerationError):
        source.finish_fetch(generation + 1)
    source.finish_fetch(generation)

    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, generation)
    source.abandon_fetch(source.begin_fetch(plan))


def test_abandon_tolerates_finished_repeated_and_unknown_generations(
    source_harness: SourceHarness,
) -> None:
    """Error paths unwind without knowing how far the fetch got."""
    plan = make_plan({0: 1})
    source, driver = source_harness.source, source_harness.driver
    finished = source.begin_fetch(plan)
    driver.land_slot(0, finished)
    source.finish_fetch(finished)

    source.abandon_fetch(finished)
    abandoned = source.begin_fetch(plan)
    source.abandon_fetch(abandoned)
    source.abandon_fetch(abandoned)
    source.abandon_fetch(NO_GENERATION)

    source.abandon_fetch(source.begin_fetch(plan))


def test_abandoning_a_stale_generation_leaves_the_active_fetch_alone(
    source_harness: SourceHarness,
) -> None:
    """Unwinding an old fetch must not cancel its successor."""
    plan = make_plan({0: 1})
    source, driver = source_harness.source, source_harness.driver
    old = source.begin_fetch(plan)
    source.abandon_fetch(old)
    active = source.begin_fetch(plan)

    source.abandon_fetch(old)
    driver.land_slot(0, active)

    assert source.poll_layer(0, active) == RESIDENT


def test_the_pump_loads_ascending_when_slots_land_backwards(
    source_harness: SourceHarness,
) -> None:
    """End to end through the pump: arrival order never reaches the loader."""
    plan = _plan()
    drive = [("land", i) for i in reversed(range(len(plan.slots)))]

    sink, errors = _run_pump_while_driving(source_harness, plan, drive)

    assert errors == []
    assert sink.loaded_layers() == plan.layer_ids()
    assert len(sink.finished_generations()) == 1


def test_the_pump_falls_back_when_a_slot_is_declined(
    source_harness: SourceHarness,
) -> None:
    """A declined slot surfaces as LayerUnservableError and abandons the load."""
    plan = _plan()
    drive = [("decline", _slots_of(plan, 0)[1])]

    sink, errors = _run_pump_while_driving(source_harness, plan, drive)

    assert len(errors) == 1
    assert isinstance(errors[0], LayerUnservableError)
    assert sink.finished_generations() == ()
    assert len(sink.abandoned_generations()) == 1
