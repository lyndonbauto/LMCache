# SPDX-License-Identifier: Apache-2.0
"""Tests for the RDMA windows reserved in L1, per the docstring contracts of
``L1MemoryManager``, ``L1Manager``, and ``normalize_storage_manager_config``.

The slab is small pinned CPU memory, so no GPU is needed.
"""

# Standard
from collections.abc import Iterator

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
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.internal_api import (
    GENERAL_L1_POOL,
    L1ManagerListener,
    L1Pool,
)
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
    validate_windows_reserved,
)
from lmcache.v1.distributed.memory_manager import L1MemoryManager

ALIGN = 4096
WINDOW_BYTES = 256 * 1024
WINDOW_COUNT = 2
RESERVED = WINDOW_BYTES * WINDOW_COUNT
SLAB_BYTES = 4 * 1024 * 1024
# 64 KiB per object: four fit in one window.
LAYOUT = MemoryLayoutDesc(shapes=[torch.Size([16, 1024])], dtypes=[torch.float32])
OBJECT_BYTES = 64 * 1024
PER_WINDOW = WINDOW_BYTES // OBJECT_BYTES


def _memory_config(window_count: int = WINDOW_COUNT) -> L1MemoryManagerConfig:
    return L1MemoryManagerConfig(
        size_in_bytes=SLAB_BYTES,
        use_lazy=False,
        align_bytes=ALIGN,
        shm_name="",
        rdma_window_count=window_count,
        rdma_window_bytes=WINDOW_BYTES if window_count else 0,
    )


def _key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)


def _offset(manager: L1MemoryManager, obj) -> int:
    return obj.data_ptr - manager.get_l1_memory_desc().ptr


def _rdma_adapter(plan: RdmaWindowPlan) -> AerospikeL2AdapterConfig:
    return AerospikeL2AdapterConfig(
        hosts="127.0.0.1:3000",
        rdma=L1RdmaConfig(transport=RdmaTransport.RC, window_plan=plan),
    )


class _RecordingListener(L1ManagerListener):
    def __init__(self) -> None:
        self.write_finished: list[ObjectKey] = []
        self.deleted: list[ObjectKey] = []

    def on_l1_keys_reserved_read(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_read_finished(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_reserved_write(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_write_finished(self, keys: list[ObjectKey]) -> None:
        self.write_finished.extend(keys)

    def on_l1_keys_finish_write_and_reserve_read(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_deleted_by_manager(self, keys: list[ObjectKey]) -> None:
        self.deleted.extend(keys)

    def on_l1_keys_accessed(self, keys: list[ObjectKey]) -> None:
        pass


@pytest.fixture
def manager() -> Iterator[L1MemoryManager]:
    mm = L1MemoryManager(_memory_config())
    yield mm
    mm.close()


@pytest.fixture
def l1() -> Iterator[tuple[L1Manager, _RecordingListener]]:
    mgr = L1Manager(L1ManagerConfig(memory_config=_memory_config()))
    listener = _RecordingListener()
    mgr.register_listener(listener)
    yield mgr, listener
    mgr.close()


# ---------------------------------------------------------------------------
# L1MemoryManager
# ---------------------------------------------------------------------------


def test_general_allocations_never_enter_the_windows(
    manager: L1MemoryManager,
) -> None:
    err, objs = manager.allocate(LAYOUT, (SLAB_BYTES - RESERVED) // OBJECT_BYTES)

    assert err == L1Error.SUCCESS
    assert all(_offset(manager, obj) >= RESERVED for obj in objs)
    assert manager.allocate(LAYOUT, 1) == (L1Error.OUT_OF_MEMORY, [])
    assert all(manager.get_pool(obj) == GENERAL_L1_POOL for obj in objs)


def test_window_allocations_stay_inside_their_window(
    manager: L1MemoryManager,
) -> None:
    for index in range(WINDOW_COUNT):
        pool = L1Pool.rdma_window(index)
        err, objs = manager.allocate(LAYOUT, PER_WINDOW, pool)

        assert err == L1Error.SUCCESS
        for obj in objs:
            offset = _offset(manager, obj)
            assert index * WINDOW_BYTES <= offset < (index + 1) * WINDOW_BYTES
            assert obj.meta.address == offset
            assert manager.get_pool(obj) == pool
        assert manager.allocate(LAYOUT, 1, pool) == (L1Error.OUT_OF_MEMORY, [])


def test_a_full_window_does_not_touch_general_memory(
    manager: L1MemoryManager,
) -> None:
    window = L1Pool.rdma_window(0)
    _, held = manager.allocate(LAYOUT, PER_WINDOW, window)

    assert manager.allocate(LAYOUT, 1, window)[0] == L1Error.OUT_OF_MEMORY
    assert manager.get_memory_usage()[0] == 0
    assert len(held) == PER_WINDOW


def test_free_returns_each_object_to_its_own_pool(
    manager: L1MemoryManager,
) -> None:
    window = L1Pool.rdma_window(1)
    _, in_window = manager.allocate(LAYOUT, PER_WINDOW, window)
    _, general = manager.allocate(LAYOUT, 2)

    manager.free(in_window + general)

    assert manager.get_memory_usage()[0] == 0
    assert manager.allocate(LAYOUT, PER_WINDOW, window)[0] == L1Error.SUCCESS
    assert manager.memcheck()


def test_memory_usage_covers_the_general_pool_only(
    manager: L1MemoryManager,
) -> None:
    _, in_window = manager.allocate(LAYOUT, PER_WINDOW, L1Pool.rdma_window(0))
    _, general = manager.allocate(LAYOUT, 1)

    used, total = manager.get_memory_usage()

    assert total == SLAB_BYTES - RESERVED
    assert used == OBJECT_BYTES
    assert len(in_window) + len(general) == PER_WINDOW + 1


def test_the_memory_desc_still_covers_the_whole_slab(
    manager: L1MemoryManager,
) -> None:
    assert manager.get_l1_memory_desc().size == SLAB_BYTES


def test_a_missing_window_is_refused(manager: L1MemoryManager) -> None:
    with pytest.raises(ValueError):
        manager.allocate(LAYOUT, 1, L1Pool.rdma_window(WINDOW_COUNT))


def test_without_windows_every_window_pool_is_refused() -> None:
    mm = L1MemoryManager(_memory_config(window_count=0))
    try:
        assert mm.get_memory_usage()[1] == SLAB_BYTES
        with pytest.raises(ValueError):
            mm.allocate(LAYOUT, 1, L1Pool.rdma_window(0))
    finally:
        mm.close()


def test_windows_with_a_lazy_allocator_are_refused() -> None:
    config = _memory_config()
    config.use_lazy = True
    with pytest.raises(ValueError):
        L1MemoryManager(config)


def test_windows_that_fill_the_slab_are_refused() -> None:
    config = L1MemoryManagerConfig(
        size_in_bytes=RESERVED,
        use_lazy=False,
        align_bytes=ALIGN,
        shm_name="",
        rdma_window_count=WINDOW_COUNT,
        rdma_window_bytes=WINDOW_BYTES,
    )
    with pytest.raises(ValueError):
        L1MemoryManager(config)


@pytest.mark.parametrize(("count", "size"), [(1, 0), (0, WINDOW_BYTES), (-1, 1)])
def test_inconsistent_window_fields_are_refused(count: int, size: int) -> None:
    with pytest.raises(ValueError):
        L1MemoryManagerConfig(
            size_in_bytes=SLAB_BYTES,
            use_lazy=False,
            rdma_window_count=count,
            rdma_window_bytes=size,
        )


# ---------------------------------------------------------------------------
# L1Manager
# ---------------------------------------------------------------------------


def test_reserve_write_in_a_window(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, _ = l1
    keys = [_key(i) for i in range(PER_WINDOW)]

    ret = mgr.reserve_write(
        keys, [False] * len(keys), LAYOUT, mode="new", pool=L1Pool.rdma_window(0)
    )

    base = mgr.get_l1_memory_desc().ptr
    for key in keys:
        err, obj = ret[key]
        assert err == L1Error.SUCCESS
        assert obj is not None
        assert 0 <= obj.data_ptr - base < WINDOW_BYTES


@pytest.mark.parametrize("mode", ["update", "all"])
def test_reserve_write_in_a_window_requires_new_mode(
    l1: tuple[L1Manager, _RecordingListener], mode: str
) -> None:
    mgr, _ = l1
    with pytest.raises(ValueError):
        mgr.reserve_write(
            [_key(0)], [False], LAYOUT, mode=mode, pool=L1Pool.rdma_window(0)
        )


def test_window_objects_are_not_evictable(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, _ = l1
    mgr.reserve_write([_key(0)], [False], LAYOUT, mode="new")
    mgr.reserve_write(
        [_key(1)], [False], LAYOUT, mode="new", pool=L1Pool.rdma_window(0)
    )
    mgr.finish_write([_key(0), _key(1)])

    assert mgr.is_key_evictable(_key(0))
    assert not mgr.is_key_evictable(_key(1))


def _fill_window(mgr: L1Manager, keys: list[ObjectKey]) -> None:
    mgr.reserve_write(
        keys, [False] * len(keys), LAYOUT, mode="new", pool=L1Pool.rdma_window(0)
    )
    mgr.finish_write(keys)


def test_delete_if_none_locked_deletes_everything(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, listener = l1
    keys = [_key(i) for i in range(PER_WINDOW)]
    _fill_window(mgr, keys)

    assert mgr.delete_if_none_locked(keys + [_key(99)]) == L1Error.SUCCESS

    assert all(mgr.get_object_state(key) is None for key in keys)
    assert listener.deleted == keys
    refill = mgr.reserve_write(
        [_key(50)], [False], LAYOUT, mode="new", pool=L1Pool.rdma_window(0)
    )
    assert refill[_key(50)][0] == L1Error.SUCCESS


@pytest.mark.parametrize("lock", ["read", "write"])
def test_delete_if_none_locked_deletes_nothing_when_one_is_locked(
    l1: tuple[L1Manager, _RecordingListener], lock: str
) -> None:
    mgr, listener = l1
    keys = [_key(i) for i in range(PER_WINDOW)]
    _fill_window(mgr, keys)
    if lock == "read":
        mgr.reserve_read([keys[-1]])
    else:
        mgr.reserve_write([keys[-1]], [False], LAYOUT, mode="update")

    assert mgr.delete_if_none_locked(keys) == L1Error.KEY_IS_LOCKED

    assert all(mgr.get_object_state(key) is not None for key in keys)
    assert listener.deleted == []


def test_abort_write_removes_the_reservation_silently(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, listener = l1
    keys = [_key(i) for i in range(PER_WINDOW)]
    mgr.reserve_write(
        keys, [False] * len(keys), LAYOUT, mode="new", pool=L1Pool.rdma_window(0)
    )

    ret = mgr.abort_write(keys)

    assert ret == {key: L1Error.SUCCESS for key in keys}
    assert all(mgr.get_object_state(key) is None for key in keys)
    assert listener.write_finished == []
    assert listener.deleted == keys
    refill = mgr.reserve_write(
        keys, [False] * len(keys), LAYOUT, mode="new", pool=L1Pool.rdma_window(0)
    )
    assert all(err == L1Error.SUCCESS for err, _ in refill.values())


def test_abort_write_then_reserve_in_general_l1(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, _ = l1
    key = _key(0)
    mgr.reserve_write([key], [False], LAYOUT, mode="new", pool=L1Pool.rdma_window(0))

    mgr.abort_write([key])
    err, obj = mgr.reserve_write([key], [False], LAYOUT, mode="new")[key]

    assert err == L1Error.SUCCESS
    assert obj is not None
    assert obj.data_ptr - mgr.get_l1_memory_desc().ptr >= RESERVED


def test_abort_write_leaves_keys_it_does_not_own(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, listener = l1
    finished, read_locked = _key(0), _key(1)
    _fill_window(mgr, [finished, read_locked])
    mgr.reserve_read([read_locked])

    ret = mgr.abort_write([finished, read_locked, _key(99)])

    assert ret == {
        finished: L1Error.KEY_IN_WRONG_STATE,
        read_locked: L1Error.KEY_IN_WRONG_STATE,
        _key(99): L1Error.KEY_NOT_EXIST,
    }
    assert mgr.get_object_state(finished) is not None
    assert mgr.get_object_state(read_locked) is not None
    assert listener.deleted == []


def test_window_object_count_follows_reserve_and_abort(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, _ = l1
    keys = [_key(i) for i in range(3)]
    mgr.reserve_write(
        keys, [False] * len(keys), LAYOUT, mode="new", pool=L1Pool.rdma_window(1)
    )
    mgr.reserve_write([_key(9)], [False], LAYOUT, mode="new")

    assert mgr.get_rdma_window_count() == WINDOW_COUNT
    assert mgr.get_rdma_window_object_count(0) == 0
    assert mgr.get_rdma_window_object_count(1) == 3

    mgr.abort_write(keys[:2])

    assert mgr.get_rdma_window_object_count(1) == 1


def test_reclaim_empties_one_window_and_nothing_else(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, listener = l1
    in_window = [_key(i) for i in range(PER_WINDOW)]
    _fill_window(mgr, in_window)
    other, general = _key(50), _key(60)
    mgr.reserve_write([other], [False], LAYOUT, mode="new", pool=L1Pool.rdma_window(1))
    mgr.reserve_write([general], [False], LAYOUT, mode="new")
    mgr.finish_write([other, general])

    assert mgr.reclaim_rdma_window(0) == L1Error.SUCCESS

    assert all(mgr.get_object_state(key) is None for key in in_window)
    assert sorted(listener.deleted, key=str) == sorted(in_window, key=str)
    assert mgr.get_object_state(other) is not None
    assert mgr.get_object_state(general) is not None
    assert mgr.get_rdma_window_object_count(0) == 0
    refill = [_key(i) for i in range(100, 100 + PER_WINDOW)]
    ret = mgr.reserve_write(
        refill, [False] * len(refill), LAYOUT, mode="new", pool=L1Pool.rdma_window(0)
    )
    assert all(err == L1Error.SUCCESS for err, _ in ret.values())


def test_reclaiming_an_empty_window_succeeds_silently(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, listener = l1

    assert mgr.reclaim_rdma_window(1) == L1Error.SUCCESS
    assert listener.deleted == []


@pytest.mark.parametrize("lock", ["read", "write"])
def test_reclaim_deletes_nothing_when_one_object_is_locked(
    l1: tuple[L1Manager, _RecordingListener], lock: str
) -> None:
    mgr, listener = l1
    keys = [_key(i) for i in range(PER_WINDOW)]
    _fill_window(mgr, keys)
    if lock == "read":
        mgr.reserve_read([keys[0]])
    else:
        mgr.reserve_write([keys[0]], [False], LAYOUT, mode="update")

    assert mgr.reclaim_rdma_window(0) == L1Error.KEY_IS_LOCKED

    assert all(mgr.get_object_state(key) is not None for key in keys)
    assert listener.deleted == []


def test_reclaim_ignores_a_key_that_moved_to_general_l1(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, _ = l1
    key = _key(0)
    _fill_window(mgr, [key])
    mgr.delete([key])
    mgr.reserve_write([key], [False], LAYOUT, mode="new")
    mgr.finish_write([key])

    assert mgr.get_rdma_window_object_count(0) == 0
    assert mgr.reclaim_rdma_window(0) == L1Error.SUCCESS
    assert mgr.get_object_state(key) is not None


def test_clear_empties_the_window_record(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, _ = l1
    _fill_window(mgr, [_key(0), _key(1)])

    mgr.clear()

    assert mgr.get_rdma_window_object_count(0) == 0


def test_a_missing_window_cannot_be_reclaimed(
    l1: tuple[L1Manager, _RecordingListener],
) -> None:
    mgr, _ = l1
    with pytest.raises(ValueError):
        mgr.reclaim_rdma_window(WINDOW_COUNT)
    with pytest.raises(ValueError):
        mgr.get_rdma_window_object_count(-1)


# ---------------------------------------------------------------------------
# Config normalization
# ---------------------------------------------------------------------------


def _storage_config(
    adapters: list[AerospikeL2AdapterConfig],
) -> StorageManagerConfig:
    return StorageManagerConfig(
        l1_manager_config=L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(
                size_in_bytes=SLAB_BYTES, use_lazy=False, shm_name=""
            )
        ),
        eviction_config=EvictionConfig(eviction_policy="LRU"),
        l2_adapter_config=L2AdaptersConfig(adapters),
    )


def test_normalization_reserves_the_rdma_adapter_windows() -> None:
    plan = RdmaWindowPlan(window_count=3, window_bytes=WINDOW_BYTES)

    config = _storage_config([_rdma_adapter(plan)])

    memory = config.l1_manager_config.memory_config
    assert (memory.rdma_window_count, memory.rdma_window_bytes) == (3, WINDOW_BYTES)


def test_normalization_reserves_nothing_without_rdma() -> None:
    config = _storage_config([AerospikeL2AdapterConfig(hosts="127.0.0.1:3000")])

    memory = config.l1_manager_config.memory_config
    assert (memory.rdma_window_count, memory.rdma_window_bytes) == (0, 0)


def test_two_rdma_adapters_are_refused() -> None:
    plan = RdmaWindowPlan(window_count=1, window_bytes=WINDOW_BYTES)
    with pytest.raises(ValueError):
        _storage_config([_rdma_adapter(plan), _rdma_adapter(plan)])


def test_an_adapter_whose_windows_were_not_reserved_is_refused() -> None:
    adapter = _rdma_adapter(RdmaWindowPlan(window_count=2, window_bytes=WINDOW_BYTES))

    validate_windows_reserved(adapter, 2, WINDOW_BYTES)
    validate_windows_reserved(AerospikeL2AdapterConfig(hosts="h:1"), 0, 0)
    with pytest.raises(ValueError):
        validate_windows_reserved(adapter, 0, 0)
    with pytest.raises(ValueError):
        validate_windows_reserved(adapter, 2, 2 * WINDOW_BYTES)
