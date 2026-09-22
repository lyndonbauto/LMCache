# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the RDMA byte-equivalence harness.

Build and device setup live in ``conftest.py``.
"""

# Standard
from collections.abc import Callable


def test_rdma_write_is_byte_identical_to_the_normal_path(
    rdma_harness: Callable[[str], str],
) -> None:
    """A payload delivered by RDMA into L1 matches the non-RDMA payload.

    Runs the C++ harness, which drives the production ``RdmaContext`` and
    kv-sink codec against a mock Aerospike writer that performs real
    ``ibv_post_send`` RDMA writes and fences on its own send completion queue.

    Args:
        rdma_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in rdma_harness("rdma_equivalence_test")
