# SPDX-License-Identifier: Apache-2.0
"""Shared builders, and source and sink fixtures, for layerwise contract tests."""

# Standard
from collections.abc import Callable
from dataclasses import dataclass

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    ArrivalDriver,
    LayerArrivalSource,
    LayerFetchPlan,
    LayerLoadSink,
    LoadObserver,
    RecordingLayerLoadSink,
    ScriptedArrivalDriver,
    ScriptedLayerArrivalSource,
    SlotPlacement,
)

#: Node names used by plans built here. Tests that care about node
#: attribution build their own plans; these builders exist for tests about
#: layer accounting, so one node is enough.
TEST_NODE_NAMES = ("node-a",)


def make_slot(
    layer_id: int,
    *,
    length: int = 64,
    chunk_id: int = 0,
    plane: int = 0,
    piece: int = 0,
) -> SlotPlacement:
    """Build one slot placement for tests."""
    return SlotPlacement(
        layer_id=layer_id,
        chunk_id=chunk_id,
        node_index=0,
        record_key="record",
        plane=plane,
        piece=piece,
        offset=0,
        length=length,
    )


def make_plan(layer_slot_counts: dict[int, int]) -> LayerFetchPlan:
    """Build a plan with the given number of slots per layer."""
    slots: list[SlotPlacement] = []
    for layer_id, count in layer_slot_counts.items():
        for chunk_id in range(count):
            slots.append(make_slot(layer_id, chunk_id=chunk_id))
    return LayerFetchPlan(tuple(slots), TEST_NODE_NAMES)


@dataclass(frozen=True)
class SourceHarness:
    """One :class:`LayerArrivalSource` implementation under test.

    Attributes:
        source: A fresh source with no active fetch.
        driver: Lands and declines that source's slots.
    """

    source: LayerArrivalSource
    driver: ArrivalDriver


def _scripted_harness() -> SourceHarness:
    source = ScriptedLayerArrivalSource()
    return SourceHarness(source, ScriptedArrivalDriver(source))


#: Every source implementation the conformance suite runs against, by test id.
#: Add one factory per implementation. A factory may call ``pytest.skip`` when
#: its implementation cannot be built here, e.g. without the native extension.
def _aerospike_harness() -> SourceHarness:
    # Local
    from .aerospike_harness import aerospike_harness

    return aerospike_harness()


SOURCE_HARNESS_FACTORIES: dict[str, Callable[[], SourceHarness]] = {
    "aerospike": _aerospike_harness,
    "scripted": _scripted_harness,
}


@pytest.fixture(params=sorted(SOURCE_HARNESS_FACTORIES))
def source_harness(request: pytest.FixtureRequest) -> SourceHarness:
    """Yield a fresh harness for each registered source implementation."""
    return SOURCE_HARNESS_FACTORIES[request.param]()


@dataclass(frozen=True)
class SinkHarness:
    """One :class:`LayerLoadSink` implementation under test.

    Attributes:
        sink: A fresh sink with no active load.
        observer: Reports what that sink's consumers would see.
    """

    sink: LayerLoadSink
    observer: LoadObserver


def _recording_harness() -> SinkHarness:
    sink = RecordingLayerLoadSink()
    return SinkHarness(sink, sink)


#: Every sink implementation the conformance suite runs against, by test id.
#: Add one factory per implementation, as for sources. A factory may call
#: ``pytest.skip`` when its implementation cannot be built here, e.g. without
#: a GPU.
SINK_HARNESS_FACTORIES: dict[str, Callable[[], SinkHarness]] = {
    "recording": _recording_harness,
}


@pytest.fixture(params=sorted(SINK_HARNESS_FACTORIES))
def sink_harness(request: pytest.FixtureRequest) -> SinkHarness:
    """Yield a fresh harness for each registered sink implementation."""
    return SINK_HARNESS_FACTORIES[request.param]()
