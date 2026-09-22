# SPDX-License-Identifier: Apache-2.0
"""Tests for :class:`LayerFetchPlan`."""

# Standard
import types

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise import LayerFetchPlan, LayerNotInPlanError

# Local
from .conftest import make_plan, make_slot


def test_layer_ids_returns_each_layer_once_in_ascending_order() -> None:
    """layer_ids is ascending and unique however the slots were supplied.

    That order is the order the loader must copy in, so it cannot depend on
    the order slots happened to be planned.
    """
    slots = (
        make_slot(5),
        make_slot(1),
        make_slot(3),
        make_slot(1, chunk_id=1),
        make_slot(5, chunk_id=1),
        make_slot(3, chunk_id=1),
    )
    plan = LayerFetchPlan(slots)
    assert plan.layer_ids() == (1, 3, 5)


def test_slots_for_layer_counts_only_that_layer() -> None:
    """Transport accounting is per-layer slot count, not total slots."""
    plan = make_plan({2: 3, 7: 1})
    assert plan.slots_for_layer(2) == 3
    assert plan.slots_for_layer(7) == 1


def test_slots_for_layer_raises_for_uncovered_layer() -> None:
    """Referencing a layer outside the plan fails loudly."""
    plan = make_plan({0: 1})
    with pytest.raises(LayerNotInPlanError, match="layer 9"):
        plan.slots_for_layer(9)


def test_slot_counts_matches_slots_for_layer_and_is_immutable() -> None:
    """slot_counts is a read-only view consistent with slots_for_layer."""
    plan = make_plan({4: 2, 1: 1})
    counts = plan.slot_counts()
    for layer_id in plan.layer_ids():
        assert counts[layer_id] == plan.slots_for_layer(layer_id)
    assert isinstance(counts, types.MappingProxyType)
    with pytest.raises(TypeError):
        counts[1] = 99  # type: ignore[index]


def test_constructor_rejects_empty_plan() -> None:
    """A fetch with no slots cannot complete."""
    with pytest.raises(ValueError, match="at least one slot"):
        LayerFetchPlan(())


def test_constructor_rejects_non_positive_slot_length() -> None:
    """Zero- or negative-length slots cannot describe real RDMA writes."""
    with pytest.raises(ValueError, match="non-positive length"):
        LayerFetchPlan((make_slot(0, length=0),))
    with pytest.raises(ValueError, match="non-positive length"):
        LayerFetchPlan((make_slot(0, length=-1),))
