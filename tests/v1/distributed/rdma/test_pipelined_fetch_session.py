# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for the pipelined-fetch session harness.

The session stitches plan building, per-node commands, declined-slot handling,
and notification draining without any cluster or device. These tests defend
against the numbering and abandon failures that only appear when multiple nodes
share one slot index space.
"""

# Standard
from collections.abc import Callable


def test_the_pipelined_fetch_session_driver(
    logic_harness: Callable[[str], str],
) -> None:
    """Multi-node numbering, declined slots, stale generations, and abandon.

    Without a dedicated driver, each of these invariants would have to be
    reimplemented at the connector and would never run in CI without hardware:

    - two nodes still share one slot index space in their commands,
    - a failed slot in the reply makes only its layer unservable,
    - notifications tagged with a finished request's generation are ignored,
    - a reply after abandon does not affect a later request.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("pipelined_fetch_session_test")
