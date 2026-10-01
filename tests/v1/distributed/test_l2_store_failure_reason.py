# SPDX-License-Identifier: Apache-2.0
"""A failed L2 store says why it failed.

Day 1 saw ``Store task N to adapter 0 failed for keys: [...]`` with no reason,
although the native connector reports one (an Aerospike "queue too deep"
error, in that run). The reason now travels from the adapter's completion
through ``L2StoreResult`` to the store controller's warning and its
``L2_STORE_COMPLETED`` event.
"""

# Standard
import select
import time

# Third Party
import pytest
import torch

# First Party
from lmcache import torch_dev
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.config import L1ManagerConfig, L1MemoryManagerConfig
from lmcache.v1.distributed.internal_api import L2StoreResult
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.mock_l2_adapter import (
    MockL2Adapter,
    MockL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
)
from lmcache.v1.distributed.storage_controllers import store_controller
from lmcache.v1.distributed.storage_controllers.store_controller import (
    StoreController,
)
from lmcache.v1.distributed.storage_controllers.store_policy import (
    AdapterDescriptor,
    DefaultStorePolicy,
)
from lmcache.v1.mp_observability.event import Event, EventType
from tests.v1.distributed.test_native_connector_l2_adapter import (
    MockNativeConnector,
    create_memory_obj,
    create_object_key,
)
from tests.v1.distributed.utils import should_use_lazy_alloc

REASON = "AEROSPIKE_ERR_DEVICE_OVERLOAD: queue too deep: exceeds max 8"


class _FailingNativeConnector(MockNativeConnector):
    """A native connector whose batch writes fail with the backend's error."""

    def submit_batch_set(self, keys: list[str], memoryviews: list) -> int:
        with self._lock:
            fid = self._next_id
            self._next_id += 1
        self._push_completion(fid, False, REASON, None)
        return fid


class _RecordingBus:
    """Stands in for the event bus; keeps what was published."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


def _wait_readable(fd: int, timeout: float = 5.0) -> bool:
    poll = select.poll()
    poll.register(fd, select.POLLIN)
    return bool(poll.poll(timeout * 1000))


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _failed_count(bus: _RecordingBus) -> int:
    """Keys the store controller has reported failed so far."""
    return sum(
        e.metadata["failed_count"]
        for e in bus.events
        if e.event_type is EventType.L2_STORE_COMPLETED
    )


def test_a_failed_result_carries_its_reason_outside_its_value() -> None:
    result = L2StoreResult(False, 0, failure_reason=REASON)

    assert not result.is_successful()
    assert result.failure_reason() == REASON
    assert result == L2StoreResult(False, 0)
    assert int(result) == -1


def test_a_successful_result_has_no_reason() -> None:
    result = L2StoreResult(True, 4096, failure_reason="ignored")

    assert result.is_successful()
    assert result.bytes_transferred() == 4096
    assert result.failure_reason() == ""


def test_a_result_without_a_reason_reports_none() -> None:
    assert L2StoreResult(False, 0).failure_reason() == ""


def test_the_native_adapter_passes_the_backend_error_through() -> None:
    adapter = NativeConnectorL2Adapter(_FailingNativeConnector())
    try:
        task_id = adapter.submit_store_task(
            [create_object_key(1)], [create_memory_obj()]
        )
        assert _wait_readable(adapter.get_store_event_fd())

        completed = adapter.pop_completed_store_tasks()
        assert set(completed) == {task_id}
        assert not completed[task_id].is_successful()
        assert completed[task_id].failure_reason() == REASON
    finally:
        adapter.close()


@pytest.mark.skipif(not torch_dev.is_available(), reason="L1 needs a device runtime")
def test_the_store_controller_logs_and_publishes_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _RecordingBus()
    monkeypatch.setattr(store_controller, "get_event_bus", lambda: bus)
    warnings: list[str] = []
    monkeypatch.setattr(
        store_controller.logger,
        "warning",
        lambda msg, *args, **kwargs: warnings.append(str(msg) % args),
    )
    adapter = MockL2Adapter(
        MockL2AdapterConfig(max_size_gb=0.01, mock_bandwidth_gb=10.0)
    )
    succeeded = adapter.pop_completed_store_tasks

    def fail_every_store() -> dict[int, L2StoreResult]:
        return {
            task_id: L2StoreResult(False, 0, failure_reason=REASON)
            for task_id in succeeded()
        }

    monkeypatch.setattr(adapter, "pop_completed_store_tasks", fail_every_store)
    l1 = L1Manager(
        L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(
                size_in_bytes=64 * 1024 * 1024,
                use_lazy=should_use_lazy_alloc(),
                init_size_in_bytes=32 * 1024 * 1024,
                align_bytes=0x1000,
            ),
            write_ttl_seconds=600,
            read_ttl_seconds=300,
        )
    )
    controller = StoreController(
        l1_manager=l1,
        l2_adapters=[adapter],
        adapter_descriptors=[
            AdapterDescriptor(
                index=0,
                config=MockL2AdapterConfig(max_size_gb=0.01, mock_bandwidth_gb=10.0),
            )
        ],
        policy=DefaultStorePolicy(),
    )
    controller.start()
    try:
        keys = [
            ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)
            for i in range(3)
        ]
        written = l1.reserve_write(
            keys=keys,
            is_temporary=[False] * len(keys),
            layout_desc=MemoryLayoutDesc([torch.Size([16, 2, 64])], [torch.bfloat16]),
            mode="new",
        )
        l1.finish_write([k for k, (_e, obj) in written.items() if obj is not None])

        assert _wait_for(lambda: _failed_count(bus) == len(keys))
    finally:
        controller.stop()
        adapter.close()
        l1.close()

    failures = [w for w in warnings if "failed for" in w]
    assert failures and all(REASON in w for w in failures)
    completed = [e for e in bus.events if e.event_type is EventType.L2_STORE_COMPLETED]
    assert all(e.metadata["failure_reason"] == REASON for e in completed)
