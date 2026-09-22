# SPDX-License-Identifier: Apache-2.0
"""Track A's skeleton matches the frozen arrival contract.

These tests pin the *shape* of Track A's implementation before it has any
behaviour, so that Track B and Track C can build against it. They are
deliberately cheap; the real coverage is the conformance suite, which this
implementation is expected to pass once the methods are filled in.
"""

# Standard
import inspect

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.l2_adapters.layerwise_source import (
    AerospikeLayerArrivalSource,
)
from lmcache.v1.layerwise import LayerArrivalSource


def test_the_aerospike_source_satisfies_the_arrival_protocol() -> None:
    """Track A's class is a LayerArrivalSource as far as the protocol can tell."""
    assert isinstance(AerospikeLayerArrivalSource(), LayerArrivalSource)


@pytest.mark.parametrize(
    "method_name",
    ["begin_fetch", "poll_layer", "finish_fetch", "abandon_fetch"],
)
def test_the_source_signature_matches_the_contract(method_name: str) -> None:
    """Every method keeps the parameter names and types the contract declares.

    ``isinstance`` against a runtime_checkable Protocol only checks that the
    attributes exist, so it would accept a method with the wrong parameters.
    Comparing signatures is what actually stops the two sides drifting.
    """
    expected = inspect.signature(getattr(LayerArrivalSource, method_name))
    actual = inspect.signature(getattr(AerospikeLayerArrivalSource, method_name))
    assert actual == expected


def test_the_unimplemented_source_refuses_to_pretend_it_worked() -> None:
    """An unfilled method raises rather than returning a plausible default.

    A skeleton that returned ``PENDING`` would let a caller poll forever
    against a transport that does not exist yet.
    """
    source = AerospikeLayerArrivalSource()
    with pytest.raises(NotImplementedError):
        source.poll_layer(0, 1)
