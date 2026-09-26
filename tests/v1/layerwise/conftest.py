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


class RecordingLauncher:
    """A :class:`LayerLauncher` that records calls instead of touching a GPU.

    ``calls`` holds ``"begin"``, ``("launch", layer_id)`` and ``"failed"`` in
    the order they happened, so tests can assert exactly what the sink asked
    the GPU side to do.
    """

    def __init__(self) -> None:
        """Build a launcher with no calls recorded."""
        self.calls: list[object] = []

    def begin(self) -> None:
        """Record that setup ran."""
        self.calls.append("begin")

    def launch_layer(self, layer_id: int) -> None:
        """Record one layer launch.

        Args:
            layer_id: Global layer index.
        """
        self.calls.append(("launch", layer_id))

    def mark_failed(self) -> None:
        """Record that the retrieve was marked failed."""
        self.calls.append("failed")

    def launched_layers(self) -> list[int]:
        """Return the launched layer ids, in launch order.

        Returns:
            Layer ids from every recorded launch.
        """
        return [
            call[1]
            for call in self.calls
            if isinstance(call, tuple) and call[0] == "launch"
        ]


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
SOURCE_HARNESS_FACTORIES: dict[str, Callable[[], SourceHarness]] = {
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


def _multiprocess_harness() -> SinkHarness:
    """Track B's loader over real progress records (no GPU needed).

    Imported here, not at module level, so these tests do not depend on the
    multiprocess package unless this entry runs.
    """
    try:
        # Local
        from .multiprocess_sink_harness import multiprocess_sink_harness
    except ImportError as exc:  # e.g. torch or the native extension missing
        pytest.skip(f"multiprocess loader unavailable: {exc}")
    sink, observer = multiprocess_sink_harness()
    return SinkHarness(sink, observer)


#: Every sink implementation the conformance suite runs against, by test id.
#: Add one factory per implementation, as for sources. A factory may call
#: ``pytest.skip`` when its implementation cannot be built here, e.g. without
#: a GPU.
SINK_HARNESS_FACTORIES: dict[str, Callable[[], SinkHarness]] = {
    "recording": _recording_harness,
    "multiprocess": _multiprocess_harness,
}


@pytest.fixture(params=sorted(SINK_HARNESS_FACTORIES))
def sink_harness(request: pytest.FixtureRequest) -> SinkHarness:
    """Yield a fresh harness for each registered sink implementation."""
    return SINK_HARNESS_FACTORIES[request.param]()
