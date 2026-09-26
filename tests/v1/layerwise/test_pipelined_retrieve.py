# SPDX-License-Identifier: Apache-2.0
"""Tests for :func:`run_pipelined_retrieve`: lease, plan, pump, release.

Every way a retrieve can end is driven here with the real pump, the scripted
transport and the recording loader, and each is checked for two things the
caller relies on: which error it raises (so one handler can fall back), and
how the window lease was released (which decides whether the window is
quarantined).
"""

# Standard
from collections.abc import Sequence

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.layerwise import (
    LayerArrivalPump,
    LayerArrivalSource,
    LayerArrivalTimeoutError,
    LayerFetchPlan,
    LayerUnservableError,
    LayerwiseContractError,
    PlanTooLargeError,
    RecordingLayerLoadSink,
    ScriptedLayerArrivalSource,
    UnservableLayerArrivalSource,
    pipelined_retrieve,
)
from lmcache.v1.layerwise.pipelined_retrieve import run_pipelined_retrieve
from lmcache.v1.layerwise.pump import DEFAULT_LAYER_TIMEOUT_SECONDS
from lmcache.v1.layerwise.request_fetch import (
    ChunkLocation,
    LeaseOutcome,
    ObjectToPlace,
    RequestFetch,
)

# Local
from .placers import PackingLease, PackingPlacer
from .vllm_requests import MAX_RECORD_BYTES, fetch_model, resolve_obj_keys, vllm_request


class LandingSource(ScriptedLayerArrivalSource):
    """Lands every slot as soon as the fetch begins, in reverse order."""

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        generation = super().begin_fetch(plan)
        for slot_index in reversed(range(len(plan.slots))):
            self.land_slot(slot_index, generation)
        return generation


class RefusingSource(ScriptedLayerArrivalSource):
    """Refuses every plan when the fetch begins."""

    def __init__(self, error: LayerwiseContractError) -> None:
        super().__init__()
        self.error = error

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        raise self.error


class FailingSink(RecordingLayerLoadSink):
    """Fails the GPU copy of the second layer."""

    def load_layer(self, layer_id: int) -> None:
        if len(self.loaded_layers()) == 1:
            raise RuntimeError("gpu copy failed")
        super().load_layer(layer_id)


class OverlappingPlacer(PackingPlacer):
    """Places every object at offset 0, which the planner must refuse."""

    def lease(self, objects: Sequence[ObjectToPlace]) -> PackingLease:
        lease = super().lease(objects)
        for key, location in lease.locations.items():
            lease.locations[key] = ChunkLocation(location.node_name, 0)
        return lease


class CrashingLocateLease(PackingLease):
    """A lease whose window pool crashes while locating an object."""

    def locate(self, chunk_id: int, object_group_id: int) -> ChunkLocation:
        raise RuntimeError("window pool crashed")


class CrashingLocatePlacer(PackingPlacer):
    """Hands out leases that crash while locating."""

    def lease(self, objects: Sequence[ObjectToPlace]) -> PackingLease:
        lease = super().lease(objects)
        crashing = CrashingLocateLease(
            lease.window_bytes(), lease.locations, lease.window_start()
        )
        self.leases[-1] = crashing
        return crashing


class FailingReleaseLease(PackingLease):
    """A lease whose release always fails."""

    def release(self, outcome: LeaseOutcome) -> None:
        super().release(outcome)
        raise RuntimeError("window pool is broken")


class FailingReleasePlacer(PackingPlacer):
    """Hands out leases whose release fails."""

    def lease(self, objects: Sequence[ObjectToPlace]) -> PackingLease:
        lease = super().lease(objects)
        failing = FailingReleaseLease(
            lease.window_bytes(), lease.locations, lease.window_start()
        )
        self.leases[-1] = failing
        return failing


def _run(
    placer: PackingPlacer,
    source: LayerArrivalSource,
    sink: RecordingLayerLoadSink | None = None,
    keys: list[list[ObjectKey]] | None = None,
    layer_timeout_seconds: float = DEFAULT_LAYER_TIMEOUT_SECONDS,
) -> tuple[RecordingLayerLoadSink, RequestFetch]:
    sink = sink if sink is not None else RecordingLayerLoadSink()
    pump = LayerArrivalPump(
        source,
        sink,
        poll_interval_seconds=0.001,
        layer_timeout_seconds=layer_timeout_seconds,
    )
    fetch = run_pipelined_retrieve(
        fetch_model(),
        keys if keys is not None else resolve_obj_keys(vllm_request()),
        MAX_RECORD_BYTES,
        placer,
        pump,
    )
    return sink, fetch


def _only_outcome(placer: PackingPlacer) -> LeaseOutcome:
    (lease,) = placer.leases
    (outcome,) = lease.outcomes
    return outcome


def test_a_finished_retrieve_loads_every_layer_and_releases_finished() -> None:
    """Every planned layer reaches the loader in order; the window is reusable."""
    placer = PackingPlacer()
    source = LandingSource()

    sink, fetch = _run(placer, source)

    assert sink.loaded_layers() == fetch.plan.layer_ids()
    assert len(source.finished_generations()) == 1
    assert _only_outcome(placer) is LeaseOutcome.FINISHED


def test_the_placer_is_asked_for_exactly_the_objects_the_plan_covers() -> None:
    """The lease covers the request's objects, and only those are fetched."""
    placer = PackingPlacer()

    _, fetch = _run(placer, LandingSource())

    (request,) = placer.requests
    assert {(o.chunk_id, o.object_group_id) for o in request} == {
        (p.chunk_id, p.object_group_id) for p in fetch.request.placements
    }


def test_a_request_too_large_for_any_window_raises_plan_too_large() -> None:
    """The caller can split; nothing was leased, begun or loaded."""
    placer = PackingPlacer(window_bytes=4096)
    source = LandingSource()
    sink = RecordingLayerLoadSink()

    with pytest.raises(PlanTooLargeError):
        _run(placer, source, sink)

    assert placer.leases == []
    assert source.finished_generations() == source.abandoned_generations() == ()
    assert sink.loaded_layers() == ()


def test_no_free_window_raises_a_plain_contract_error() -> None:
    """Splitting would not help, so it is not PlanTooLargeError."""
    placer = PackingPlacer()
    placer.busy = True

    with pytest.raises(LayerwiseContractError) as caught:
        _run(placer, LandingSource())

    assert not isinstance(caught.value, PlanTooLargeError)
    assert placer.leases == []


def test_keys_that_do_not_match_the_model_fall_back_without_a_lease() -> None:
    """A layout mismatch is a contract error, and no window is taken for it."""
    placer = PackingPlacer()
    keys = resolve_obj_keys(vllm_request())[:2]

    with pytest.raises(LayerwiseContractError, match="cannot plan"):
        _run(placer, LandingSource(), keys=keys)

    assert placer.requests == []


def test_a_bad_placement_releases_the_window_as_never_fetched() -> None:
    """Planning refused the lease's layout before anything was issued."""
    placer = OverlappingPlacer()
    source = LandingSource()

    with pytest.raises(LayerwiseContractError, match="overlap"):
        _run(placer, source)

    assert _only_outcome(placer) is LeaseOutcome.NEVER_FETCHED
    assert source.finished_generations() == source.abandoned_generations() == ()


def test_a_lease_that_crashes_while_placing_is_still_released() -> None:
    """A non-planning error propagates unchanged; nothing was issued."""
    placer = CrashingLocatePlacer()
    source = LandingSource()

    with pytest.raises(RuntimeError, match="window pool crashed"):
        _run(placer, source)

    assert _only_outcome(placer) is LeaseOutcome.NEVER_FETCHED
    assert source.finished_generations() == source.abandoned_generations() == ()


def test_a_declined_layer_releases_the_window_as_abandoned() -> None:
    """The transport gave up mid-fetch; writes may still be on the wire."""
    placer = PackingPlacer()
    sink = RecordingLayerLoadSink()

    with pytest.raises(LayerUnservableError):
        _run(placer, UnservableLayerArrivalSource(), sink)

    assert _only_outcome(placer) is LeaseOutcome.ABANDONED
    assert len(sink.abandoned_generations()) == 1


def test_a_layer_that_never_arrives_releases_the_window_as_abandoned() -> None:
    """A timeout is an abandon: the missing slot may still land later."""
    placer = PackingPlacer()
    source = ScriptedLayerArrivalSource()

    with pytest.raises(LayerArrivalTimeoutError):
        _run(placer, source, layer_timeout_seconds=0.01)

    assert _only_outcome(placer) is LeaseOutcome.ABANDONED
    assert len(source.abandoned_generations()) == 1


def test_a_refusal_at_begin_fetch_releases_the_window_as_abandoned() -> None:
    """The transport may have issued part of the plan before refusing it.

    Quarantining a window that turned out to be untouched costs one fetch
    timeout; reusing one that still receives writes corrupts the next request.
    """
    placer = PackingPlacer()

    with pytest.raises(PlanTooLargeError):
        _run(placer, RefusingSource(PlanTooLargeError("receive queue too short")))

    assert _only_outcome(placer) is LeaseOutcome.ABANDONED


def test_a_loader_failure_propagates_unchanged_and_abandons_the_window() -> None:
    """Loader errors are not contract errors; the window is still quarantined."""
    placer = PackingPlacer()
    sink = FailingSink()

    with pytest.raises(RuntimeError, match="gpu copy failed"):
        _run(placer, LandingSource(), sink)

    assert _only_outcome(placer) is LeaseOutcome.ABANDONED
    assert len(sink.abandoned_generations()) == 1


def test_a_failing_release_does_not_mask_the_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller must see why the fetch failed, not why the release did.

    The module logger does not propagate, so the test spies on it directly.
    """
    logged: list[str] = []
    monkeypatch.setattr(
        pipelined_retrieve.logger,
        "exception",
        lambda msg, *args, **kwargs: logged.append(str(msg) % args),
    )
    placer = FailingReleasePlacer()

    with pytest.raises(LayerUnservableError):
        _run(placer, UnservableLayerArrivalSource())

    assert _only_outcome(placer) is LeaseOutcome.ABANDONED
    assert logged == ["Releasing a window lease as ABANDONED failed"]


def test_a_failing_release_after_success_is_raised() -> None:
    """With no earlier error to preserve, a broken window pool must surface."""
    placer = FailingReleasePlacer()

    with pytest.raises(RuntimeError, match="window pool is broken"):
        _run(placer, LandingSource())

    assert _only_outcome(placer) is LeaseOutcome.FINISHED


def test_each_retrieve_takes_and_returns_its_own_lease() -> None:
    """Back-to-back retrieves each release exactly once, as finished."""
    placer = PackingPlacer()
    for _ in range(3):
        _run(placer, LandingSource())

    assert [lease.outcomes for lease in placer.leases] == [[LeaseOutcome.FINISHED]] * 3


def test_every_refusal_is_one_handler_away_from_a_fallback() -> None:
    """Placer, planner and transport refusals all satisfy a single except."""
    scenarios: list[tuple[PackingPlacer, LayerArrivalSource]] = [
        (PackingPlacer(window_bytes=4096), LandingSource()),
        (OverlappingPlacer(), LandingSource()),
        (PackingPlacer(), UnservableLayerArrivalSource()),
        (PackingPlacer(), RefusingSource(LayerwiseContractError("unsupported"))),
    ]
    busy = PackingPlacer()
    busy.busy = True
    scenarios.append((busy, LandingSource()))

    for placer, source in scenarios:
        with pytest.raises(LayerwiseContractError):
            _run(placer, source)


def test_object_sizes_reach_the_placer_unrounded() -> None:
    """Alignment is the placer's business; it is told each object's true size."""
    placer = PackingPlacer()
    _run(placer, LandingSource())
    layout = fetch_model().layout

    (request,) = placer.requests
    assert all(
        o.object_bytes == layout.object_group_bytes(o.object_group_id) for o in request
    )
