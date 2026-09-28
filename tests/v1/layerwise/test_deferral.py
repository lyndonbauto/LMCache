# SPDX-License-Identifier: Apache-2.0
"""Tests for the lookup-time pipelined deferral and its settings."""

# Standard
from collections.abc import Sequence

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import AttnWindowDesc, MemoryLayoutDesc, ObjectKey
from lmcache.v1.layerwise.deferral import (
    PipelinedDeferral,
    PipelinedFetchConfig,
    PipelinedModel,
    SharedKeyPolicy,
)
from lmcache.v1.layerwise.planner import ModelLayout
from lmcache.v1.layerwise.pump import DEFAULT_LAYER_TIMEOUT_SECONDS
from lmcache.v1.layerwise.request_fetch import FetchModel, ObjectToPlace, WindowLease

ADAPTER_ID = 3
# Two layers of (K/V, 16 tokens, 512) float32: 64 KiB per layer.
LAYOUT = MemoryLayoutDesc([torch.Size([2, 2, 16, 512])], [torch.float32])
MAX_RECORD_BYTES = 1 << 20


class _NoPlacer:
    def lease(self, objects: Sequence[ObjectToPlace]) -> WindowLease:
        raise AssertionError("the deferral does not lease")


def _fetch_model() -> FetchModel:
    return FetchModel(
        ModelLayout.from_registration({0: LAYOUT}, {0: [[0, 1]]}),
        AttnWindowDesc(num_chunks_in_sw=[-1], world_size=1, group_kinds=("attention",)),
    )


def _deferral(max_chunks: int = 4, max_slots: int = 1000) -> PipelinedDeferral:
    return PipelinedDeferral(
        PipelinedModel(
            fetch_model=_fetch_model(),
            placer=_NoPlacer(),
            max_record_bytes=MAX_RECORD_BYTES,
            max_slots=max_slots,
            adapter_id=ADAPTER_ID,
            max_chunks=max_chunks,
        )
    )


def _key(chunk: int, group: int = 0) -> ObjectKey:
    return ObjectKey(
        chunk_hash=ObjectKey.IntHash2Bytes(chunk),
        model_name="m",
        kv_rank=0,
        object_group_id=group,
    )


def _slots_per_object() -> int:
    return _fetch_model().layout.record_count(0, MAX_RECORD_BYTES)


def test_a_small_load_from_the_pipelined_adapter_is_deferred() -> None:
    assert _deferral().accepts([ADAPTER_ID], [_key(0), _key(1)])


@pytest.mark.parametrize("adapter_ids", [[ADAPTER_ID + 1], [ADAPTER_ID, 0], []])
def test_a_load_from_any_other_adapter_is_not_deferred(adapter_ids: list[int]) -> None:
    assert not _deferral().accepts(adapter_ids, [_key(0)])


def test_an_empty_load_is_not_deferred() -> None:
    assert not _deferral().accepts([ADAPTER_ID], [])


def test_the_chunk_cap_counts_chunks_not_keys() -> None:
    deferral = _deferral(max_chunks=2)
    assert deferral.accepts([ADAPTER_ID], [_key(0), _key(1)])
    assert not deferral.accepts([ADAPTER_ID], [_key(0), _key(1), _key(2)])


def test_the_slot_cap_counts_every_record() -> None:
    per_object = _slots_per_object()
    keys = [_key(0), _key(1)]
    assert _deferral(max_slots=2 * per_object).accepts([ADAPTER_ID], keys)
    assert not _deferral(max_slots=2 * per_object - 1).accepts([ADAPTER_ID], keys)


def test_a_key_outside_the_layout_is_not_deferred() -> None:
    assert not _deferral().accepts([ADAPTER_ID], [_key(0, group=7)])


def test_the_defaults_recompute_on_shared_keys() -> None:
    config = PipelinedFetchConfig()
    assert not config.enabled
    assert config.shared_keys is SharedKeyPolicy.RECOMPUTE


@pytest.mark.parametrize("max_chunks", [0, -1])
def test_the_chunk_cap_must_be_positive(max_chunks: int) -> None:
    with pytest.raises(ValueError, match="max_chunks"):
        PipelinedFetchConfig(max_chunks=max_chunks)


@pytest.mark.parametrize("seconds", [-0.1, DEFAULT_LAYER_TIMEOUT_SECONDS])
def test_the_shared_key_wait_must_be_below_the_layer_timeout(seconds: float) -> None:
    with pytest.raises(ValueError, match="shared_wait_seconds"):
        PipelinedFetchConfig(
            shared_keys=SharedKeyPolicy.WAIT, shared_wait_seconds=seconds
        )
