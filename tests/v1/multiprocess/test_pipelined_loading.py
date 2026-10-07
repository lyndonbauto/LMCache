# SPDX-License-Identifier: Apache-2.0
"""Tests for :func:`fetch_deferred_objects`: a retrieve's deferred objects,
fetched layer by layer or loaded whole.

The placer, transport and sink are the layerwise test doubles and storage is
a fake, so no GPU, RDMA or L2 is needed. What is checked is what retrieve
relies on: which way the objects were delivered, what the object table holds
when the sink loads each layer, and which keys are left locked for retrieve
to release.
"""

# Standard
from collections.abc import Sequence
from typing import Any, cast
from unittest.mock import MagicMock
import time

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.layerwise import (
    LayerArrivalSource,
    LayerFetchPlan,
    LayerwiseContractError,
    RecordingLayerLoadSink,
    ScriptedLayerArrivalSource,
    UnservableLayerArrivalSource,
)
from lmcache.v1.layerwise.deferral import PipelinedFetchConfig, PipelinedModel
from lmcache.v1.layerwise.pump import DEFAULT_LAYER_TIMEOUT_SECONDS
from lmcache.v1.memory_management import MemoryObj
from lmcache.v1.multiprocess.pipelined_loading import (
    DeferredLoad,
    ObjectTable,
    PipelinedLoadRequest,
    PipelinedOutcome,
    PipelinedSink,
    fetch_deferred_objects,
)
from tests.v1.layerwise.placers import PackingPlacer
from tests.v1.layerwise.vllm_requests import (
    GROUP_LAYOUTS,
    MAX_RECORD_BYTES,
    fetch_model,
    resolve_obj_keys,
    vllm_request,
)

KEYS = resolve_obj_keys(vllm_request())
NUM_GROUPS = len(KEYS)
NUM_CHUNKS = len(KEYS[0])
# The last two chunks of each retrieved group were deferred; the rest are in
# L1. Group 2 is aux, which retrieve never reads.
RETRIEVED_GROUPS = (0, 1)
# Layer 4 is the aux group's, so the plan's last layer is 3.
LAST_RETRIEVED_LAYER = 3
DEFERRED = [(g, c) for g in RETRIEVED_GROUPS for c in range(NUM_CHUNKS - 2, NUM_CHUNKS)]
#: Distinct timeouts, so a test can tell which one a wait used.
CONFIG = PipelinedFetchConfig(
    enabled=True, layer_timeout_seconds=0.05, whole_load_timeout_seconds=0.75
)


class _Held:
    """Stands in for an object's memory; identified by its label."""

    def __init__(self, label: str) -> None:
        self.label = label


def _held(label: str) -> MemoryObj:
    return _Held(label)  # type: ignore[return-value]


class LandingSource(ScriptedLayerArrivalSource):
    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        generation = super().begin_fetch(plan)
        for slot_index in range(len(plan.slots)):
            self.land_slot(slot_index, generation)
        return generation


class TableReadingSink(RecordingLayerLoadSink):
    """Records, per loaded layer, the deferred objects the table held."""

    def __init__(self, table: ObjectTable, fail_on_layer: int = -1) -> None:
        super().__init__()
        self._table = table
        self._fail_on_layer = fail_on_layer
        self.seen: dict[int, list[object]] = {}
        self.waits = 0

    def load_layer(self, layer_id: int) -> None:
        if layer_id == self._fail_on_layer:
            raise RuntimeError("gpu copy failed")
        self.seen[layer_id] = [self._table.get(g, c) for g, c in DEFERRED]
        super().load_layer(layer_id)

    def wait_for_copies(self) -> None:
        self.waits += 1


class Factory:
    def __init__(self, fail_on_layer: int = -1) -> None:
        self.fail_on_layer = fail_on_layer
        self.sinks: list[TableReadingSink] = []

    def build(self, request: PipelinedLoadRequest) -> PipelinedSink:
        sink = TableReadingSink(request.objects, self.fail_on_layer)
        self.sinks.append(sink)
        return sink


class FakeStorage:
    def __init__(
        self,
        source: LayerArrivalSource | None = None,
        missing: Sequence[ObjectKey] = (),
    ) -> None:
        self.source = source
        self.missing = set(missing)
        self.loads: list[list[ObjectKey]] = []
        self.timeouts: list[float] = []
        self.released: list[ObjectKey] = []

    def layer_arrival_source(self) -> LayerArrivalSource:
        if self.source is None:
            raise LayerwiseContractError("no L2 adapter can fetch layer by layer")
        return self.source

    def load_into_l1(
        self,
        keys: list[ObjectKey],
        group_layout_descs: dict[int, MemoryLayoutDesc],
        timeout_seconds: float,
    ) -> dict[ObjectKey, MemoryObj]:
        self.loads.append(list(keys))
        self.timeouts.append(timeout_seconds)
        return {
            k: _held(f"whole:{k.chunk_hash!r}") for k in keys if k not in self.missing
        }

    def finish_read_prefetched(
        self, keys: list[ObjectKey], read_locks: int = 1
    ) -> None:
        self.released.extend(keys)


def _model(placer: PackingPlacer) -> PipelinedModel:
    return PipelinedModel(
        fetch_model=fetch_model(),
        placer=placer,
        max_record_bytes=MAX_RECORD_BYTES,
        max_slots=10_000,
        adapter_id=0,
        max_chunks=NUM_CHUNKS,
    )


def _table() -> ObjectTable:
    deferred = set(DEFERRED)
    return ObjectTable(
        [
            [
                None if (g, c) in deferred else _held(f"l1:{g}:{c}")
                for c in range(NUM_CHUNKS)
            ]
            for g in range(NUM_GROUPS)
        ]
    )


def _request(table: ObjectTable) -> PipelinedLoadRequest:
    mock: Any = MagicMock()
    return PipelinedLoadRequest(
        cache_context=mock,
        block_ids_gpu=[],
        objects=table,
        skip_first_n_tokens=0,
        schedule=mock,
        sequencer=mock,
        retrieve_generation=7,
        transfer_key="t",
    )


def _deferred_keys() -> list[ObjectKey]:
    return [KEYS[g][c] for g, c in DEFERRED]


def _fetch(
    storage: FakeStorage,
    placer: PackingPlacer | None = None,
    factory: Factory | None = None,
    table: ObjectTable | None = None,
    config: PipelinedFetchConfig = CONFIG,
):
    table = table if table is not None else _table()
    result = fetch_deferred_objects(
        storage,
        _model(placer if placer is not None else PackingPlacer()),
        KEYS,
        _deferred_keys(),
        factory if factory is not None else Factory(),
        _request(table),
        GROUP_LAYOUTS,
        config,
    )
    return result, table


def test_a_pipelined_fetch_loads_from_the_window_objects() -> None:
    placer = PackingPlacer()
    factory = Factory()
    storage = FakeStorage(LandingSource())

    result, table = _fetch(storage, placer, factory)

    assert result.load is DeferredLoad.PIPELINED
    assert result.outcome is PipelinedOutcome.PIPELINED
    assert result.locked_keys == ()
    (sink,) = factory.sinks
    assert sink.finished_generations() and sink.waits == 1
    (lease,) = placer.leases
    first_layer = min(sink.seen)
    addresses = [cast(MemoryObj, obj).meta.address for obj in sink.seen[first_layer]]
    assert addresses == [lease.locate(c, g).dest_offset for g, c in DEFERRED]
    assert table.get(0, 0).label == "l1:0:0"
    assert storage.loads == []


def test_only_the_deferred_objects_are_leased() -> None:
    placer = PackingPlacer()

    _fetch(FakeStorage(LandingSource()), placer)

    (request,) = placer.requests
    assert {o.key for o in request} == set(_deferred_keys())


def test_without_a_layerwise_source_the_objects_are_loaded_whole() -> None:
    storage = FakeStorage(source=None)
    factory = Factory()

    result, table = _fetch(storage, factory=factory)

    assert result.load is DeferredLoad.WHOLE
    assert result.outcome is PipelinedOutcome.NO_SOURCE
    assert set(result.locked_keys) == set(_deferred_keys())
    assert factory.sinks == []
    assert all(table.get(g, c).label.startswith("whole:") for g, c in DEFERRED)


def test_a_refused_lease_loads_the_objects_whole() -> None:
    placer = PackingPlacer()
    placer.busy = True
    storage = FakeStorage(LandingSource())

    result, _ = _fetch(storage, placer)

    assert result.load is DeferredLoad.WHOLE
    assert result.outcome is PipelinedOutcome.REFUSED
    (loaded,) = storage.loads
    assert set(loaded) == set(_deferred_keys())


@pytest.mark.parametrize("has_source", [True, False], ids=["source", "no-source"])
def test_keys_that_do_not_match_the_model_fail_before_any_load(
    has_source: bool,
) -> None:
    """Neither a fetch nor a whole load can place objects the model lacks."""
    placer = PackingPlacer()
    storage = FakeStorage(LandingSource() if has_source else None)
    one_group_short = KEYS[:-1]

    with pytest.raises(LayerwiseContractError, match="cannot place"):
        fetch_deferred_objects(
            storage,
            _model(placer),
            one_group_short,
            _deferred_keys(),
            Factory(),
            _request(_table()),
            GROUP_LAYOUTS,
            CONFIG,
        )

    assert placer.requests == []
    assert storage.loads == []
    assert storage.released == []


def test_a_transport_failure_swaps_in_whole_objects_for_the_rest() -> None:
    """Layers after the failure read the reloaded objects; retrieve unlocks them."""
    factory = Factory()
    storage = FakeStorage(UnservableLayerArrivalSource())

    result, table = _fetch(storage, factory=factory)

    assert result.load is DeferredLoad.PIPELINED
    assert result.outcome is PipelinedOutcome.FELL_BACK
    assert set(result.locked_keys) == set(_deferred_keys())
    (sink,) = factory.sinks
    assert sink.finished_generations() and not sink.abandoned_generations()
    for objects in sink.seen.values():
        assert all(cast(_Held, obj).label.startswith("whole:") for obj in objects)
    assert storage.released == []


def test_a_whole_load_that_misses_a_key_fails_and_unlocks_the_rest() -> None:
    missing = _deferred_keys()[0]
    storage = FakeStorage(source=None, missing=[missing])

    with pytest.raises(LayerwiseContractError, match="could not be loaded"):
        _fetch(storage)

    assert set(storage.released) == set(_deferred_keys()) - {missing}


def test_a_sink_failure_after_the_fallback_waits_then_unlocks() -> None:
    """The reloaded objects are released only once the copies are done."""
    factory = Factory(fail_on_layer=LAST_RETRIEVED_LAYER)
    storage = FakeStorage(UnservableLayerArrivalSource())

    with pytest.raises(RuntimeError, match="gpu copy failed"):
        _fetch(storage, factory=factory)

    (sink,) = factory.sinks
    assert sink.abandoned_generations()
    assert sink.waits >= 2
    assert set(storage.released) == set(_deferred_keys())


def test_a_sink_failure_in_the_window_leaves_nothing_locked() -> None:
    factory = Factory(fail_on_layer=min(fetch_model().layout.layer_ids()))
    storage = FakeStorage(LandingSource())

    with pytest.raises(RuntimeError, match="gpu copy failed"):
        _fetch(storage, factory=factory)

    assert storage.loads == storage.released == []


@pytest.mark.parametrize(
    "source", [None, UnservableLayerArrivalSource()], ids=["no_source", "fallback"]
)
def test_every_whole_load_is_bounded_by_the_whole_load_timeout(
    source: LayerArrivalSource | None,
) -> None:
    storage = FakeStorage(source)

    _fetch(storage)

    assert storage.timeouts == [CONFIG.whole_load_timeout_seconds]


def test_a_refused_lease_is_bounded_by_the_whole_load_timeout() -> None:
    placer = PackingPlacer()
    placer.busy = True
    storage = FakeStorage(LandingSource())

    _fetch(storage, placer)

    assert storage.timeouts == [CONFIG.whole_load_timeout_seconds]


def test_the_pump_falls_back_after_the_configured_layer_timeout() -> None:
    """A layer that never lands is given up on after the config's timeout.

    The source never lands anything, so the first layer is what times out.
    The default timeout is far longer than this config's, so finishing well
    within the default shows the config's value was the one used.
    """
    storage = FakeStorage(ScriptedLayerArrivalSource())
    start = time.monotonic()

    result, _ = _fetch(storage)

    elapsed = time.monotonic() - start
    assert result.outcome is PipelinedOutcome.FELL_BACK
    assert CONFIG.layer_timeout_seconds <= elapsed < DEFAULT_LAYER_TIMEOUT_SECONDS
    assert storage.timeouts == [CONFIG.whole_load_timeout_seconds]
