# SPDX-License-Identifier: Apache-2.0
"""Pytest wrapper for notification depth budgeting harness."""

# Standard
from collections.abc import Callable


def test_notification_depth_respects_device_caps(
    logic_harness: Callable[[str], str],
) -> None:
    """Clamping and begin_request enforcement without libibverbs.

    Args:
        logic_harness: Fixture that builds and runs a named harness.
    """
    assert "PASS" in logic_harness("notification_depth_test")
