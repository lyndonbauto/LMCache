# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the RDMA layer-pipelining harness.

Build and device setup live in ``conftest.py``.
"""

# Standard
from collections.abc import Callable


def test_a_layer_is_consumable_before_later_layers_arrive(
    rdma_harness: Callable[[str], str],
) -> None:
    """A layer is readable and correct while later layers are still missing.

    This is the property the pipelining work depends on. The C++ harness
    stages ``RDMA_WRITE_WITH_IMM`` pushes through a mock Aerospike server and
    checks, against the production ``RequestPlan`` and ``LayerReadiness``, that:

    - a layer is reported ready only once every one of its pieces has landed,
    - its bytes are correct at the moment it is reported ready,
    - the destination regions of unsent layers are still untouched,
    - a later layer can be ready while an earlier one is not, since AWS SRD
      delivers out of order, and
    - immediates from a superseded fetch are rejected by generation.

    Args:
        rdma_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in rdma_harness("rdma_pipeline_test")
