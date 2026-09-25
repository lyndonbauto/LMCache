# SPDX-License-Identifier: Apache-2.0
"""Conformance suite for :class:`LayerLoadSink` implementations.

Every test runs once per entry in ``SINK_HARNESS_FACTORIES``, using only the
contract's methods and a :class:`LoadObserver`. A new loader passes this
suite before it is wired into the retrieve path.

What is checked is what the GPU worker relies on: a layer never looks ready
before its copy is issued, copies are issued in ascending layer order (and any
other order is refused at ``begin_load``), and an abandoned load fails its
waiters instead of leaving them hung.
"""

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    LayerArrivalPump,
    LayerFetchPlan,
    LayerLoadSink,
    LayerUnservableError,
    LayerWaitOutcome,
    LayerwiseContractError,
    LoadObserver,
    ScriptedLayerArrivalSource,
    StaleGenerationError,
    UnservableLayerArrivalSource,
)

# Local
from .conftest import SinkHarness, make_plan

READY = LayerWaitOutcome.READY
PENDING = LayerWaitOutcome.PENDING
FAILED = LayerWaitOutcome.FAILED

#: Loads are always ascending by global layer index, as plans are.
LAYERS = (0, 1, 2, 3)


class _LandingSource(ScriptedLayerArrivalSource):
    """Lands every slot as the fetch begins, last slot first."""

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        generation = super().begin_fetch(plan)
        for slot_index in reversed(range(len(plan.slots))):
            self.land_slot(slot_index, generation)
        return generation


def _outcomes(
    observer: LoadObserver, generation: int, layers: tuple[int, ...] = LAYERS
) -> dict[int, LayerWaitOutcome]:
    return {layer: observer.wait_outcome(layer, generation) for layer in layers}


def _begun(harness: SinkHarness, generation: int = 7) -> LayerLoadSink:
    harness.sink.begin_load(generation, LAYERS)
    return harness.sink


# -- the happy path -------------------------------------------------------


def test_the_sink_satisfies_the_protocol(sink_harness: SinkHarness) -> None:
    """Both halves of the harness are what the pump and the suite expect."""
    assert isinstance(sink_harness.sink, LayerLoadSink)
    assert isinstance(sink_harness.observer, LoadObserver)


def test_layers_become_ready_one_at_a_time_in_issue_order(
    sink_harness: SinkHarness,
) -> None:
    """Each copy makes exactly its own layer ready, and nothing after it."""
    sink = _begun(sink_harness)
    observer = sink_harness.observer

    assert _outcomes(observer, 7) == dict.fromkeys(LAYERS, PENDING)
    for issued, layer in enumerate(LAYERS, start=1):
        sink.load_layer(layer)
        assert observer.issued_layers(7) == LAYERS[:issued]
        assert _outcomes(observer, 7) == {
            other: READY if other in LAYERS[:issued] else PENDING for other in LAYERS
        }


def test_a_finished_load_leaves_every_layer_ready(sink_harness: SinkHarness) -> None:
    """Finishing does not revoke readiness a waiter may still be checking."""
    sink = _begun(sink_harness)
    for layer in LAYERS:
        sink.load_layer(layer)

    sink.finish_load(7)

    assert _outcomes(sink_harness.observer, 7) == dict.fromkeys(LAYERS, READY)


@pytest.mark.parametrize(
    "order",
    [(0, 2, 1, 3), (3, 2, 1, 0), (0, 1, 1, 2)],
    ids=["interleaved", "descending", "repeated"],
)
def test_a_load_whose_layers_are_not_ascending_is_refused(
    sink_harness: SinkHarness, order: tuple[int, ...]
) -> None:
    """Accepting it would let a schedule-position loader report layers early.

    Plans are always ascending, so refusing costs nothing; the sink stays
    free for a correct load.
    """
    with pytest.raises(LayerwiseContractError):
        sink_harness.sink.begin_load(7, order)

    assert sink_harness.observer.issued_layers(7) == ()
    sink_harness.sink.begin_load(8, LAYERS)
    for layer in LAYERS:
        sink_harness.sink.load_layer(layer)
    sink_harness.sink.finish_load(8)


def test_a_refused_order_leaves_the_active_load_alone(
    sink_harness: SinkHarness,
) -> None:
    """The refusal is checked without disturbing a load already running."""
    sink = _begun(sink_harness)
    sink.load_layer(0)

    with pytest.raises(LayerwiseContractError):
        sink.begin_load(8, (0, 2, 1, 3))

    for layer in LAYERS[1:]:
        sink.load_layer(layer)
    sink.finish_load(7)
    assert sink_harness.observer.issued_layers(7) == LAYERS


def test_a_load_with_gaps_in_its_layers_tracks_the_layers_it_was_given(
    sink_harness: SinkHarness,
) -> None:
    """Ascending with gaps is allowed; readiness follows layers, not positions."""
    layers = (0, 2, 3)
    sink_harness.sink.begin_load(7, layers)
    sink_harness.sink.load_layer(0)

    assert _outcomes(sink_harness.observer, 7, layers) == {
        0: READY,
        2: PENDING,
        3: PENDING,
    }


def test_a_generation_that_was_never_begun_is_pending(
    sink_harness: SinkHarness,
) -> None:
    """A worker that asks before the daemon begins must wait, not proceed."""
    assert sink_harness.observer.issued_layers(7) == ()
    assert _outcomes(sink_harness.observer, 7) == dict.fromkeys(LAYERS, PENDING)


# -- ordering rules -------------------------------------------------------


def test_a_layer_before_begin_is_refused(sink_harness: SinkHarness) -> None:
    """Nothing is copied for a load nobody announced."""
    with pytest.raises(LayerwiseContractError):
        sink_harness.sink.load_layer(0)


def test_a_second_begin_is_refused_and_the_active_load_survives(
    sink_harness: SinkHarness,
) -> None:
    """Overlapping loads would share the watermark; the second is refused."""
    sink = _begun(sink_harness)
    sink.load_layer(0)

    with pytest.raises(LayerwiseContractError):
        sink.begin_load(8, LAYERS)

    for layer in LAYERS[1:]:
        sink.load_layer(layer)
    sink.finish_load(7)
    assert sink_harness.observer.issued_layers(7) == LAYERS
    assert sink_harness.observer.issued_layers(8) == ()


def test_an_out_of_order_layer_is_refused_and_not_made_ready(
    sink_harness: SinkHarness,
) -> None:
    """On one stream, issuing layer 2 first would make it look ready early."""
    sink = _begun(sink_harness)

    with pytest.raises(LayerwiseContractError):
        sink.load_layer(2)

    assert sink_harness.observer.wait_outcome(2, 7) is PENDING
    sink.load_layer(0)
    assert sink_harness.observer.issued_layers(7) == (0,)


def test_a_layer_outside_the_load_is_refused(sink_harness: SinkHarness) -> None:
    """A layer the load never announced is a caller bug, not a no-op."""
    sink = _begun(sink_harness)

    with pytest.raises(LayerwiseContractError):
        sink.load_layer(99)

    assert sink_harness.observer.issued_layers(7) == ()


def test_a_repeated_layer_is_refused(sink_harness: SinkHarness) -> None:
    """Issuing layer 0 twice would skip layer 1's copy."""
    sink = _begun(sink_harness)
    sink.load_layer(0)

    with pytest.raises(LayerwiseContractError):
        sink.load_layer(0)

    assert sink_harness.observer.issued_layers(7) == (0,)


def test_a_layer_past_the_end_is_refused(sink_harness: SinkHarness) -> None:
    """Once every announced layer is issued, there is nothing left to copy."""
    sink = _begun(sink_harness)
    for layer in LAYERS:
        sink.load_layer(layer)

    with pytest.raises(LayerwiseContractError):
        sink.load_layer(LAYERS[-1])


# -- finishing ------------------------------------------------------------


def test_finishing_with_a_layer_never_issued_is_refused(
    sink_harness: SinkHarness,
) -> None:
    """A waiter on the missing layer would otherwise wait forever."""
    sink = _begun(sink_harness)
    for layer in LAYERS[:-1]:
        sink.load_layer(layer)

    with pytest.raises(LayerwiseContractError):
        sink.finish_load(7)

    assert sink_harness.observer.wait_outcome(LAYERS[-1], 7) is not READY


def test_finishing_the_wrong_generation_is_refused_and_the_load_survives(
    sink_harness: SinkHarness,
) -> None:
    """A stale finish must not complete, or disturb, the active load."""
    sink = _begun(sink_harness)
    for layer in LAYERS:
        sink.load_layer(layer)

    with pytest.raises(StaleGenerationError):
        sink.finish_load(6)

    sink.finish_load(7)
    assert _outcomes(sink_harness.observer, 7) == dict.fromkeys(LAYERS, READY)


def test_finishing_with_no_load_active_is_refused(sink_harness: SinkHarness) -> None:
    """There is nothing to complete."""
    with pytest.raises(StaleGenerationError):
        sink_harness.sink.finish_load(7)


def test_a_new_load_can_begin_after_a_finish(sink_harness: SinkHarness) -> None:
    """Generations are independent: the new one starts with nothing ready."""
    sink = _begun(sink_harness)
    for layer in LAYERS:
        sink.load_layer(layer)
    sink.finish_load(7)

    sink.begin_load(8, LAYERS)

    assert _outcomes(sink_harness.observer, 8) == dict.fromkeys(LAYERS, PENDING)
    assert _outcomes(sink_harness.observer, 7) == dict.fromkeys(LAYERS, READY)


# -- abandoning -----------------------------------------------------------


def test_an_abandon_fails_every_layer_not_yet_issued(
    sink_harness: SinkHarness,
) -> None:
    """Waiters on the rest of the load are woken with a failure, not hung."""
    sink = _begun(sink_harness)
    sink.load_layer(0)
    sink.load_layer(1)

    sink.abandon_load(7)

    assert sink_harness.observer.wait_outcome(2, 7) is FAILED
    assert sink_harness.observer.wait_outcome(3, 7) is FAILED


def test_an_abandon_before_any_copy_fails_every_layer(
    sink_harness: SinkHarness,
) -> None:
    """The transport can give up before the first layer lands."""
    sink = _begun(sink_harness)

    sink.abandon_load(7)

    assert _outcomes(sink_harness.observer, 7) == dict.fromkeys(LAYERS, FAILED)


def test_an_abandon_with_no_load_active_is_safe(sink_harness: SinkHarness) -> None:
    """The pump abandons on every error path, including before begin_load."""
    sink_harness.sink.abandon_load(7)
    sink_harness.sink.abandon_load(7)

    sink_harness.sink.begin_load(8, LAYERS)
    assert _outcomes(sink_harness.observer, 8) == dict.fromkeys(LAYERS, PENDING)


def test_a_new_load_can_begin_after_an_abandon(sink_harness: SinkHarness) -> None:
    """An abandon releases the sink for the fallback or the next request."""
    sink = _begun(sink_harness)
    sink.load_layer(0)
    sink.abandon_load(7)

    sink.begin_load(8, LAYERS)
    for layer in LAYERS:
        sink.load_layer(layer)
    sink.finish_load(8)

    assert _outcomes(sink_harness.observer, 8) == dict.fromkeys(LAYERS, READY)


def test_an_abandon_after_finishing_does_not_revoke_readiness(
    sink_harness: SinkHarness,
) -> None:
    """A late abandon for a completed load must not fail its waiters."""
    sink = _begun(sink_harness)
    for layer in LAYERS:
        sink.load_layer(layer)
    sink.finish_load(7)

    sink.abandon_load(7)

    assert _outcomes(sink_harness.observer, 7) == dict.fromkeys(LAYERS, READY)


def test_a_stale_abandon_leaves_the_active_load_alone(
    sink_harness: SinkHarness,
) -> None:
    """Abandoning an old generation must not fail the current one's waiters."""
    sink = _begun(sink_harness)
    for layer in LAYERS:
        sink.load_layer(layer)
    sink.finish_load(7)
    sink.begin_load(8, LAYERS)
    sink.load_layer(0)

    sink.abandon_load(7)

    assert _outcomes(sink_harness.observer, 8) == {
        0: READY,
        1: PENDING,
        2: PENDING,
        3: PENDING,
    }
    for layer in LAYERS[1:]:
        sink.load_layer(layer)
    sink.finish_load(8)


# -- driven by the pump ---------------------------------------------------


def test_the_pump_issues_every_layer_in_plan_order(sink_harness: SinkHarness) -> None:
    """Arrivals in reverse order still reach the sink in ascending order."""
    plan = make_plan({layer: 2 for layer in LAYERS})
    pump = LayerArrivalPump(_LandingSource(), sink_harness.sink)

    generation = pump.run(plan)

    assert sink_harness.observer.issued_layers(generation) == plan.layer_ids()
    assert _outcomes(sink_harness.observer, generation) == dict.fromkeys(LAYERS, READY)


def test_a_pump_failure_leaves_no_waiter_hanging(sink_harness: SinkHarness) -> None:
    """When the transport declines, every waiter of that load fails."""
    plan = make_plan({layer: 1 for layer in LAYERS})
    pump = LayerArrivalPump(UnservableLayerArrivalSource(), sink_harness.sink)

    with pytest.raises(LayerUnservableError):
        pump.run(plan)

    # UnservableLayerArrivalSource always begins generation 1.
    assert _outcomes(sink_harness.observer, 1) == dict.fromkeys(LAYERS, FAILED)
