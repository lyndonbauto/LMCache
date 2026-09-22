# SPDX-License-Identifier: Apache-2.0
"""Track B's skeleton matches the frozen load contract.

These tests pin the *shape* of Track B's implementation before it has any
behaviour, so that Track A and Track C can build against it. They are
deliberately cheap and need no GPU; the real coverage is the conformance
suite, which this implementation is expected to pass once the methods are
filled in.
"""

# Standard
import inspect

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import LayerLoadSink
from lmcache.v1.multiprocess.layerwise_sink import MultiprocessLayerLoadSink


def test_the_multiprocess_sink_satisfies_the_load_protocol() -> None:
    """Track B's class is a LayerLoadSink as far as the protocol can tell."""
    assert isinstance(MultiprocessLayerLoadSink(), LayerLoadSink)


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


def test_the_unimplemented_sink_refuses_to_pretend_it_worked() -> None:
    """An unfilled method raises rather than silently doing nothing.

    A skeleton whose ``load_layer`` returned ``None`` would let a pump run to
    completion having copied nothing, which reads as success.
    """
    sink = MultiprocessLayerLoadSink()
    with pytest.raises(NotImplementedError):
        sink.load_layer(0)
