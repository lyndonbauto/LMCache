# SPDX-License-Identifier: Apache-2.0
"""Tests for layerwise contract fakes."""

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import (
    LayerArrivalSource,
    LayerLoadSink,
    LayerNotInPlanError,
    LayerwiseContractError,
    RecordingLayerLoadSink,
    ScriptedLayerArrivalSource,
    StaleGenerationError,
)

# Local
from .conftest import make_plan


def test_scripted_source_rejects_second_concurrent_begin_fetch() -> None:
    """Only one active fetch may exist at a time."""
    source = ScriptedLayerArrivalSource()
    plan = make_plan({0: 1})
    source.begin_fetch(plan)
    with pytest.raises(LayerwiseContractError, match="still active"):
        source.begin_fetch(plan)


def test_scripted_source_rejects_stale_generation_on_poll_and_finish() -> None:
    """Late replies must not credit the wrong fetch."""
    source = ScriptedLayerArrivalSource()
    plan = make_plan({0: 1})
    generation = source.begin_fetch(plan)
    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, generation + 1)
    with pytest.raises(StaleGenerationError):
        source.finish_fetch(generation + 1)


def test_scripted_source_rejects_out_of_plan_layer_on_poll() -> None:
    """Polling a layer outside the active plan is a contract violation."""
    source = ScriptedLayerArrivalSource()
    plan = make_plan({0: 1})
    generation = source.begin_fetch(plan)
    with pytest.raises(LayerNotInPlanError):
        source.poll_layer(99, generation)


def test_scripted_source_tolerates_abandon_fetch_for_finished_generation() -> None:
    """Error paths may abandon a generation that is already gone."""
    source = ScriptedLayerArrivalSource()
    plan = make_plan({0: 1})
    generation = source.begin_fetch(plan)
    source.finish_fetch(generation)
    source.abandon_fetch(generation)
    assert source.abandoned_generations() == (generation,)


def test_recording_sink_rejects_load_layer_before_begin_load() -> None:
    """Copies cannot be issued before a load is opened."""
    sink = RecordingLayerLoadSink()
    with pytest.raises(LayerwiseContractError, match="no load is active"):
        sink.load_layer(0)


def test_recording_sink_rejects_out_of_order_load_layer() -> None:
    """Layers must be issued in the order given to begin_load."""
    sink = RecordingLayerLoadSink()
    sink.begin_load(1, (0, 1))
    with pytest.raises(LayerNotInPlanError, match="expected layer 0"):
        sink.load_layer(1)


def test_recording_sink_rejects_extra_layers_past_expected_count() -> None:
    """Issuing more layers than expected is a contract violation."""
    sink = RecordingLayerLoadSink()
    sink.begin_load(1, (0,))
    sink.load_layer(0)
    with pytest.raises(LayerwiseContractError, match="after all"):
        sink.load_layer(0)


def test_recording_sink_rejects_finish_load_with_unissued_layer() -> None:
    """Finishing early would leave consumers waiting on copies never queued."""
    sink = RecordingLayerLoadSink()
    sink.begin_load(1, (0, 1))
    sink.load_layer(0)
    with pytest.raises(LayerwiseContractError, match="never issued"):
        sink.finish_load(1)


def test_fakes_satisfy_runtime_checkable_protocols() -> None:
    """Fakes are valid stand-ins for the transport and loader protocols."""
    assert isinstance(ScriptedLayerArrivalSource(), LayerArrivalSource)
    assert isinstance(RecordingLayerLoadSink(), LayerLoadSink)
