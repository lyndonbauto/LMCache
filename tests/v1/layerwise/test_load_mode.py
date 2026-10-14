# SPDX-License-Identifier: Apache-2.0
"""Tests for the per-request load-mode policy."""

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise.load_mode import (
    DEFAULT_MIN_NEW_TOKEN_RATIO,
    LoadModePolicy,
)
from lmcache.v1.multiprocess.custom_types import LoadMode


def test_the_default_ratio_is_a_tenth() -> None:
    assert LoadModePolicy().min_new_token_ratio == DEFAULT_MIN_NEW_TOKEN_RATIO == 0.1


@pytest.mark.parametrize(
    ("prompt_chunks", "hit_chunks", "expected"),
    [
        # Full hit: nothing to compute, so nothing hides the load.
        (32, 32, LoadMode.ASYNC),
        # 1 new per 31 hit is below 0.1.
        (32, 31, LoadMode.ASYNC),
        # 3 new per 29 hit is above 0.1.
        (32, 29, LoadMode.LAYERWISE),
        # Exactly at the ratio loads layerwise.
        (11, 10, LoadMode.LAYERWISE),
        # Partial hit with a long suffix.
        (64, 32, LoadMode.LAYERWISE),
        # No hit loads nothing either way.
        (32, 0, LoadMode.LAYERWISE),
        (0, 0, LoadMode.LAYERWISE),
    ],
)
def test_the_mode_follows_new_chunks_per_hit_chunk(
    prompt_chunks: int, hit_chunks: int, expected: LoadMode
) -> None:
    assert LoadModePolicy().mode_for(prompt_chunks, hit_chunks) is expected


def test_a_zero_ratio_loads_every_request_layerwise() -> None:
    policy = LoadModePolicy(min_new_token_ratio=0.0)
    assert policy.mode_for(32, 32) is LoadMode.LAYERWISE


def test_a_larger_ratio_sends_more_requests_async() -> None:
    policy = LoadModePolicy(min_new_token_ratio=0.5)
    assert policy.mode_for(32, 24) is LoadMode.ASYNC
    assert policy.mode_for(48, 32) is LoadMode.LAYERWISE


def test_the_ratio_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="min_new_token_ratio"):
        LoadModePolicy(min_new_token_ratio=-0.1)


@pytest.mark.parametrize(("prompt_chunks", "hit_chunks"), [(-1, 0), (4, -1), (4, 5)])
def test_inconsistent_counts_are_rejected(prompt_chunks: int, hit_chunks: int) -> None:
    with pytest.raises(ValueError, match="invalid chunk counts"):
        LoadModePolicy().mode_for(prompt_chunks, hit_chunks)
