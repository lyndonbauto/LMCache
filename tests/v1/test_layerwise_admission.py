# SPDX-License-Identifier: Apache-2.0
"""``LayerwiseAdmissionGate``: which layerwise loads start in a step."""

# Third Party
import pytest

# First Party
from lmcache.integration.vllm.layerwise_admission import LayerwiseAdmissionGate

GIB = 1 << 30


def test_a_disabled_gate_admits_every_request() -> None:
    gate = LayerwiseAdmissionGate(0)

    assert not gate.enabled
    assert all(gate.admit(f"r{i}", 100 * GIB) for i in range(32))


def test_requests_are_admitted_while_the_step_fits_the_budget() -> None:
    gate = LayerwiseAdmissionGate(4 * GIB)

    assert [gate.admit(f"r{i}", GIB) for i in range(6)] == [True] * 4 + [False] * 2
    assert gate.admitted_bytes == 4 * GIB


def test_once_a_request_is_held_later_ones_are_held_too() -> None:
    """FIFO: a small request does not overtake a held large one."""
    gate = LayerwiseAdmissionGate(4 * GIB)

    assert gate.admit("a", 3 * GIB)
    assert not gate.admit("b", 2 * GIB)
    assert not gate.admit("c", 1)
    assert gate.admitted_bytes == 3 * GIB


def test_the_first_request_of_a_step_is_admitted_even_if_oversized() -> None:
    gate = LayerwiseAdmissionGate(GIB)

    assert gate.admit("big", 8 * GIB)
    assert not gate.admit("next", 1)
    gate.end_step()
    assert gate.admit("next", 1)


def test_asking_again_in_the_same_step_does_not_count_twice() -> None:
    gate = LayerwiseAdmissionGate(2 * GIB)

    assert gate.admit("a", GIB)
    assert gate.admit("a", GIB)
    assert gate.admitted_bytes == GIB
    assert gate.admit("b", GIB)


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "whole_load", "abort", "preemption"]
)
def test_the_next_step_starts_with_the_full_budget_whatever_happened(
    outcome: str,
) -> None:
    """Every load ends inside its step, so no exit path keeps budget: the
    admitted requests' outcome is never reported to the gate at all."""
    gate = LayerwiseAdmissionGate(2 * GIB)
    assert gate.admit(f"{outcome}-1", 2 * GIB)
    assert not gate.admit(f"{outcome}-2", GIB)

    gate.end_step()

    assert gate.admitted_bytes == 0
    assert gate.admit(f"{outcome}-2", GIB)
    assert gate.admit(f"{outcome}-3", GIB)


def test_a_held_request_that_never_returns_holds_nothing() -> None:
    """An aborted waiting request is simply not asked about again."""
    gate = LayerwiseAdmissionGate(GIB)
    assert gate.admit("a", GIB)
    assert not gate.admit("aborted", GIB)

    gate.end_step()

    assert gate.admit("b", GIB)


def test_a_zero_byte_request_fits_any_budget() -> None:
    gate = LayerwiseAdmissionGate(GIB)

    assert gate.admit("a", GIB)
    assert gate.admit("empty", 0)


@pytest.mark.parametrize("budget", [-1, -GIB])
def test_a_negative_budget_is_rejected(budget: int) -> None:
    with pytest.raises(ValueError):
        LayerwiseAdmissionGate(budget)


def test_a_negative_request_size_is_rejected() -> None:
    gate = LayerwiseAdmissionGate(GIB)

    with pytest.raises(ValueError):
        gate.admit("a", -1)
