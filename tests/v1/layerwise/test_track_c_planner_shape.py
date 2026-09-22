# SPDX-License-Identifier: Apache-2.0
"""Track C's planner skeleton is wired up and honest about being unfinished.

Planning has no counterpart across a contract, so there is no protocol to
check it against. What these tests pin instead is that the skeleton is
importable, that its inputs are expressible, and that it refuses rather than
returning an empty plan -- an empty plan would be rejected by
``LayerFetchPlan`` anyway, but a plan built from partial arithmetic would not,
and that is the failure worth guarding against as this gets filled in.
"""

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise.planner import FetchPlanner, PlanRequest


def make_request() -> PlanRequest:
    """Build a small two-chunk, two-layer request across two nodes.

    Returns:
        A request spanning more than one node, so that plans built from it
        exercise global slot numbering rather than per-node numbering.
    """
    return PlanRequest(
        layer_ids=(0, 1),
        chunk_ids=(0, 1),
        node_index_by_chunk={0: 0, 1: 1},
        digest_by_chunk={0: b"\x00" * 20, 1: b"\x01" * 20},
        kv_planes=2,
        plane_bytes=4096,
    )


def test_a_plan_request_is_expressible_for_a_multi_node_fetch() -> None:
    """The planner's input type can describe chunks spread over nodes."""
    request = make_request()
    assert request.node_index_by_chunk[1] == 1
    assert request.kv_planes == 2


def test_the_unimplemented_planner_refuses_to_return_a_plan() -> None:
    """Planning raises rather than handing back a plausible partial plan."""
    with pytest.raises(NotImplementedError):
        FetchPlanner().plan(make_request())


def test_the_unimplemented_window_helper_refuses_to_guess() -> None:
    """The window helper raises rather than returning every candidate chunk.

    Returning all chunks would be a valid-looking answer that quietly fetches
    tokens outside the window.
    """
    with pytest.raises(NotImplementedError):
        FetchPlanner().participating_chunks((0, 1, 2), 0, 15, 16)
