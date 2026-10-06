# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the slot-planner geometry harness.

Build plumbing lives in ``conftest.py``. This covers the C++ layer geometry
the fetch driver validates layouts with, so it needs no RDMA device. The slot
schedule itself is built in Python and covered by
``tests/v1/layerwise/test_fetch_planner.py`` and ``test_slot_plan_parity.py``.
"""

# Standard
from collections.abc import Callable


def test_a_layer_resolves_to_all_of_its_kv_planes(
    logic_harness: Callable[[str], str],
) -> None:
    """A layer occupies every K/V plane it owns, not one range.

    In the standard layout the K/V dimension is outermost -- K for every
    layer, then V for every layer -- so a layer's K and V sit a whole layer
    dimension apart. Treating a layer as one range would deliver K and hand
    the model a V half that still holds whatever was in the window before:
    right shape, right dtype, plausible values, no error anywhere.

    The C++ harness checks, against ``SlotPlanner``, that:

    - a layer resolves to ``kv_size`` byte ranges that are provably not
      adjacent, and all layers' ranges tile the payload exactly once,
    - the single-plane ``NL_X_NB_BS_HS`` layout needs no special case,
    - each kernel group is strided by its own geometry, so a hybrid object
      group holding groups of different plane sizes never applies one group's
      stride to another's layers,
    - each layer resolves to its own object group,
    - ambiguous layouts are refused rather than resolved arbitrarily,
      including a layer appearing in two kernel groups, and
    - a window too small for one chunk is refused with the size it needs.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("slot_planner_test")
