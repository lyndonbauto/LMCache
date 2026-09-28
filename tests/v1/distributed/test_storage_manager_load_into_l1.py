# SPDX-License-Identifier: Apache-2.0
"""Tests for ``StorageManager.load_into_l1``, the blocking whole-object load a
layerwise retrieve falls back on.

Runs on CPU with ``MockL2Adapter``.
"""

# Standard
from collections.abc import Iterator
import time

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.mock_l2_adapter import (
    MockL2Adapter,
    MockL2AdapterConfig,
)
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.memory_management import MemoryObj, MemoryObjMetadata, TensorMemoryObj
from lmcache.v1.mp_observability.errors import LMCacheTimeoutError

LAYOUT = MemoryLayoutDesc(shapes=[torch.Size([16, 256])], dtypes=[torch.float32])


def _key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)


def _mock_adapter(sm: StorageManager) -> MockL2Adapter:
    (_descriptor, adapter), *_ = sm.l2_adapters()
    if not isinstance(adapter, MockL2Adapter):
        raise TypeError(f"expected a MockL2Adapter, got {type(adapter).__name__}")
    return adapter


def _store_in_l2(adapter: MockL2Adapter, keys: list[ObjectKey], fill: float) -> None:
    objs: list[MemoryObj] = []
    for _ in keys:
        tensor = torch.full(LAYOUT.shapes[0], fill, dtype=LAYOUT.dtypes[0])
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
    deadline = time.monotonic() + 5.0
    while not all(adapter.debug_has_key(k) for k in keys):
        assert time.monotonic() < deadline, "store to L2 did not finish"
        time.sleep(0.01)


def _config(
    mock_bandwidth_gb: float, prefetch_policy: str = "default"
) -> StorageManagerConfig:
    return StorageManagerConfig(
        l1_manager_config=L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(
                size_in_bytes=8 << 20,
                use_lazy=False,
                init_size_in_bytes=8 << 20,
                align_bytes=0x1000,
                shm_name="",
            ),
            write_ttl_seconds=600,
            read_ttl_seconds=300,
        ),
        eviction_config=EvictionConfig(eviction_policy="LRU"),
        l2_adapter_config=L2AdaptersConfig(
            adapters=[
                MockL2AdapterConfig(
                    max_size_gb=0.01, mock_bandwidth_gb=mock_bandwidth_gb
                )
            ]
        ),
        prefetch_policy=prefetch_policy,
    )


@pytest.fixture
def storage_manager() -> Iterator[StorageManager]:
    sm = StorageManager(_config(mock_bandwidth_gb=10.0))
    yield sm
    sm.close()


def test_loads_every_key_and_holds_a_read_lock(storage_manager: StorageManager) -> None:
    keys = [_key(i) for i in range(3)]
    _store_in_l2(_mock_adapter(storage_manager), keys, fill=2.0)

    loaded = storage_manager.load_into_l1(keys, {0: LAYOUT}, timeout_seconds=5.0)

    assert list(loaded) == keys
    assert all(torch.all(obj.tensor == 2.0) for obj in loaded.values())
    # Locked for the caller: a non-forced delete skips every key.
    assert storage_manager.delete_l1_keys(keys) == (0, 3)
    storage_manager.finish_read_prefetched(keys)


def test_a_key_in_neither_tier_is_left_out(storage_manager: StorageManager) -> None:
    keys = [_key(i) for i in range(3)]
    _store_in_l2(_mock_adapter(storage_manager), [keys[0], keys[2]], fill=1.0)

    loaded = storage_manager.load_into_l1(keys, {0: LAYOUT}, timeout_seconds=5.0)

    assert list(loaded) == [keys[0], keys[2]]
    storage_manager.finish_read_prefetched(list(loaded))


def test_no_keys_loads_nothing(storage_manager: StorageManager) -> None:
    assert storage_manager.load_into_l1([], {0: LAYOUT}, timeout_seconds=0.0) == {}


def _released(sm: StorageManager, keys: list[ObjectKey]) -> bool:
    status = sm.report_status()["prefetch_controller"]
    idle = not (
        status["submission_queue_size"]
        or status["pending_queue_size"]
        or status["in_flight_request_count"]
    )
    deleted, skipped = sm.delete_l1_keys(keys)
    return (
        idle
        and status["completed_results_count"] == 0
        and (deleted, skipped) == (len(keys), 0)
    )


def test_a_load_that_times_out_unlocks_its_keys_when_it_finishes() -> None:
    """Nobody reads a timed-out load's result, so it must not strand locks."""
    # 3 x 16 KiB at 100 KB/s: the load takes about half a second. ``retain``
    # keeps the loaded objects in L1 once unlocked, so they can be seen.
    sm = StorageManager(_config(mock_bandwidth_gb=1e-4, prefetch_policy="retain"))
    try:
        keys = [_key(i) for i in range(3)]
        _store_in_l2(_mock_adapter(sm), keys, fill=1.0)

        with pytest.raises(LMCacheTimeoutError):
            sm.load_into_l1(keys, {0: LAYOUT}, timeout_seconds=0.0)

        # Nothing in flight, no result left behind, and every key loaded and
        # unlocked: a non-forced delete removes them all.
        deadline = time.monotonic() + 10.0
        while not _released(sm, keys):
            assert time.monotonic() < deadline, "the late load was never released"
            time.sleep(0.05)
    finally:
        sm.close()
