# SPDX-License-Identifier: Apache-2.0
"""Tests for the prefetch controller's deferred L2 mode.

A request whose ``l2_deferral`` accepts is reported as found without its L2
hits being loaded; the keys come back in ``PrefetchResult.deferred_keys``.
See ``docs/design/v1/layerwise/c9-wiring.md``, "Lookup: deciding to defer".

Runs on CPU: a small unpinned L1 and ``MockL2Adapter``.
"""

# Standard
from collections.abc import Iterator, Sequence
import time

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import (
    MemoryLayoutDesc,
    ObjectKey,
    PrefetchMode,
    PrefetchRequestSpec,
    PrefetchResult,
)
from lmcache.v1.distributed.config import L1ManagerConfig, L1MemoryManagerConfig
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.base import L2TaskId
from lmcache.v1.distributed.l2_adapters.mock_l2_adapter import (
    MockL2Adapter,
    MockL2AdapterConfig,
)
from lmcache.v1.distributed.storage_controllers.prefetch_controller import (
    PrefetchController,
)
from lmcache.v1.distributed.storage_controllers.prefetch_policy import (
    DefaultPrefetchPolicy,
)
from lmcache.v1.distributed.storage_controllers.store_policy import (
    AdapterDescriptor,
)
from lmcache.v1.memory_management import MemoryObj, MemoryObjMetadata, TensorMemoryObj

LAYOUT = MemoryLayoutDesc(shapes=[torch.Size([16, 256])], dtypes=[torch.float32])


class _CountingAdapter(MockL2Adapter):
    """A mock adapter that counts the load tasks submitted to it."""

    def __init__(self) -> None:
        super().__init__(MockL2AdapterConfig(max_size_gb=0.01, mock_bandwidth_gb=10.0))
        self.load_calls = 0

    def submit_load_task(
        self, keys: list[ObjectKey], objects: list[MemoryObj]
    ) -> L2TaskId:
        self.load_calls += 1
        return super().submit_load_task(keys, objects)


class _RecordingDeferral:
    """An ``L2Deferral`` returning a fixed answer and recording its calls."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[list[int], list[ObjectKey]]] = []

    def accepts(self, adapter_ids: Sequence[int], keys: Sequence[ObjectKey]) -> bool:
        self.calls.append((list(adapter_ids), list(keys)))
        return self.answer


def _key(chunk: int) -> ObjectKey:
    return ObjectKey(
        chunk_hash=ObjectKey.IntHash2Bytes(chunk), model_name="m", kv_rank=0
    )


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _store_in_l2(adapter: MockL2Adapter, keys: list[ObjectKey]) -> None:
    objs: list[MemoryObj] = []
    for _ in keys:
        tensor = torch.zeros(LAYOUT.shapes[0], dtype=LAYOUT.dtypes[0])
        meta = MemoryObjMetadata(
            shape=LAYOUT.shapes[0],
            dtype=LAYOUT.dtypes[0],
            address=0,
            phy_size=tensor.nelement() * tensor.element_size(),
            ref_count=0,
        )
        objs.append(
            TensorMemoryObj(raw_data=tensor, metadata=meta, parent_allocator=None)
        )
    adapter.submit_store_task(keys, objs)
    assert _wait(lambda: all(adapter.debug_has_key(k) for k in keys))


def _put_in_l1(l1: L1Manager, keys: list[ObjectKey]) -> None:
    reserved = l1.reserve_write(
        keys=keys, is_temporary=[False] * len(keys), layout_desc=LAYOUT, mode="new"
    )
    assert all(err == L1Error.SUCCESS for err, _ in reserved.values())
    l1.finish_write(keys)


def _outcome(ctrl: PrefetchController, request_id: int) -> PrefetchResult:
    box: list[PrefetchResult] = []

    def ready() -> bool:
        outcome = ctrl.query_prefetch_outcome(request_id)
        if outcome is not None:
            box.append(outcome)
        return bool(box)

    assert _wait(ready), "prefetch did not complete"
    return box[0]


@pytest.fixture
def l1() -> Iterator[L1Manager]:
    manager = L1Manager(
        L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(
                size_in_bytes=8 << 20,
                use_lazy=False,
                align_bytes=4096,
                shm_name="",
            ),
            write_ttl_seconds=600,
            read_ttl_seconds=300,
        )
    )
    yield manager
    manager.close()


@pytest.fixture
def adapter() -> Iterator[_CountingAdapter]:
    counting = _CountingAdapter()
    yield counting
    counting.close()


@pytest.fixture
def controller(
    l1: L1Manager, adapter: _CountingAdapter
) -> Iterator[PrefetchController]:
    ctrl = PrefetchController(
        l1_manager=l1,
        l2_adapters=[adapter],
        adapter_descriptors=[
            AdapterDescriptor(
                index=0,
                config=MockL2AdapterConfig(max_size_gb=0.01, mock_bandwidth_gb=10.0),
            )
        ],
        policy=DefaultPrefetchPolicy(),
    )
    ctrl.start()
    yield ctrl
    ctrl.stop()


def test_accepted_deferral_reports_hits_without_loading(
    l1: L1Manager, adapter: _CountingAdapter, controller: PrefetchController
) -> None:
    keys = [_key(i) for i in range(4)]
    _store_in_l2(adapter, keys)
    deferral = _RecordingDeferral(answer=True)

    request_id = controller.submit_prefetch_request(
        PrefetchRequestSpec(keys, {0: LAYOUT}, l2_deferral=deferral)
    )
    outcome = _outcome(controller, request_id)

    assert outcome.retained.count_leading_ones() == 4
    assert outcome.deferred_keys == tuple(keys)
    assert adapter.load_calls == 0
    assert all(err == L1Error.KEY_NOT_EXIST for err, _ in l1.unsafe_read(keys).values())
    assert _wait(lambda: adapter.debug_get_locked_key_count() == 0)


def test_deferral_is_asked_about_the_trimmed_plan_only(
    l1: L1Manager, adapter: _CountingAdapter, controller: PrefetchController
) -> None:
    # Chunk 0 is in L1, chunks 1-2 in L2, chunk 3 missing, chunk 4 in L2 past
    # the gap: the plan is chunks 1 and 2.
    keys = [_key(i) for i in range(5)]
    _put_in_l1(l1, keys[:1])
    _store_in_l2(adapter, [keys[1], keys[2], keys[4]])
    deferral = _RecordingDeferral(answer=True)

    request_id = controller.submit_prefetch_request(
        PrefetchRequestSpec(keys, {0: LAYOUT}, l2_deferral=deferral)
    )
    outcome = _outcome(controller, request_id)

    assert deferral.calls == [([0], [keys[1], keys[2]])]
    assert outcome.retained.count_leading_ones() == 3
    assert outcome.deferred_keys == (keys[1], keys[2])


def test_deferred_request_keeps_its_l1_hits_read_locked(
    l1: L1Manager, adapter: _CountingAdapter, controller: PrefetchController
) -> None:
    keys = [_key(i) for i in range(3)]
    _put_in_l1(l1, keys[:1])
    _store_in_l2(adapter, keys[1:])

    request_id = controller.submit_prefetch_request(
        PrefetchRequestSpec(keys, {0: LAYOUT}, l2_deferral=_RecordingDeferral(True))
    )
    _outcome(controller, request_id)

    # Read-locked for the retriever: a delete must refuse it.
    l1.delete(keys[:1])
    assert l1.unsafe_read(keys[:1])[keys[0]][0] == L1Error.SUCCESS
    l1.finish_read(keys[:1])
    l1.delete(keys[:1])
    assert l1.unsafe_read(keys[:1])[keys[0]][0] == L1Error.KEY_NOT_EXIST


def test_declined_deferral_loads_as_usual(
    l1: L1Manager, adapter: _CountingAdapter, controller: PrefetchController
) -> None:
    keys = [_key(i) for i in range(3)]
    _store_in_l2(adapter, keys)
    deferral = _RecordingDeferral(answer=False)

    request_id = controller.submit_prefetch_request(
        PrefetchRequestSpec(keys, {0: LAYOUT}, l2_deferral=deferral)
    )
    outcome = _outcome(controller, request_id)

    assert len(deferral.calls) == 1
    assert outcome.deferred_keys == ()
    assert outcome.retained.count_leading_ones() == 3
    assert adapter.load_calls == 1
    assert all(err == L1Error.SUCCESS for err, _ in l1.unsafe_read(keys).values())
    l1.finish_read(keys)


def test_deferral_is_not_asked_without_l2_hits(
    l1: L1Manager, adapter: _CountingAdapter, controller: PrefetchController
) -> None:
    keys = [_key(i) for i in range(2)]
    _put_in_l1(l1, keys)
    deferral = _RecordingDeferral(answer=True)

    request_id = controller.submit_prefetch_request(
        PrefetchRequestSpec(keys, {0: LAYOUT}, l2_deferral=deferral)
    )
    outcome = _outcome(controller, request_id)

    assert deferral.calls == []
    assert outcome.deferred_keys == ()
    l1.finish_read(keys)


def test_warm_prefetch_never_defers(
    l1: L1Manager, adapter: _CountingAdapter, controller: PrefetchController
) -> None:
    keys = [_key(i) for i in range(2)]
    _store_in_l2(adapter, keys)
    deferral = _RecordingDeferral(answer=True)

    request_id = controller.submit_prefetch_request(
        PrefetchRequestSpec(
            keys, {0: LAYOUT}, mode=PrefetchMode.WARM, l2_deferral=deferral
        )
    )
    outcome = _outcome(controller, request_id)

    assert deferral.calls == []
    assert outcome.deferred_keys == ()
    assert adapter.load_calls == 1


def test_a_result_is_returned_once_by_either_query(
    adapter: _CountingAdapter, controller: PrefetchController
) -> None:
    keys = [_key(i) for i in range(2)]
    _store_in_l2(adapter, keys)

    request_id = controller.submit_prefetch_request(
        PrefetchRequestSpec(keys, {0: LAYOUT}, l2_deferral=_RecordingDeferral(True))
    )
    assert _wait(lambda: controller.wait_prefetch_result(request_id, 0.0))
    retained = controller.query_prefetch_result(request_id)

    assert retained is not None and retained.count_leading_ones() == 2
    assert controller.query_prefetch_outcome(request_id) is None
    assert controller.query_prefetch_result(request_id) is None
