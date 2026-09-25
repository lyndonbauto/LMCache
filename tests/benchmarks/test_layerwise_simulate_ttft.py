# SPDX-License-Identifier: Apache-2.0
"""Tests for the layerwise TTFT simulation (Track B, B8)."""

# Third Party
import pytest
import torch

# First Party
from benchmarks.layerwise.simulate_ttft import (
    Scenario,
    default_scenarios,
    modeled_ttft_ms,
    run_scenario,
)


def test_barrier_serialises_every_stage() -> None:
    """Barrier TTFT is the sum of all three stages for every layer."""
    model = modeled_ttft_ms(layers=4, remote_ms=1.0, h2d_ms=0.5, compute_ms=2.0)

    assert model.barrier_ms == pytest.approx(4 * (1.0 + 0.5 + 2.0))


def test_compute_bound_layerwise_hides_all_but_the_first_layer_of_transfer() -> None:
    """With compute slowest, only layer 0's arrival and copy stay exposed."""
    model = modeled_ttft_ms(layers=8, remote_ms=1.0, h2d_ms=0.5, compute_ms=3.0)

    assert model.layerwise_ms == pytest.approx(1.0 + 0.5 + 8 * 3.0)


def test_transfer_bound_layerwise_hides_all_but_the_last_layer_of_compute() -> None:
    """With arrival slowest, only the last layer's copy and compute stay exposed."""
    model = modeled_ttft_ms(layers=8, remote_ms=3.0, h2d_ms=0.5, compute_ms=1.0)

    assert model.layerwise_ms == pytest.approx(8 * 3.0 + 0.5 + 1.0)


def test_streamed_copy_hides_the_copy_but_not_the_compute() -> None:
    """Streaming copies removes copy time from the barrier, nothing more."""
    model = modeled_ttft_ms(layers=8, remote_ms=1.0, h2d_ms=0.5, compute_ms=3.0)

    assert model.streamed_copy_ms == pytest.approx(8 * 1.0 + 0.5 + 8 * 3.0)
    assert model.layerwise_ms <= model.streamed_copy_ms <= model.barrier_ms


def test_one_layer_cannot_overlap_anything() -> None:
    """A single layer gives every mode the same TTFT."""
    model = modeled_ttft_ms(layers=1, remote_ms=1.0, h2d_ms=0.5, compute_ms=2.0)

    assert model.barrier_ms == pytest.approx(3.5)
    assert model.streamed_copy_ms == pytest.approx(3.5)
    assert model.layerwise_ms == pytest.approx(3.5)


@pytest.mark.parametrize(
    ("layers", "remote_ms"), [(0, 1.0), (4, -1.0)], ids=["no-layers", "negative"]
)
def test_invalid_inputs_are_rejected(layers: int, remote_ms: float) -> None:
    """Nonsense inputs raise instead of returning a meaningless number."""
    with pytest.raises(ValueError):
        modeled_ttft_ms(layers=layers, remote_ms=remote_ms, h2d_ms=0.5, compute_ms=1.0)


def test_default_scenarios_follow_the_design_doc_bandwidths() -> None:
    """8 MiB per layer at 12.2 GB/s and 1.88 GB/s, as in the M0 sweep."""
    scenarios = default_scenarios(layers=32, prompt_tokens=2048)

    assert all(s.layer_bytes == 2048 * 4096 for s in scenarios)
    assert scenarios[0].remote_ms == pytest.approx(0.6876, rel=1e-3)
    assert scenarios[2].remote_ms == pytest.approx(4.462, rel=1e-3)


@pytest.mark.cuda
@pytest.mark.skipif(
    not torch.cuda.is_available(), reason="requires an available CUDA runtime"
)
def test_layerwise_beats_the_barrier_on_a_small_compute_bound_prefill() -> None:
    """End to end through the real Track B path on a GPU, tiny and quick."""
    scenario = Scenario(
        name="smoke",
        layers=4,
        layer_bytes=1024 * 1024,
        remote_ms=2.0,
        compute_ms=4.0,
    )

    result = run_scenario(scenario, repeats=3)

    assert result.layerwise_ms < result.barrier_ms
    assert result.layerwise_ms <= result.streamed_copy_ms
