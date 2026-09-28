# SPDX-License-Identifier: Apache-2.0
"""How ``LMCacheDrivenTransferModule.retrieve`` serves keys the lookup deferred.

The lookup left the last chunk of each group in L2. Retrieve must read only
the rest from L1, fetch the deferred keys layer by layer (or load them whole
when it cannot), and release every lock it holds after the copies.
"""

# Standard
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ResidentKeys
from lmcache.v1.layerwise.deferral import PipelinedFetchConfig
from lmcache.v1.mp_observability.event import EventType
from lmcache.v1.multiprocess import pipelined_loading
from lmcache.v1.multiprocess.modules import lmcache_driven_transfer as mod
from lmcache.v1.multiprocess.pipelined_loading import (
    DeferredFetchResult,
    DeferredLoad,
    PipelinedLoadRequest,
    PipelinedOutcome,
    PipelinedSinkFactory,
)

NUM_GROUPS = 2
NUM_CHUNKS = 3
CHUNK_SIZE = 256
KEYS = [[f"g{g}c{c}" for c in range(NUM_CHUNKS)] for g in range(NUM_GROUPS)]
DEFERRED = [KEYS[g][NUM_CHUNKS - 1] for g in range(NUM_GROUPS)]
L1_KEYS = [KEYS[g][c] for g in range(NUM_GROUPS) for c in range(NUM_CHUNKS - 1)]


def _obj(name: str) -> MagicMock:
    obj = MagicMock(get_size=MagicMock(return_value=10))
    obj.name = name
    return obj


class _Session:
    """Hands out its deferred keys once, like the real session."""

    def __init__(self, deferred: Iterable[str]) -> None:
        self._deferred = set(deferred)

    def claim_deferred_keys(self, keys: Iterable[str]) -> list[str]:
        claimed = [k for k in keys if k in self._deferred]
        self._deferred.difference_update(claimed)
        return claimed


class _SinkFactory:
    def build(self, request: PipelinedLoadRequest) -> MagicMock:
        raise AssertionError("fetch_deferred_objects is replaced in these tests")


@dataclass
class _Harness:
    module: mod.LMCacheDrivenTransferModule
    storage: MagicMock
    reads: list[list[str]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    fetches: list[tuple[tuple[str, ...], list[list[object]]]] = field(
        default_factory=list
    )
    layerwise_transfers: list[list[list[object]]] = field(default_factory=list)
    group_transfers: list[tuple[int, list[object]]] = field(default_factory=list)
    retrieve_end: list[dict[str, object]] = field(default_factory=list)

    def outcome(self) -> object:
        """The ``pipelined_outcome`` of the one retrieve run."""
        [end] = self.retrieve_end
        return end["pipelined_outcome"]

    def retrieve(self) -> bool:
        _handle, ok = self.module.retrieve(
            key=SimpleNamespace(
                request_id="req", cache_salt="salt", world_size=1, worker_id=0
            ),
            instance_id=1,
            gpu_block_ids=[[1, 2, 3] for _ in range(NUM_GROUPS)],
            event_ipc_handle=b"x",
            skip_first_n_tokens=0,
            retrieve_generation=1,
        )
        return ok


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deferred: Iterable[str] = DEFERRED,
    layerwise: bool = True,
    delivery: DeferredLoad = DeferredLoad.PIPELINED,
    resident: Iterable[str] = (),
    loaded: Iterable[str] | None = None,
    sink_factory: PipelinedSinkFactory | None = None,
    missing_from_l1: Iterable[str] = (),
    busy: Iterable[str] = (),
) -> _Harness:
    """Build a module over mocks, with ``deferred`` left in L2 by the lookup.

    ``resident`` are deferred keys another fetch already put in L1;
    ``loaded`` are the keys a whole load finds (default: all of them).
    ``missing_from_l1`` are keys the L1 read cannot find, and ``busy`` are
    deferred keys another request is still fetching.
    """
    monkeypatch.setattr(mod, "DeviceHostFuncDispatcher", MagicMock())
    monkeypatch.setattr(mod, "downsample_and_stage_block_ids", lambda cc, b: b)
    monkeypatch.setattr(mod, "torch_dev", MagicMock())

    ctx = MagicMock()
    ctx.chunk_size = CHUNK_SIZE
    ctx.use_layerwise = layerwise
    ctx.resolve_obj_keys.return_value = KEYS
    ctx.pipelined_fetch = PipelinedFetchConfig(enabled=True)
    ctx.session_manager.get.return_value = _Session(deferred)
    storage = ctx.storage_manager

    factory = _SinkFactory() if sink_factory is None else sink_factory
    module = mod.LMCacheDrivenTransferModule(ctx, pipelined_sink_factory=factory)
    h = _Harness(module=module, storage=storage)

    kvlgm = SimpleNamespace(
        num_object_groups=NUM_GROUPS,
        num_kernel_groups=NUM_GROUPS,
        get_attn_desc=lambda: SimpleNamespace(
            num_chunks_in_sw=[-1] * NUM_GROUPS, group_kinds=()
        ),
    )
    cache_context = MagicMock(kv_layer_groups_manager=kvlgm, max_batch_size=8)
    cache_context.calculate_num_blocks.return_value = 1
    entry = SimpleNamespace(
        cache_context=cache_context,
        model_name="m",
        event_backend=MagicMock(),
        layerwise_schedule=MagicMock() if layerwise else None,
        layer_progress=MagicMock() if layerwise else None,
        daemon_layer_event_pool=MagicMock() if layerwise else None,
    )
    monkeypatch.setattr(module, "get_and_touch_context_entry", lambda _id: entry)

    missing = set(missing_from_l1)

    @contextmanager
    def read(keys: list[str]) -> Iterator[list[MagicMock]]:
        h.reads.append(list(keys))
        yield [_obj(k) for k in keys if k not in missing]

    storage.read_prefetched_results.side_effect = read

    resident_set = set(resident)
    busy_set = set(busy)

    def lock_resident(keys: list[str]) -> ResidentKeys:
        locked = {k: _obj(k) for k in keys if k in resident_set}
        in_flight = tuple(k for k in keys if k in busy_set)
        absent = tuple(k for k in keys if k not in resident_set and k not in busy_set)
        return ResidentKeys(locked, in_flight, absent)  # type: ignore[arg-type]

    storage.lock_resident_keys.side_effect = lock_resident
    found = None if loaded is None else set(loaded)
    storage.load_into_l1.side_effect = lambda keys, layouts, timeout: {
        k: _obj(k) for k in keys if found is None or k in found
    }

    def fetch(
        storage_arg, model, keys_per_group, keys_to_fetch, factory_arg, request, layouts
    ):
        h.fetches.append((tuple(keys_to_fetch), request.objects.by_group()))
        if delivery is DeferredLoad.PIPELINED:
            return DeferredFetchResult(PipelinedOutcome.PIPELINED, ())
        placed = {}
        for g, group in enumerate(keys_per_group):
            for c, k in enumerate(group):
                if k in keys_to_fetch:
                    placed[(g, c)] = _obj(k)
        request.objects.put(placed)
        return DeferredFetchResult(PipelinedOutcome.REFUSED, tuple(keys_to_fetch))

    monkeypatch.setattr(mod, "fetch_deferred_objects", fetch)

    def layerwise_transfer(cc, block_ids, objs_by_group, *args, **kwargs):
        h.layerwise_transfers.append([list(g) for g in objs_by_group])

    monkeypatch.setattr(mod, "transfer_kv_layerwise_h2d", layerwise_transfer)

    def group_transfer(cc, block_ids, objs, object_group_id, **kwargs):
        h.group_transfers.append((object_group_id, list(objs)))

    monkeypatch.setattr(mod, "transfer_kv_per_object_group", group_transfer)

    def submit(stream, kind, payload):
        if kind == "finish_read_prefetched":
            h.released.extend(payload)

    monkeypatch.setattr(mod, "submit_callback_to_stream", submit)

    def publish_on_stream(stream, event):
        if event.event_type == EventType.MP_RETRIEVE_END:
            h.retrieve_end.append(event.metadata)

    ctx.event_bus.publish_on_stream.side_effect = publish_on_stream
    return h


def _names(objs: list[object]) -> list[str | None]:
    return [None if o is None else cast(MagicMock, o).name for o in objs]


def test_a_pipelined_retrieve_reads_only_l1_and_fetches_the_deferred(monkeypatch):
    h = _harness(monkeypatch)

    assert h.retrieve() is True

    assert h.reads == [KEYS[g][: NUM_CHUNKS - 1] for g in range(NUM_GROUPS)]
    [(keys_to_fetch, table)] = h.fetches
    assert keys_to_fetch == tuple(DEFERRED)
    # The loader sees the L1 objects in place and a gap for each deferred one.
    assert [_names(g) for g in table] == [
        KEYS[g][: NUM_CHUNKS - 1] + [None] for g in range(NUM_GROUPS)
    ]
    assert h.layerwise_transfers == []
    assert sorted(h.released) == sorted(L1_KEYS)
    [end] = h.retrieve_end
    assert end["retrieved_count"] == NUM_GROUPS * NUM_CHUNKS
    assert end["num_tokens"] == NUM_CHUNKS * CHUNK_SIZE
    assert end["pipelined_outcome"] == "pipelined"
    assert end["deferred_count"] == len(DEFERRED)


def test_a_whole_delivery_runs_the_layerwise_transfer_over_every_object(
    monkeypatch,
):
    h = _harness(monkeypatch, delivery=DeferredLoad.WHOLE)

    assert h.retrieve() is True

    [transferred] = h.layerwise_transfers
    assert [_names(g) for g in transferred] == KEYS
    assert sorted(h.released) == sorted(L1_KEYS + DEFERRED)
    assert h.retrieve_end[0]["num_tokens"] == NUM_CHUNKS * CHUNK_SIZE
    assert h.retrieve_end[0]["pipelined_outcome"] == "refused"


def test_a_key_another_fetch_left_in_l1_is_read_not_fetched(monkeypatch):
    reused = DEFERRED[0]
    h = _harness(monkeypatch, resident=[reused])

    assert h.retrieve() is True

    [(keys_to_fetch, _table)] = h.fetches
    assert keys_to_fetch == tuple(DEFERRED[1:])
    assert reused in h.reads[0]
    assert sorted(h.released) == sorted(L1_KEYS + [reused])


def test_a_retrieve_that_is_not_layerwise_loads_the_deferred_keys_whole(
    monkeypatch,
):
    h = _harness(monkeypatch, layerwise=False)

    assert h.retrieve() is True

    [call] = h.storage.load_into_l1.call_args_list
    assert call.args[0] == DEFERRED
    assert call.args[2] == pipelined_loading.WHOLE_LOAD_TIMEOUT_SECONDS
    assert h.fetches == []
    assert h.reads == KEYS
    assert [(g, _names(objs)) for g, objs in h.group_transfers] == [
        (g, KEYS[g]) for g in range(NUM_GROUPS)
    ]
    assert sorted(h.released) == sorted(L1_KEYS + DEFERRED)
    assert h.outcome() == "loaded_whole"


def test_a_whole_load_missing_a_key_fails_and_unlocks_what_it_loaded(
    monkeypatch,
):
    h = _harness(monkeypatch, layerwise=False, loaded=DEFERRED[:1])

    assert h.retrieve() is False

    h.storage.finish_read_prefetched.assert_called_once_with(DEFERRED[:1])
    assert h.reads == []
    assert h.group_transfers == []
    assert h.outcome() == "failed"


def test_a_whole_load_that_raises_unlocks_the_reused_keys(monkeypatch):
    reused = DEFERRED[0]
    h = _harness(monkeypatch, layerwise=False, resident=[reused])
    h.storage.load_into_l1.side_effect = TimeoutError("slow")

    assert h.retrieve() is False

    h.storage.finish_read_prefetched.assert_called_once_with([reused])


def test_without_a_sink_factory_deferred_keys_are_not_claimed(monkeypatch):
    h = _harness(monkeypatch, sink_factory=pipelined_loading.NO_PIPELINED_SINK_FACTORY)

    assert h.retrieve() is True

    assert h.reads == KEYS
    assert h.fetches == []
    h.storage.lock_resident_keys.assert_not_called()
    assert h.outcome() == "not_deferred"
    assert h.retrieve_end[0]["deferred_count"] == 0
    [transferred] = h.layerwise_transfers
    assert [_names(g) for g in transferred] == KEYS


def test_a_retrieve_that_fails_before_reading_a_reused_key_still_unlocks_it(
    monkeypatch,
):
    reused = DEFERRED[0]
    h = _harness(monkeypatch, resident=[reused], missing_from_l1=[KEYS[0][0]])

    assert h.retrieve() is False

    # Group 0's read failed, so nothing was read, but the reused key's lock
    # (taken by this retrieve) is released after the stream work.
    assert h.released == [reused]
    assert h.fetches == []


def test_every_deferred_key_already_in_l1_is_reported_as_reused(monkeypatch):
    h = _harness(monkeypatch, resident=DEFERRED)

    assert h.retrieve() is True

    assert h.fetches == []
    assert h.outcome() == "reused"


def test_a_busy_shared_key_under_recompute_is_reported(monkeypatch):
    h = _harness(monkeypatch, busy=DEFERRED[:1])

    assert h.retrieve() is False

    assert h.fetches == []
    assert h.outcome() == "shared_keys_busy"
