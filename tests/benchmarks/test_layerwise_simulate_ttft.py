# SPDX-License-Identifier: Apache-2.0
"""Tests for the layerwise TTFT simulation (Track B, B8)."""

# Standard
import itertools
import statistics

# Third Party
import pytest
import torch

# First Party
from benchmarks.layerwise.simulate_ttft import (
    Scenario,
    arrival_offsets_ms,
    default_scenarios,
    modeled_ttft_ms,
    run_scenario,
)


def _fixed(layers: int, remote_ms: float) -> tuple[float, ...]:
    return arrival_offsets_ms(layers, remote_ms)


def test_barrier_serialises_every_stage() -> None:
    """Barrier TTFT is the last arrival, the whole copy, then all compute."""
    model = modeled_ttft_ms(
        _fixed(4, 1.0), h2d_ms=0.5, compute_ms=2.0, barrier_copy_ms=4 * 0.5
    )

    assert model.barrier_ms == pytest.approx(4 * (1.0 + 0.5 + 2.0))


def test_compute_bound_layerwise_hides_all_but_the_first_layer_of_transfer() -> None:
    """With compute slowest, only layer 0's arrival and copy stay exposed."""
    model = modeled_ttft_ms(
        _fixed(8, 1.0), h2d_ms=0.5, compute_ms=3.0, barrier_copy_ms=4.0
    )

    assert model.layerwise_ms == pytest.approx(1.0 + 0.5 + 8 * 3.0)


def test_transfer_bound_layerwise_hides_all_but_the_last_layer_of_compute() -> None:
    """With arrival slowest, only the last layer's copy and compute stay exposed."""
    model = modeled_ttft_ms(
        _fixed(8, 3.0), h2d_ms=0.5, compute_ms=1.0, barrier_copy_ms=4.0
    )

    assert model.layerwise_ms == pytest.approx(8 * 3.0 + 0.5 + 1.0)


def test_streamed_copy_hides_the_copy_but_not_the_compute() -> None:
    """Streaming copies removes copy time from the barrier, nothing more."""
    model = modeled_ttft_ms(
        _fixed(8, 1.0), h2d_ms=0.5, compute_ms=3.0, barrier_copy_ms=4.0
    )

    assert model.streamed_copy_ms == pytest.approx(8 * 1.0 + 0.5 + 8 * 3.0)
    assert model.layerwise_ms <= model.streamed_copy_ms <= model.barrier_ms


def test_one_layer_cannot_overlap_anything() -> None:
    """A single layer gives every mode the same TTFT."""
    model = modeled_ttft_ms(
        _fixed(1, 1.0), h2d_ms=0.5, compute_ms=2.0, barrier_copy_ms=0.5
    )

    assert model.barrier_ms == pytest.approx(3.5)
    assert model.streamed_copy_ms == pytest.approx(3.5)
    assert model.layerwise_ms == pytest.approx(3.5)


def test_a_late_layer_stalls_layerwise_but_not_past_the_barrier() -> None:
    """One slow arrival delays everything after it, in every mode."""
    arrivals = (1.0, 2.0, 9.0, 10.0)

    model = modeled_ttft_ms(arrivals, h2d_ms=0.5, compute_ms=1.0, barrier_copy_ms=2.0)

    assert model.layerwise_ms == pytest.approx(10.0 + 0.5 + 1.0)
    assert model.barrier_ms == pytest.approx(10.0 + 2.0 + 4 * 1.0)


@pytest.mark.parametrize(
    "arrivals",
    [(), (-1.0, 1.0), (2.0, 1.0)],
    ids=["no-layers", "negative", "decreasing"],
)
def test_invalid_model_inputs_are_rejected(arrivals: tuple[float, ...]) -> None:
    """Nonsense inputs raise instead of returning a meaningless number."""
    with pytest.raises(ValueError):
        modeled_ttft_ms(arrivals, h2d_ms=0.5, compute_ms=1.0, barrier_copy_ms=1.0)


def test_fixed_arrivals_are_evenly_spaced() -> None:
    assert arrival_offsets_ms(4, 1.5) == pytest.approx((1.5, 3.0, 4.5, 6.0))


def test_jittered_arrivals_have_the_requested_median_and_tail() -> None:
    """Intervals keep the median and reach roughly the requested p99."""
    arrivals = arrival_offsets_ms(20_000, 1.0, p99_ratio=2.0, seed=7)
    intervals = [b - a for a, b in itertools.pairwise((0.0, *arrivals))]

    assert statistics.median(intervals) == pytest.approx(1.0, rel=0.02)
    p99 = statistics.quantiles(intervals, n=100)[98]
    assert p99 == pytest.approx(2.0, rel=0.05)
    assert all(later >= earlier for earlier, later in itertools.pairwise(arrivals))


def test_jittered_arrivals_are_reproducible_per_seed() -> None:
    first = arrival_offsets_ms(32, 1.0, p99_ratio=1.5, seed=3)

    assert arrival_offsets_ms(32, 1.0, p99_ratio=1.5, seed=3) == first
    assert arrival_offsets_ms(32, 1.0, p99_ratio=1.5, seed=4) != first


@pytest.mark.parametrize(
    ("layers", "remote_ms", "p99_ratio"),
    [(0, 1.0, 1.0), (4, -1.0, 1.0), (4, 1.0, 0.5)],
    ids=["no-layers", "negative", "tail-below-median"],
)
def test_invalid_arrival_inputs_are_rejected(
    layers: int, remote_ms: float, p99_ratio: float
) -> None:
    with pytest.raises(ValueError):
        arrival_offsets_ms(layers, remote_ms, p99_ratio)


def test_default_scenarios_follow_the_design_doc_bandwidths() -> None:
    """8 MiB per layer at 12.2 GB/s and 1.88 GB/s, as in the M0 sweep."""
    scenarios = default_scenarios(layers=32, prompt_tokens=2048)

    assert all(s.layer_bytes == 2048 * 4096 for s in scenarios)
    assert scenarios[0].remote_ms == pytest.approx(0.6876, rel=1e-3)
    assert scenarios[2].remote_ms == pytest.approx(4.462, rel=1e-3)


def test_default_scenarios_split_layers_like_production_objects() -> None:
    """2048 tokens in 256-token chunks, K and V planes: 16 copies per layer."""
    scenario = default_scenarios(layers=32, prompt_tokens=2048)[0]

    assert (scenario.chunks, scenario.planes) == (8, 2)
    assert scenario.copies_per_layer == 16
    assert scenario.piece_bytes == 2048 * 4096 // 16


def test_default_scenarios_reject_a_partial_chunk() -> None:
    with pytest.raises(ValueError):
        default_scenarios(prompt_tokens=2048, chunk_tokens=300)


@pytest.mark.cuda
@pytest.mark.skipif(
    not torch.cuda.is_available(), reason="requires an available CUDA runtime"
)
@pytest.mark.parametrize(
    ("chunks", "planes", "p99_ratio"),
    [(1, 1, 1.0), (4, 2, 1.8)],
    ids=["one-copy-fixed", "per-plane-jittered"],
)
def test_layerwise_beats_the_barrier_on_a_small_compute_bound_prefill(
    chunks: int, planes: int, p99_ratio: float
) -> None:
    """End to end through the real Track B path on a GPU, tiny and quick."""
    scenario = Scenario(
        name="smoke",
        layers=4,
        layer_bytes=1024 * 1024,
        remote_ms=2.0,
        compute_ms=4.0,
        chunks=chunks,
        planes=planes,
        arrival_p99_ratio=p99_ratio,
    )

    result = run_scenario(scenario, repeats=3)

    assert result.layerwise_ms < result.barrier_ms
    assert result.layerwise_ms <= result.streamed_copy_ms
