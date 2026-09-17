# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the request-scoped readiness harness.

Build plumbing lives in ``conftest.py``. This covers the bookkeeping rather
than the data path, so it needs no RDMA device; ``test_rdma_pipeline.py``
exercises the same structures over a real fabric for a single chunk.
"""

# Standard
from collections.abc import Callable


def test_readiness_is_tracked_across_every_node_serving_a_request(
    logic_harness: Callable[[str], str],
) -> None:
    """A layer is ready only when every participating chunk's pieces land.

    vLLM computes a layer for the whole sequence, but the sequence's chunks
    are separate keys spread across cluster nodes, so one layer arrives from
    several nodes via several fetch commands whose notifications all land on
    one queue pair. The C++ harness checks, against the production
    ``RequestPlan`` and ``LayerReadiness``, that:

    - a layer is not reported ready when only some of its chunks have landed,
      which would otherwise hand the model a tensor still partly zeroed with
      no error raised anywhere,
    - slot indices are unique across the whole request rather than per fetch,
      so two nodes' notifications can never be confused for duplicates of
      each other,
    - arrivals interleaved across nodes and layers are each counted once, and
      a later layer can be ready while an earlier one is not,
    - a sliding-window layer is ready once its own window of trailing chunks
      arrives, rather than waiting forever for chunks it never covered,
    - writes from a superseded request are rejected by generation and do not
      register as progress, and
    - the 16-bit slot budget is shared by the whole request and refuses to
      wrap.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("request_plan_test")
