# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the plane-aligned sharding harness.

Build plumbing lives in ``conftest.py``. Unlike the data-path harnesses here
this one needs no RDMA device and no Aerospike cluster.
"""

# Standard
from collections.abc import Callable


def test_a_record_never_spans_two_model_layers(
    logic_harness: Callable[[str], str],
) -> None:
    """Records stay confined to one K/V plane, so each maps to one layer.

    This is what lets a layer-pipelined reader serve layer 0 without waiting
    on layer 1. The C++ harness checks, against the production
    ``make_shard_plan`` and ``segment_range``, that:

    - no plane-aligned record spans a plane boundary, while byte-count
      sharding demonstrably does,
    - a layer waits for exactly the bytes it owns, where byte-count sharding
      inflates that by the amounts recorded in the design doc for the unified
      block sizes Mamba/GDN hybrids force (+88% at 544 tokens, +31% at 784,
      +8% at 944),
    - records still tile the payload exactly once, including when a plane's
      size is not a whole multiple of the record size and the plane therefore
      ends in a short record,
    - a multi-plane payload small enough for the single-record fast path is
      still split per plane rather than collapsed into one record holding
      several, and
    - an alignment hint that cannot describe the payload is refused visibly
      rather than applied to a layout it does not fit.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("shard_plan_test")
