# SPDX-License-Identifier: Apache-2.0
"""Concurrent retrieves over one native client, one fetch per RDMA window.

Each retrieve holds its own :class:`AerospikeLayerArrivalSource`; all of them
share one fabric-free native client with two windows. A fetch runs in the
window of its first slot, so the second plan's slots sit in window 1.
"""

# Standard
from dataclasses import replace

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.l2_adapters.layerwise_source import (
    AerospikeLayerArrivalSource,
    NativePlanIssuer,
)
from lmcache.v1.layerwise.contract import (
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerwiseContractError,
)

# Local
from .aerospike_harness import WINDOW_BYTES, FabricFreeClient, fabric_free_connector
from .conftest import make_plan


def _in_window(plan: LayerFetchPlan, window: int) -> LayerFetchPlan:
    slots = tuple(
        replace(slot, offset=slot.offset + window * WINDOW_BYTES) for slot in plan.slots
    )
    return LayerFetchPlan(slots, plan.node_names)


def _source(connector: FabricFreeClient) -> AerospikeLayerArrivalSource:
    return AerospikeLayerArrivalSource(connector, NativePlanIssuer(connector))


def test_fetches_in_different_windows_run_side_by_side() -> None:
    """Arrivals count only for the fetch whose generation they carry."""
    connector = fabric_free_connector(window_count=2)
    first, second = _source(connector), _source(connector)
    plan = make_plan({0: 1, 1: 1})

    first_generation = first.begin_fetch(plan)
    second_generation = second.begin_fetch(_in_window(plan, 1))
    assert first_generation != second_generation

    connector.land_slot(0, first_generation)
    assert first.poll_layer(0, first_generation) is LayerArrivalStatus.RESIDENT
    assert second.poll_layer(0, second_generation) is LayerArrivalStatus.PENDING

    connector.land_slot(0, second_generation)
    connector.land_slot(1, second_generation)
    assert second.poll_layer(1, second_generation) is LayerArrivalStatus.RESIDENT
    assert first.poll_layer(1, first_generation) is LayerArrivalStatus.PENDING

    second.finish_fetch(second_generation)
    first.abandon_fetch(first_generation)


def test_a_busy_window_refuses_a_second_fetch_until_released() -> None:
    """Two plans for the same window cannot run at once."""
    connector = fabric_free_connector(window_count=2)
    first, second = _source(connector), _source(connector)
    plan = make_plan({0: 1})

    first_generation = first.begin_fetch(plan)
    with pytest.raises(LayerwiseContractError):
        second.begin_fetch(plan)

    first.abandon_fetch(first_generation)
    second_generation = second.begin_fetch(plan)
    assert second_generation != first_generation


def test_a_late_write_for_an_abandoned_fetch_does_not_reach_its_successor() -> None:
    """The window's next fetch has a new generation, so old writes miss it."""
    connector = fabric_free_connector(window_count=2)
    source = _source(connector)
    plan = make_plan({0: 1})

    old_generation = source.begin_fetch(plan)
    source.abandon_fetch(old_generation)
    new_generation = source.begin_fetch(plan)
    connector.land_slot(0, old_generation)

    assert source.poll_layer(0, new_generation) is LayerArrivalStatus.PENDING
    connector.land_slot(0, new_generation)
    assert source.poll_layer(0, new_generation) is LayerArrivalStatus.RESIDENT
    source.finish_fetch(new_generation)
