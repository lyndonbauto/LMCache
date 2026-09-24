# SPDX-License-Identifier: Apache-2.0
"""Track A's source matches the frozen arrival contract.

These tests pin the *shape* of Track A's implementation so that Track B and
Track C can build against it. Behaviour is covered in
``test_aerospike_layer_arrival_source.py``.
"""

# Standard
import inspect

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.l2_adapters.layerwise_source import (
    AerospikeLayerArrivalSource,
    NativePlanIssuer,
    PipelinedFetchConnector,
)
from lmcache.v1.layerwise import LayerArrivalSource

# Local
from .conftest import make_plan


class _ReadyConnector:
    """The smallest native client that reports pipelined fetch as ready."""

    def pipelined_fetch_ready(self) -> bool:
        return True

    def pipelined_fetch_init_error(self) -> str:
        return ""

    def is_pipelined_layer_ready(self, layer_id: int, request_generation: int) -> bool:
        return False

    def pipelined_unservable_layers(self) -> list[int]:
        return []

    def finish_pipelined_fetch(self) -> None:
        return None

    def abandon_pipelined_fetch(self) -> None:
        return None


def _native_source() -> AerospikeLayerArrivalSource:
    connector = _ReadyConnector()
    return AerospikeLayerArrivalSource(connector, NativePlanIssuer(connector))


def test_the_fake_connector_matches_the_native_surface() -> None:
    """Keeps the stand-in used below honest about the protocol it replaces."""
    assert isinstance(_ReadyConnector(), PipelinedFetchConnector)


def test_the_aerospike_source_satisfies_the_arrival_protocol() -> None:
    """Track A's class is a LayerArrivalSource as far as the protocol can tell."""
    assert isinstance(_native_source(), LayerArrivalSource)


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


def test_the_unfinished_plan_translation_refuses_to_pretend_it_worked() -> None:
    """The one unimplemented step raises rather than returning a generation.

    A generation from a fetch that was never issued would let a caller poll
    forever against slots nobody asked for.
    """
    with pytest.raises(NotImplementedError):
        _native_source().begin_fetch(make_plan({0: 1}))
