# SPDX-License-Identifier: Apache-2.0
"""Tests for how ``LookupModule`` hands L2 hits to the pipelined retrieve.

The engine context is mocked, except for the pipelined settings and model
registry, so ``lookup()``, ``query_prefetch_status()`` and
``free_lookup_locks()`` run end to end without a GPU or storage.
"""

# Standard
from collections.abc import Sequence
from unittest.mock import MagicMock, patch

# Third Party
import pytest
import torch

# First Party
from lmcache.lmcache_native import Bitmap
from lmcache.v1.distributed.api import (
    NO_L2_DEFERRAL,
    AttnWindowDesc,
    L2Deferral,
    MemoryLayoutDesc,
    ObjectKey,
    PrefetchHandle,
    PrefetchResult,
)
from lmcache.v1.layerwise.deferral import (
    PipelinedDeferral,
    PipelinedFetchConfig,
    PipelinedModel,
)
from lmcache.v1.layerwise.load_mode import LoadModePolicy
from lmcache.v1.layerwise.planner import ModelLayout
from lmcache.v1.layerwise.request_fetch import (
    FetchModel,
    ModelRegistry,
    ObjectToPlace,
    WindowLease,
)
from lmcache.v1.multiprocess.custom_types import (
    IPCCacheServerKey,
    LoadMode,
    PrefetchStatusWithMode,
)
from lmcache.v1.multiprocess.modules.lookup import LookupModule
from lmcache.v1.multiprocess.session import Session
from lmcache.v1.multiprocess.token_hasher import TokenHasher

MODEL = "m"
LAYOUT = MemoryLayoutDesc([torch.Size([2, 1, 16, 512])], [torch.float32])


class _NoPlacer:
    def lease(self, objects: Sequence[ObjectToPlace]) -> WindowLease:
        raise AssertionError("lookup does not lease")


def _pipelined_model() -> PipelinedModel:
    fetch_model = FetchModel(
        ModelLayout.from_registration({0: LAYOUT}, {0: [[0]]}),
        AttnWindowDesc(num_chunks_in_sw=[-1], world_size=1, group_kinds=("attention",)),
    )
    return PipelinedModel(
        fetch_model=fetch_model,
        placer=_NoPlacer(),
        max_record_bytes=1 << 20,
        max_slots=1000,
        adapter_id=0,
        max_chunks=8,
    )


def _ctx(enabled: bool = True, registered: bool = True) -> MagicMock:
    ctx = MagicMock()
    ctx.chunk_size = 16
    ctx.event_bus.has_subscribers.return_value = False
    ctx.layout_desc_registry.find.return_value = LAYOUT
    ctx.layout_desc_registry.find_group_layout_descs.return_value = {0: LAYOUT}
    ctx.layout_desc_registry.find_attn_desc.return_value = AttnWindowDesc(
        num_chunks_in_sw=[-1]
    )
    ctx.token_hasher.compute_chunk_hashes.return_value = [b"c0", b"c1"]
    ctx.pipelined_fetch = PipelinedFetchConfig(enabled=enabled)
    registry: ModelRegistry[PipelinedModel] = ModelRegistry("pipelined fetch setup")
    if registered:
        registry.register(MODEL, 1, _pipelined_model())
    ctx.pipelined_models = registry
    return ctx


def _key(world_size: int = 1, num_kv_readers: int = 1) -> IPCCacheServerKey:
    return IPCCacheServerKey(
        model_name=MODEL,
        world_size=world_size,
        num_kv_readers=num_kv_readers,
        worker_id=None,
        token_ids=tuple(range(32)),
        start=0,
        end=32,
        request_id="req",
    )


def _submitted_deferral(ctx: MagicMock, key: IPCCacheServerKey) -> L2Deferral:
    LookupModule(ctx).lookup(key, tp_size=1)
    ctx.storage_manager.submit_prefetch_task.assert_called_once()
    return ctx.storage_manager.submit_prefetch_task.call_args.args[0].l2_deferral


def _object_key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name=MODEL, kv_rank=0)


def test_an_eligible_lookup_offers_the_pipelined_deferral() -> None:
    deferral = _submitted_deferral(_ctx(), _key())
    assert isinstance(deferral, PipelinedDeferral)


@pytest.mark.parametrize(
    ("ctx_kwargs", "key_kwargs"),
    [
        ({"enabled": False}, {}),
        ({"registered": False}, {}),
        ({}, {"num_kv_readers": 2}),
    ],
    ids=["disabled", "model-not-registered", "several-readers"],
)
def test_an_ineligible_lookup_loads_as_usual(
    ctx_kwargs: dict[str, bool], key_kwargs: dict[str, int]
) -> None:
    deferral = _submitted_deferral(_ctx(**ctx_kwargs), _key(**key_kwargs))
    assert deferral is NO_L2_DEFERRAL


def test_a_lookup_above_world_size_one_loads_as_usual() -> None:
    ctx = _ctx()
    ctx.pipelined_models.register(MODEL, 2, _pipelined_model())
    assert _submitted_deferral(ctx, _key(world_size=2)) is NO_L2_DEFERRAL


def test_the_prefetch_result_records_the_deferred_keys_on_the_session() -> None:
    ctx = _ctx()
    session = Session(request_id="req", hasher=TokenHasher(chunk_size=16))
    ctx.session_manager.get_or_create.return_value = session
    deferred = (_object_key(1),)
    ctx.storage_manager.query_prefetch_outcome.return_value = PrefetchResult(
        Bitmap(2, 2), deferred
    )
    ctx.storage_manager.submit_prefetch_task.return_value = PrefetchHandle(
        prefetch_request_id=0,
        external_request_id="req",
        l1_found_indices=(0,),
        l1_hit_chunks=1,
        total_requested_keys=2,
        submit_time=0.0,
    )
    module = LookupModule(ctx)
    module.lookup(_key(), tp_size=1)

    assert module.query_prefetch_status("req") == 2
    assert session.prefetch_hit_chunks == 2
    assert session.deferred_keys() == set(deferred)


def test_the_deferral_judges_the_load_mode_from_the_lookup_chunks() -> None:
    ctx = _ctx()
    ctx.load_mode_policy = LoadModePolicy(min_new_token_ratio=0.5)
    deferral = _submitted_deferral(ctx, _key())
    hit_one = ObjectKey(chunk_hash=b"c0", model_name=MODEL, kv_rank=0)
    hit_both = ObjectKey(chunk_hash=b"c1", model_name=MODEL, kv_rank=0)
    assert deferral.accepts([0], [hit_one])
    assert not deferral.accepts([0], [hit_one, hit_both])


def _finished_lookup(
    ctx: MagicMock, deferred: tuple[ObjectKey, ...], found: Bitmap
) -> LookupModule:
    ctx.session_manager.get_or_create.return_value = Session(
        request_id="req", hasher=TokenHasher(chunk_size=16)
    )
    ctx.storage_manager.query_prefetch_outcome.return_value = PrefetchResult(
        found, deferred
    )
    ctx.storage_manager.submit_prefetch_task.return_value = PrefetchHandle(
        prefetch_request_id=0,
        external_request_id="req",
        l1_found_indices=(),
        l1_hit_chunks=0,
        total_requested_keys=2,
        submit_time=0.0,
    )
    module = LookupModule(ctx)
    module.lookup(_key(), tp_size=1)
    return module


def test_a_lookup_that_deferred_loads_layerwise() -> None:
    ctx = _ctx()
    ctx.load_mode_for.return_value = LoadMode.ASYNC
    module = _finished_lookup(ctx, (_object_key(1),), Bitmap(2, 2))

    status = module.query_prefetch_status_with_mode("req")

    assert status == PrefetchStatusWithMode(
        found_chunks=2, load_mode=LoadMode.LAYERWISE
    )
    ctx.load_mode_for.assert_not_called()


@pytest.mark.parametrize("mode", [LoadMode.ASYNC, LoadMode.LAYERWISE])
def test_a_lookup_that_deferred_nothing_takes_the_policy_mode(mode: LoadMode) -> None:
    ctx = _ctx()
    ctx.load_mode_for.return_value = mode
    module = _finished_lookup(ctx, (), Bitmap(2, 2))

    status = module.query_prefetch_status_with_mode("req")

    assert status == PrefetchStatusWithMode(found_chunks=2, load_mode=mode)
    # Two chunk hashes of 16 tokens, both hit.
    ctx.load_mode_for.assert_called_once_with(2, 2)


def test_the_mode_query_consumes_the_job_like_the_plain_query() -> None:
    ctx = _ctx()
    ctx.load_mode_for.return_value = LoadMode.ASYNC
    module = _finished_lookup(ctx, (), Bitmap(2, 2))

    assert module.query_prefetch_status_with_mode("req") is not None
    # Exactly once: a second poll finds no job and reports no hit.
    assert module.query_prefetch_status("req") == 0


def test_the_mode_query_waits_for_the_prefetch() -> None:
    ctx = _ctx()
    module = _finished_lookup(ctx, (), Bitmap(2, 2))
    ctx.storage_manager.query_prefetch_outcome.return_value = None

    assert module.query_prefetch_status_with_mode("req") is None


def test_freeing_lookup_locks_skips_the_deferred_keys() -> None:
    ctx = _ctx()
    session = Session(request_id="req", hasher=TokenHasher(chunk_size=16))
    session.record_prefetch_result(2, (0,), (_object_key(1),))
    ctx.session_manager.get_or_create.return_value = session
    locked = [_object_key(0), _object_key(1)]

    with patch(
        "lmcache.v1.multiprocess.modules.lookup.ipc_key_to_object_keys",
        return_value=[locked],
    ):
        LookupModule(ctx).free_lookup_locks(_key(), 1)

    ctx.storage_manager.finish_read_prefetched.assert_called_once_with(
        [_object_key(0)], read_locks=1
    )
