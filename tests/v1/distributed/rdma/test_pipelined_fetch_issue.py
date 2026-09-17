# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for pipelined fetch issue logic tests."""

# Standard
from collections.abc import Callable


def test_pipelined_fetch_issue_handles_transport_failures(
    logic_harness: Callable[[str], str],
) -> None:
    """Issue marks unreachable nodes unservable and abandons on hard failures."""
    assert "PASS" in logic_harness("pipelined_fetch_issue_test")
