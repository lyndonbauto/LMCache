# SPDX-License-Identifier: Apache-2.0
"""Track B's sink matches the frozen load contract.

These pin the *shape* of Track B's implementation so Track A and Track C can
build against it. Behaviour is covered in ``test_multiprocess_sink.py``.
"""

# Standard
import inspect

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import LayerLoadSink, LayerwiseContractError
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.layerwise_sink import MultiprocessLayerLoadSink

# Local
from .conftest import RecordingLauncher


def _make_sink() -> MultiprocessLayerLoadSink:
    """Build a one-retrieve sink over a two-layer schedule."""
    return MultiprocessLayerLoadSink.for_retrieve(
        LayerwiseSchedule([[0, 1]]), RecordingLauncher()
    )


def test_the_multiprocess_sink_satisfies_the_load_protocol() -> None:
    """Track B's class is a LayerLoadSink as far as the protocol can tell."""
    assert isinstance(_make_sink(), LayerLoadSink)


@pytest.mark.parametrize(
    "method_name",
    ["begin_load", "load_layer", "finish_load", "abandon_load"],
)
def test_the_sink_signature_matches_the_contract(method_name: str) -> None:
    """Every method keeps the parameter names and types the contract declares.

    ``isinstance`` against a runtime_checkable Protocol only checks that the
    attributes exist, so it would accept a method with the wrong parameters.
    Comparing signatures is what actually stops the two sides drifting.
    """
    expected = inspect.signature(getattr(LayerLoadSink, method_name))
    actual = inspect.signature(getattr(MultiprocessLayerLoadSink, method_name))
    assert actual == expected


def test_the_sink_refuses_to_load_before_a_load_begins() -> None:
    """A layer issued with no active load is refused rather than ignored.

    A sink whose ``load_layer`` quietly returned would let a pump run to
    completion having copied nothing, which reads as success.
    """
    sink = _make_sink()
    with pytest.raises(LayerwiseContractError):
        sink.load_layer(0)
