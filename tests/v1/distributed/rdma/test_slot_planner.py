# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the slot-schedule harness.

Build plumbing lives in ``conftest.py``. This covers the arithmetic that
decides which writes a request expects and where each lands, so it needs no
RDMA device.
"""

# Standard
from collections.abc import Callable


def test_a_layer_is_scheduled_as_all_of_its_kv_planes(
    logic_harness: Callable[[str], str],
) -> None:
    """A layer's schedule covers every K/V plane it owns, not one range.

    In the standard layout the K/V dimension is outermost -- K for every
    layer, then V for every layer -- so a layer's K and V sit a whole layer
    dimension apart. A planner that emitted one write per layer per chunk
    would deliver K, see it land, report the layer ready and hand the model a
    cache whose V half still holds whatever was in the window before: right
    shape, right dtype, plausible values, no error anywhere.

    The C++ harness checks, against the production ``SlotPlanner``, that:

    - a layer resolves to ``kv_size`` byte ranges that are provably not
      adjacent, and all layers' ranges tile the payload exactly once,
    - the layer is reported ready only once every one of its planes lands,
    - a plane larger than the maximum RDMA write becomes several writes, none
      over the cap, together covering the plane without crossing into its
      neighbour,
    - each kernel group is strided by its own geometry, so a hybrid object
      group holding groups of different plane sizes never applies one group's
      stride to another's layers,
    - the schedule is ordered layer-major across chunks, so every chunk's
      layer 0 is asked for before any chunk's layer 1,
    - a sliding-window group is scheduled over its own trailing window and can
      therefore complete, while full attention still waits for every chunk,
    - an object group the request never placed contributes no writes and its
      layers are never reported ready, which is what CacheBlend's per-leg
      subsets need, and
    - ambiguous layouts are refused rather than resolved arbitrarily,
      including a layer appearing in two kernel groups and a chunk placed
      twice for one group.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("slot_planner_test")
