# SPDX-License-Identifier: Apache-2.0
"""``LMCacheDrivenTransferModule.retrieve`` with ``--use-layerwise`` on.

A layerwise retrieve reads every object from L1 and hands them all to one
``transfer_kv_layerwise_h2d`` call, which publishes progress layer by layer
under the worker's retrieve generation. When the retrieve fails before any
layer copy is queued, the module itself must publish the failure for that
generation, or the worker's per-layer waits never return.

No pipelined sink factory is installed, so no key is deferred: this is the
plain layerwise path. Kernels, streams and the progress record are mocked.
"""

# Standard
from collections.abc import Iterator
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock
import threading
import time

# Third Party
import pytest

# First Party
from lmcache.v1.layerwise.deferral import PipelinedFetchConfig
from lmcache.v1.multiprocess.deferred_response import DeferredResponse
from lmcache.v1.multiprocess.modules import lmcache_driven_transfer as mod
from lmcache.v1.multiprocess.retrieve_sequencer import RetrieveLaunchSequencer
from lmcache.v1.platform.base.transfer_gate import TransferGate

NUM_GROUPS = 2
NUM_CHUNKS = 3
GENERATION = 7
JOIN_SECONDS = 5.0
KEYS = [[f"g{g}c{c}" for c in range(NUM_CHUNKS)] for g in range(NUM_GROUPS)]
ALL_KEYS = [k for group in KEYS for k in group]


@dataclass
class _LayerwiseCall:
    """The arguments of one ``transfer_kv_layerwise_h2d`` call."""

    objects_by_group: list[list[str]]
    schedule: object
    sequencer: object
    generation: int


@dataclass
class _Harness:
    module: mod.LMCacheDrivenTransferModule
    entry: SimpleNamespace
    layerwise_calls: list[_LayerwiseCall] = field(default_factory=list)
    group_transfers: list[int] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def retrieve(self, generation: int = GENERATION) -> bool:
        _handle, ok = self.start_retrieve(generation).result(timeout=JOIN_SECONDS)
        return ok

    def start_retrieve(
        self, generation: int = GENERATION
    ) -> DeferredResponse[tuple[bytes, bool]]:
        return self.module.start_retrieve(
            key=SimpleNamespace(
                request_id="req", cache_salt="salt", world_size=1, worker_id=0
            ),
            instance_id=1,
            gpu_block_ids=[[1, 2, 3] for _ in range(NUM_GROUPS)],
            event_ipc_handle=b"x",
            skip_first_n_tokens=0,
            retrieve_generation=generation,
        )

    @property
    def progress(self) -> MagicMock:
        return self.entry.layer_progress


def _obj(name: str) -> MagicMock:
    obj = MagicMock(get_size=MagicMock(return_value=10))
    obj.name = name
    return obj


@pytest.fixture
def make_harness(monkeypatch: pytest.MonkeyPatch):
    """Build a layerwise module over mocks; ``missing`` keys are not in L1.

    ``windows`` is how many RDMA windows the storage manager reports.
    """

    def build(missing: frozenset[str] = frozenset(), windows: int = 0) -> _Harness:
        monkeypatch.setattr(mod, "DeviceHostFuncDispatcher", MagicMock())
        monkeypatch.setattr(mod, "downsample_and_stage_block_ids", lambda cc, b: b)
        monkeypatch.setattr(
            mod, "downsample_and_stage_owned_block_ids", lambda cc, b: b
        )
        monkeypatch.setattr(mod, "torch_dev", MagicMock())

        ctx = MagicMock()
        ctx.chunk_size = 256
        ctx.use_layerwise = True
        ctx.resolve_obj_keys.return_value = KEYS
        ctx.pipelined_fetch = PipelinedFetchConfig()
        ctx.storage_manager.pipelined_window_count.return_value = windows
        module = mod.LMCacheDrivenTransferModule(ctx)

        kvlgm = SimpleNamespace(
            num_object_groups=NUM_GROUPS,
            num_kernel_groups=NUM_GROUPS,
            get_attn_desc=lambda: SimpleNamespace(
                num_chunks_in_sw=[-1] * NUM_GROUPS, group_kinds=()
            ),
        )
        cache_context = MagicMock(kv_layer_groups_manager=kvlgm, max_batch_size=8)
        cache_context.calculate_num_blocks.return_value = 1
        progress = MagicMock()
        event_pool = MagicMock()
        entry = SimpleNamespace(
            cache_context=cache_context,
            model_name="m",
            world_size=1,
            event_backend=MagicMock(),
            layerwise_schedule=MagicMock(),
            layer_progress=progress,
            layer_progress_shm=None,
            daemon_layer_event_pool=event_pool,
            retrieve_sequencer=RetrieveLaunchSequencer(
                cache_context.stream, progress, event_pool, TransferGate()
            ),
            inflight_retrieves=mod.InflightRetrieves(),
        )
        monkeypatch.setattr(module, "get_and_touch_context_entry", lambda _id: entry)
        h = _Harness(module=module, entry=entry)

        @contextmanager
        def read(keys: list[str]) -> Iterator[list[MagicMock]]:
            yield [_obj(k) for k in keys if k not in missing]

        ctx.storage_manager.read_prefetched_results.side_effect = read

        def layerwise_transfer(
            cc, block_ids, objs_by_group, skip, schedule, sequencer, gen, **kw
        ):
            h.layerwise_calls.append(
                _LayerwiseCall(
                    objects_by_group=[[o.name for o in g] for g in objs_by_group],
                    schedule=schedule,
                    sequencer=sequencer,
                    generation=gen,
                )
            )
            # A real transfer completes the retrieve once its last layer is queued.
            sequencer.complete(gen)

        monkeypatch.setattr(mod, "transfer_kv_layerwise_h2d", layerwise_transfer)

        def group_transfer(cc, block_ids, objs, object_group_id, **kwargs):
            h.group_transfers.append(object_group_id)

        monkeypatch.setattr(mod, "transfer_kv_per_object_group", group_transfer)

        def submit(stream, kind, payload):
            if kind == "finish_read_prefetched":
                h.released.extend(payload)

        monkeypatch.setattr(mod, "submit_callback_to_stream", submit)
        return h

    return build


def test_one_layerwise_transfer_carries_every_object_and_the_generation(
    make_harness,
) -> None:
    h = make_harness()

    assert h.retrieve() is True

    [call] = h.layerwise_calls
    assert call.objects_by_group == KEYS
    assert call.schedule is h.entry.layerwise_schedule
    assert call.sequencer is h.entry.retrieve_sequencer
    assert call.generation == GENERATION
    assert h.group_transfers == []
    h.progress.fail_retrieve.assert_not_called()
    assert sorted(h.released) == sorted(ALL_KEYS)


def test_a_key_missing_from_l1_publishes_the_failure_for_this_generation(
    make_harness,
) -> None:
    h = make_harness(missing=frozenset({KEYS[1][0]}))

    assert h.retrieve() is False

    assert h.layerwise_calls == []
    assert h.group_transfers == []
    h.progress.fail_retrieve.assert_called_once_with(GENERATION)


def test_a_transfer_that_raises_publishes_the_failure_for_this_generation(
    make_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness()

    def broken_transfer(*args, **kwargs):
        raise RuntimeError("kernel launch failed")

    monkeypatch.setattr(mod, "transfer_kv_layerwise_h2d", broken_transfer)

    assert h.retrieve() is False

    h.progress.fail_retrieve.assert_called_once_with(GENERATION)
    assert sorted(h.released) == sorted(ALL_KEYS)


def test_a_later_retrieve_is_answered_while_an_earlier_one_still_loads(
    make_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler hands layer loading off, so one worker's retrieves overlap
    instead of queueing behind each other on its request thread."""
    h = make_harness()
    first_loading = threading.Event()
    finish_first = threading.Event()
    owned_stagings: list[object] = []

    def stage_owned(cc, block_ids):
        owned_stagings.append(block_ids)
        return block_ids

    def transfer(cc, block_ids, objs_by_group, skip, schedule, sequencer, gen, **kw):
        if gen == GENERATION:
            first_loading.set()
            finish_first.wait(JOIN_SECONDS)
        sequencer.complete(gen)

    monkeypatch.setattr(mod, "downsample_and_stage_owned_block_ids", stage_owned)
    monkeypatch.setattr(mod, "transfer_kv_layerwise_h2d", transfer)

    first = h.start_retrieve(GENERATION)
    assert first_loading.wait(JOIN_SECONDS)
    second = h.start_retrieve(GENERATION + 1)

    assert second.result(timeout=JOIN_SECONDS)[1] is True
    with pytest.raises(FutureTimeoutError):
        first.result(timeout=0.2)
    finish_first.set()
    assert first.result(timeout=JOIN_SECONDS)[1] is True
    # Each retrieve stages its own block IDs; the shared buffer is rewritten
    # by every later transfer while it loads.
    assert len(owned_stagings) == 2
    h.progress.fail_retrieve.assert_not_called()


def _blocking_transfer(
    release: threading.Event, started: list[int], lock: threading.Lock
):
    """A layerwise transfer that records its generation, then waits for
    ``release`` before completing."""

    def transfer(cc, block_ids, objs_by_group, skip, schedule, sequencer, gen, **kw):
        with lock:
            started.append(gen)
        release.wait(JOIN_SECONDS)
        sequencer.complete(gen)

    return transfer


def _wait_for(predicate, timeout: float = JOIN_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_retrieves_beyond_the_window_count_wait_for_a_thread(
    make_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pool thread per RDMA window: a retrieve past the window count
    waits for a running one to finish instead of loading at once, where it
    would be refused a window and fall back to a whole load."""
    h = make_harness(windows=2)
    release, started, lock = threading.Event(), [], threading.Lock()
    monkeypatch.setattr(
        mod, "transfer_kv_layerwise_h2d", _blocking_transfer(release, started, lock)
    )

    futures = [h.start_retrieve(GENERATION + i) for i in range(3)]

    assert _wait_for(lambda: len(started) == 2)
    with pytest.raises(FutureTimeoutError):
        futures[2].result(timeout=0.2)
    assert sorted(started) == [GENERATION, GENERATION + 1]
    release.set()
    assert [f.result(timeout=JOIN_SECONDS)[1] for f in futures] == [True] * 3
    assert started[2] == GENERATION + 2
    h.progress.fail_retrieve.assert_not_called()


def test_without_rdma_windows_retrieves_load_at_once(
    make_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness(windows=0)
    release, started, lock = threading.Event(), [], threading.Lock()
    monkeypatch.setattr(
        mod, "transfer_kv_layerwise_h2d", _blocking_transfer(release, started, lock)
    )

    futures = [h.start_retrieve(GENERATION + i) for i in range(3)]

    assert _wait_for(lambda: len(started) == 3)
    release.set()
    assert [f.result(timeout=JOIN_SECONDS)[1] for f in futures] == [True] * 3


def test_unregistering_a_worker_stops_its_loading_retrieve_and_waits_for_it(
    make_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-28: the worker's KV cache is released only after its retrieve on the
    pool has stopped, so no copy lands in released memory."""
    h = make_harness()
    loading = threading.Event()
    done_when_closed: list[bool] = []

    def transfer(cc, block_ids, objs_by_group, skip, schedule, sequencer, gen, **kw):
        sequencer.begin(gen)
        loading.set()
        # Stand-in for waiting on the next layer: the stop shows at its launch.
        assert _wait_for(lambda: h.progress.fail_retrieve.called)
        sequencer.launch(gen, 0, lambda slots: None)

    monkeypatch.setattr(mod, "transfer_kv_layerwise_h2d", transfer)
    first = h.start_retrieve(GENERATION)
    assert loading.wait(JOIN_SECONDS)

    def close() -> None:
        try:
            first.result(timeout=0)
        except FutureTimeoutError:
            done_when_closed.append(False)
        else:
            done_when_closed.append(True)

    h.entry.cache_context.close.side_effect = close
    monkeypatch.setitem(h.module._cache_contexts, 1, h.entry)

    h.module.unregister_kv_cache(1)

    assert done_when_closed == [True]
    assert first.result(timeout=0)[1] is False
    h.progress.fail_retrieve.assert_called_with(GENERATION)


def test_unregistering_an_idle_worker_does_not_publish_a_failure(
    make_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness()
    assert h.retrieve() is True
    monkeypatch.setitem(h.module._cache_contexts, 1, h.entry)

    h.module.unregister_kv_cache(1)

    h.entry.cache_context.close.assert_called_once_with()
    h.progress.fail_retrieve.assert_not_called()


def test_a_layerwise_retrieve_without_a_generation_is_refused(make_harness) -> None:
    """Generation 0 means "not layerwise"; no copy runs under it."""
    h = make_harness()

    assert h.retrieve(generation=0) is False

    assert h.layerwise_calls == []
    assert h.group_transfers == []
