# SPDX-License-Identifier: Apache-2.0
"""Tests for :func:`run_pipelined_retrieve`: lease, plan, pump, fall back, release.

Every way a retrieve can end is driven here with the real pump, the scripted
transport and the recording loader, and each is checked for what the caller
relies on: which error it raises (a refusal means nothing began, so it can
load whole objects), how the window lease was released (which decides
whether the window is quarantined), and that the window outlives every copy
out of it.
"""

# Standard
from collections.abc import Sequence

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.layerwise import (
    LayerArrivalSource,
    LayerFetchPlan,
    LayerLoadSink,
    LayerwiseContractError,
    PlanTooLargeError,
    RecordingLayerLoadSink,
    ScriptedLayerArrivalSource,
    UnservableLayerArrivalSource,
    pipelined_retrieve,
)
from lmcache.v1.layerwise.pipelined_retrieve import (
    PipelinedRetrieveRefused,
    PipelinedRetrieveResult,
    RetrieveCompletion,
    run_pipelined_retrieve,
)
from lmcache.v1.layerwise.pump import DEFAULT_LAYER_TIMEOUT_SECONDS
from lmcache.v1.layerwise.request_fetch import (
    ChunkLocation,
    LeaseOutcome,
    ObjectToPlace,
    WindowLease,
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


class FailsOnSecondLayerSource(ScriptedLayerArrivalSource):
    """Delivers the first layer, then declines the second."""

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        generation = super().begin_fetch(plan)
        first, second = plan.layer_ids()[:2]
        self.deliver_layer(first)
        self.decline_layer(second)
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


class FakeLoader:
    """A loader over a recording sink that logs what it was asked, and when.

    Each log entry notes the lease's outcomes at that moment, so a test can
    tell whether the window was still held.
    """

    def __init__(self, sink: RecordingLayerLoadSink | None = None) -> None:
        self.sink = sink if sink is not None else RecordingLayerLoadSink()
        self.lease: PackingLease | None = None
        self.log: list[tuple[str, tuple[LeaseOutcome, ...]]] = []
        self.reloaded: list[tuple[ObjectToPlace, ...]] = []
        self.fail_reload = False
        self.fail_wait = False
        self.fail_sink_for = False

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        if self.fail_sink_for:
            raise RuntimeError("cannot build the sink")
        assert isinstance(lease, PackingLease)
        self.lease = lease
        self._note("sink_for")
        return self.sink

    def wait_for_copies(self) -> None:
        self._note("wait")
        if self.fail_wait:
            raise RuntimeError("copy stream failed")

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        self._note("reload")
        if self.fail_reload:
            raise RuntimeError("record gone")
        self.reloaded.append(tuple(objects))

    def steps(self) -> list[str]:
        return [step for step, _ in self.log]

    def outcomes_at(self, step: str) -> tuple[LeaseOutcome, ...]:
        (outcomes,) = [outcomes for name, outcomes in self.log if name == step]
        return outcomes

    def _note(self, step: str) -> None:
        held = tuple(self.lease.outcomes) if self.lease is not None else ()
        self.log.append((step, held))


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
    loader: FakeLoader | None = None,
    keys: list[list[ObjectKey]] | None = None,
    layer_timeout_seconds: float = DEFAULT_LAYER_TIMEOUT_SECONDS,
) -> PipelinedRetrieveResult:
    return run_pipelined_retrieve(
        fetch_model(),
        keys if keys is not None else resolve_obj_keys(vllm_request()),
        MAX_RECORD_BYTES,
        placer,
        source,
        loader if loader is not None else FakeLoader(),
        layer_timeout_seconds=layer_timeout_seconds,
        poll_interval_seconds=0.001,
    )


def _only_outcome(placer: PackingPlacer) -> LeaseOutcome:
    (lease,) = placer.leases
    (outcome,) = lease.outcomes
    return outcome


def test_a_finished_retrieve_loads_every_layer_then_releases_finished() -> None:
    """Every layer reaches the loader in order; the copies end before release."""
    placer = PackingPlacer()
    source = LandingSource()
    loader = FakeLoader()

    result = _run(placer, source, loader)

    assert result.completion is RetrieveCompletion.PIPELINED
    assert loader.sink.loaded_layers() == result.fetch.plan.layer_ids()
    assert len(loader.sink.finished_generations()) == 1
    assert len(source.finished_generations()) == 1
    assert loader.steps() == ["sink_for", "wait"]
    assert loader.outcomes_at("wait") == ()
    assert _only_outcome(placer) is LeaseOutcome.FINISHED


def test_the_placer_is_asked_for_exactly_the_objects_the_plan_covers() -> None:
    """The lease covers the request's objects, and only those are fetched."""
    placer = PackingPlacer()

    result = _run(placer, LandingSource())

    (request,) = placer.requests
    assert {(o.chunk_id, o.object_group_id) for o in request} == {
        (p.chunk_id, p.object_group_id) for p in result.fetch.request.placements
    }


def test_a_request_too_large_for_any_window_is_refused() -> None:
    """Nothing was leased or begun; the cause says splitting would help."""
    placer = PackingPlacer(window_bytes=4096)
    source = LandingSource()
    loader = FakeLoader()

    with pytest.raises(PipelinedRetrieveRefused) as caught:
        _run(placer, source, loader)

    assert isinstance(caught.value.__cause__, PlanTooLargeError)
    assert placer.leases == []
    assert loader.steps() == []
    assert source.finished_generations() == source.abandoned_generations() == ()


def test_no_free_window_is_refused() -> None:
    placer = PackingPlacer()
    placer.busy = True

    with pytest.raises(PipelinedRetrieveRefused) as caught:
        _run(placer, LandingSource())

    assert not isinstance(caught.value.__cause__, PlanTooLargeError)
    assert placer.leases == []


def test_keys_that_do_not_match_the_model_are_refused_without_a_lease() -> None:
    placer = PackingPlacer()
    keys = resolve_obj_keys(vllm_request())[:2]

    with pytest.raises(PipelinedRetrieveRefused, match="cannot plan"):
        _run(placer, LandingSource(), keys=keys)

    assert placer.requests == []


def test_a_bad_placement_is_refused_and_the_window_released_never_fetched() -> None:
    placer = OverlappingPlacer()
    source = LandingSource()
    loader = FakeLoader()

    with pytest.raises(PipelinedRetrieveRefused, match="overlap"):
        _run(placer, source, loader)

    assert _only_outcome(placer) is LeaseOutcome.NEVER_FETCHED
    assert loader.steps() == []
    assert source.finished_generations() == source.abandoned_generations() == ()


def test_a_lease_that_crashes_while_placing_is_still_released() -> None:
    """A non-planning error propagates unchanged; nothing was issued."""
    placer = CrashingLocatePlacer()

    with pytest.raises(RuntimeError, match="window pool crashed"):
        _run(placer, LandingSource())

    assert _only_outcome(placer) is LeaseOutcome.NEVER_FETCHED


def test_a_loader_that_cannot_build_its_sink_releases_never_fetched() -> None:
    placer = PackingPlacer()
    source = LandingSource()
    loader = FakeLoader()
    loader.fail_sink_for = True

    with pytest.raises(RuntimeError, match="cannot build the sink"):
        _run(placer, source, loader)

    assert _only_outcome(placer) is LeaseOutcome.NEVER_FETCHED
    assert source.finished_generations() == source.abandoned_generations() == ()


@pytest.mark.parametrize(
    "error",
    [PlanTooLargeError("receive queue too short"), LayerwiseContractError("no")],
    ids=["too-large", "unsupported"],
)
def test_a_refusal_at_begin_fetch_is_refused_and_abandons_the_window(
    error: LayerwiseContractError,
) -> None:
    """No load began, so the caller may load whole objects.

    The transport may have issued part of the plan before refusing it, so the
    window is quarantined: that costs one fetch timeout, while reusing one
    that still receives writes corrupts the next request.
    """
    placer = PackingPlacer()
    loader = FakeLoader()

    with pytest.raises(PipelinedRetrieveRefused) as caught:
        _run(placer, RefusingSource(error), loader)

    assert caught.value.__cause__ is error
    assert _only_outcome(placer) is LeaseOutcome.ABANDONED
    assert loader.sink.abandoned_generations() == ()
    assert loader.sink.finished_generations() == ()


def test_a_transport_failure_continues_the_same_load_from_whole_objects() -> None:
    """Layers before the failure come from the window, the rest from L1.

    The worker's waiters see one load that finishes: no abandon, no new
    generation.
    """
    placer = PackingPlacer()
    source = FailsOnSecondLayerSource()
    loader = FakeLoader()

    result = _run(placer, source, loader)

    assert result.completion is RetrieveCompletion.FELL_BACK
    assert loader.sink.loaded_layers() == result.fetch.plan.layer_ids()
    assert len(loader.sink.finished_generations()) == 1
    assert loader.sink.abandoned_generations() == ()
    assert len(source.abandoned_generations()) == 1
    (request,) = placer.requests
    assert loader.reloaded == [request]


def test_the_fallback_waits_for_copies_then_releases_before_reloading() -> None:
    """Copies out of the window end first; the reload needs the keys free."""
    placer = PackingPlacer()
    loader = FakeLoader()

    _run(placer, FailsOnSecondLayerSource(), loader)

    assert loader.steps() == ["sink_for", "wait", "reload"]
    assert loader.outcomes_at("wait") == ()
    assert loader.outcomes_at("reload") == (LeaseOutcome.ABANDONED,)
    assert _only_outcome(placer) is LeaseOutcome.ABANDONED


def test_a_layer_that_never_arrives_falls_back_too() -> None:
    placer = PackingPlacer()
    loader = FakeLoader()

    result = _run(
        placer, ScriptedLayerArrivalSource(), loader, layer_timeout_seconds=0.01
    )

    assert result.completion is RetrieveCompletion.FELL_BACK
    assert loader.sink.loaded_layers() == result.fetch.plan.layer_ids()
    assert _only_outcome(placer) is LeaseOutcome.ABANDONED


def test_a_failed_reload_abandons_the_load() -> None:
    """The fallback could not get the objects; vLLM recomputes."""
    placer = PackingPlacer()
    loader = FakeLoader()
    loader.fail_reload = True

    with pytest.raises(RuntimeError, match="record gone"):
        _run(placer, UnservableLayerArrivalSource(), loader)

    assert len(loader.sink.abandoned_generations()) == 1
    assert loader.sink.finished_generations() == ()
    assert _only_outcome(placer) is LeaseOutcome.ABANDONED


def test_a_failed_wait_before_the_fallback_abandons_the_load() -> None:
    placer = PackingPlacer()
    loader = FakeLoader()
    loader.fail_wait = True

    with pytest.raises(RuntimeError, match="copy stream failed"):
        _run(placer, UnservableLayerArrivalSource(), loader)

    assert "reload" not in loader.steps()
    assert len(loader.sink.abandoned_generations()) == 1
    assert _only_outcome(placer) is LeaseOutcome.ABANDONED


def test_a_loader_failure_abandons_the_load_after_its_copies() -> None:
    """Loader errors propagate unchanged; the window is still quarantined."""
    placer = PackingPlacer()
    loader = FakeLoader(FailingSink())

    with pytest.raises(RuntimeError, match="gpu copy failed"):
        _run(placer, LandingSource(), loader)

    assert len(loader.sink.abandoned_generations()) == 1
    assert loader.outcomes_at("wait") == ()
    assert _only_outcome(placer) is LeaseOutcome.ABANDONED


def test_a_failed_wait_after_every_layer_abandons_the_window() -> None:
    """The copies may still read the window, so it is not reused."""
    placer = PackingPlacer()
    loader = FakeLoader()
    loader.fail_wait = True

    with pytest.raises(RuntimeError, match="copy stream failed"):
        _run(placer, LandingSource(), loader)

    assert _only_outcome(placer) is LeaseOutcome.ABANDONED


def test_a_failing_release_does_not_mask_the_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller must see why the load failed, not why the release did.

    The module logger does not propagate, so the test spies on it directly.
    """
    logged: list[str] = []
    monkeypatch.setattr(
        pipelined_retrieve.logger,
        "exception",
        lambda msg, *args, **kwargs: logged.append(str(msg) % args),
    )
    placer = FailingReleasePlacer()

    with pytest.raises(RuntimeError, match="gpu copy failed"):
        _run(placer, LandingSource(), FakeLoader(FailingSink()))

    assert _only_outcome(placer) is LeaseOutcome.ABANDONED
    assert logged == ["Releasing a window lease as ABANDONED failed"]


def test_a_failing_release_after_success_is_raised() -> None:
    """With no earlier error to preserve, a broken window pool must surface."""
    placer = FailingReleasePlacer()

    with pytest.raises(RuntimeError, match="window pool is broken"):
        _run(placer, LandingSource())

    assert _only_outcome(placer) is LeaseOutcome.FINISHED


def test_each_retrieve_takes_and_returns_its_own_lease() -> None:
    placer = PackingPlacer()
    for _ in range(3):
        _run(placer, LandingSource())

    assert [lease.outcomes for lease in placer.leases] == [[LeaseOutcome.FINISHED]] * 3


def test_every_refusal_is_one_handler_away_from_a_whole_load() -> None:
    """Placer, planner and begin_fetch refusals all satisfy a single except."""
    scenarios: list[tuple[PackingPlacer, LayerArrivalSource]] = [
        (PackingPlacer(window_bytes=4096), LandingSource()),
        (OverlappingPlacer(), LandingSource()),
        (PackingPlacer(), RefusingSource(LayerwiseContractError("unsupported"))),
    ]
    busy = PackingPlacer()
    busy.busy = True
    scenarios.append((busy, LandingSource()))

    for placer, source in scenarios:
        loader = FakeLoader()
        with pytest.raises(PipelinedRetrieveRefused):
            _run(placer, source, loader)
        assert loader.sink.abandoned_generations() == ()


def test_only_the_keys_to_fetch_are_leased_and_planned() -> None:
    """The deferred objects keep their chunk ids; the rest stay out of it."""
    keys = resolve_obj_keys(vllm_request())
    placer = PackingPlacer()
    everything = _run(placer, LandingSource(), keys=keys).fetch.request.placements
    later = {
        (p.chunk_id, p.object_group_id) for p in everything[len(everything) // 2 :]
    }
    deferred = {keys[group][chunk] for chunk, group in later}

    result = run_pipelined_retrieve(
        fetch_model(),
        keys,
        MAX_RECORD_BYTES,
        placer,
        LandingSource(),
        FakeLoader(),
        keys_to_fetch=deferred,
        poll_interval_seconds=0.001,
    )

    assert {o.key for o in placer.requests[-1]} == deferred
    placed = {(p.chunk_id, p.object_group_id) for p in result.fetch.request.placements}
    assert placed == later
    assert result.completion is RetrieveCompletion.PIPELINED


def test_object_sizes_reach_the_placer_unrounded() -> None:
    """Alignment is the placer's business; it is told each object's true size."""
    placer = PackingPlacer()
    _run(placer, LandingSource())
    layout = fetch_model().layout

    (request,) = placer.requests
    assert all(
        o.object_bytes == layout.object_group_bytes(o.object_group_id) for o in request
    )
