# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the kv-sink fetch bookkeeping harness.

Build plumbing lives in ``conftest.py``. This covers the bookkeeping rather
than the data path, so it needs no RDMA device or cluster.
"""

# Standard
from collections.abc import Callable


def test_sink_fetch_table_accounts_for_layers_windows_and_stale_results(
    logic_harness: Callable[[str], str],
) -> None:
    """``SinkFetchTable`` keeps the accounting the layerwise source relies on.

    The C++ harness checks, against the production table, that:

    - a plan becomes one batch per layer, in the order layers first appear,
      each prioritized by its ordinal so the server places layer 0 first,
    - a layer is ready only when every one of its slots landed, and a failed
      slot makes its layer unservable without affecting the others,
    - each window runs one fetch, and a malformed or oversized plan leaves
      nothing active, and
    - a result from an abandoned fetch is dropped, even after the 16-bit
      generation wraps back to its value.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("sink_fetch_table_test")
