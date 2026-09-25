# SPDX-License-Identifier: Apache-2.0
"""Tests for ``RdmaWindowLeaser`` against its docstring contract.

The L1 is small pinned CPU memory with real RDMA windows, so no GPU or RDMA
device is needed. Time comes from a fake clock.
"""

# Standard
from collections.abc import Iterator

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.config import L1ManagerConfig, L1MemoryManagerConfig
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.internal_api import L1Pool
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.l2_adapters.rdma_window_leaser import (
    FetchOutcome,
    RdmaWindowLeaser,
    WindowLease,
)
from lmcache.v1.layerwise.contract import LayerwiseContractError, PlanTooLargeError

WINDOW_BYTES = 256 * 1024
SLAB_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT = 30.0
LAYOUT = MemoryLayoutDesc(shapes=[torch.Size([16, 1024])], dtypes=[torch.float32])
OBJECT_BYTES = 64 * 1024


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _rdma_config(window_count: int) -> L1RdmaConfig:
    return L1RdmaConfig(
        transport=RdmaTransport.RC,
        window_plan=RdmaWindowPlan(
            window_count=window_count, window_bytes=WINDOW_BYTES
        ),
        fetch_timeout_seconds=FETCH_TIMEOUT,
    )


def _l1(window_count: int) -> L1Manager:
    return L1Manager(
        L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(
                size_in_bytes=SLAB_BYTES,
                use_lazy=False,
                align_bytes=4096,
                shm_name="",
                rdma_window_count=window_count,
                rdma_window_bytes=WINDOW_BYTES,
            )
        )
    )


def _key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)


def _store(mgr: L1Manager, lease: WindowLease, keys: list[ObjectKey]) -> None:
    ret = mgr.reserve_write(
        keys, [False] * len(keys), LAYOUT, mode="new", pool=lease.pool()
    )
    assert all(err == L1Error.SUCCESS for err, _ in ret.values())
    mgr.finish_write(keys)


def _present(mgr: L1Manager, keys: list[ObjectKey]) -> bool:
    return all(mgr.get_object_state(key) is not None for key in keys)


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def two_windows(clock: _FakeClock) -> Iterator[tuple[L1Manager, RdmaWindowLeaser]]:
    mgr = _l1(2)
    yield mgr, RdmaWindowLeaser(mgr, _rdma_config(2), clock)
    mgr.close()


@pytest.fixture
def one_window(clock: _FakeClock) -> Iterator[tuple[L1Manager, RdmaWindowLeaser]]:
    mgr = _l1(1)
    yield mgr, RdmaWindowLeaser(mgr, _rdma_config(1), clock)
    mgr.close()


def test_a_lease_describes_its_window(
    two_windows: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    mgr, leaser = two_windows

    lease = leaser.lease(OBJECT_BYTES)

    assert lease.size_bytes == WINDOW_BYTES
    assert lease.base_offset == lease.window_index * WINDOW_BYTES
    assert lease.pool() == L1Pool.rdma_window(lease.window_index)
    err, obj = mgr.reserve_write(
        [_key(0)], [False], LAYOUT, mode="new", pool=lease.pool()
    )[_key(0)]
    assert err == L1Error.SUCCESS
    assert obj is not None
    offset = obj.data_ptr - mgr.get_l1_memory_desc().ptr
    assert lease.base_offset <= offset < lease.base_offset + lease.size_bytes


def test_only_one_lease_is_outstanding(
    two_windows: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    _, leaser = two_windows
    first = leaser.lease(OBJECT_BYTES)

    with pytest.raises(LayerwiseContractError) as refused:
        leaser.lease(OBJECT_BYTES)
    assert not isinstance(refused.value, PlanTooLargeError)

    leaser.release(first, FetchOutcome.FINISHED)
    leaser.lease(OBJECT_BYTES)


def test_a_request_larger_than_a_window_is_too_large(
    two_windows: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    _, leaser = two_windows

    with pytest.raises(PlanTooLargeError):
        leaser.lease(WINDOW_BYTES + 1)
    leaser.lease(WINDOW_BYTES)


@pytest.mark.parametrize("request_bytes", [0, -1])
def test_a_non_positive_request_is_refused(
    two_windows: tuple[L1Manager, RdmaWindowLeaser], request_bytes: int
) -> None:
    _, leaser = two_windows
    with pytest.raises(ValueError):
        leaser.lease(request_bytes)


def test_an_empty_window_is_preferred_to_reclaiming(
    two_windows: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    mgr, leaser = two_windows
    first = leaser.lease(OBJECT_BYTES)
    kept = [_key(0), _key(1)]
    _store(mgr, first, kept)
    leaser.release(first, FetchOutcome.FINISHED)

    second = leaser.lease(OBJECT_BYTES)

    assert second.window_index != first.window_index
    assert _present(mgr, kept)


def test_the_window_released_longest_ago_is_reclaimed(
    two_windows: tuple[L1Manager, RdmaWindowLeaser], clock: _FakeClock
) -> None:
    mgr, leaser = two_windows
    older_keys, newer_keys = [_key(0), _key(1)], [_key(2)]
    older = leaser.lease(OBJECT_BYTES)
    _store(mgr, older, older_keys)
    leaser.release(older, FetchOutcome.FINISHED)
    clock.now += 1
    newer = leaser.lease(OBJECT_BYTES)
    _store(mgr, newer, newer_keys)
    leaser.release(newer, FetchOutcome.FINISHED)

    third = leaser.lease(OBJECT_BYTES)

    assert third.window_index == older.window_index
    assert all(mgr.get_object_state(key) is None for key in older_keys)
    assert _present(mgr, newer_keys)


def test_a_window_with_an_object_in_use_is_skipped(
    two_windows: tuple[L1Manager, RdmaWindowLeaser], clock: _FakeClock
) -> None:
    mgr, leaser = two_windows
    older = leaser.lease(OBJECT_BYTES)
    _store(mgr, older, [_key(0)])
    leaser.release(older, FetchOutcome.FINISHED)
    clock.now += 1
    newer = leaser.lease(OBJECT_BYTES)
    _store(mgr, newer, [_key(1)])
    leaser.release(newer, FetchOutcome.FINISHED)
    mgr.reserve_read([_key(0)])

    third = leaser.lease(OBJECT_BYTES)

    assert third.window_index == newer.window_index
    assert _present(mgr, [_key(0)])
    assert mgr.get_object_state(_key(1)) is None


def test_every_window_in_use_refuses_and_deletes_nothing(
    two_windows: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    mgr, leaser = two_windows
    keys = [_key(0), _key(1)]
    for key in keys:
        lease = leaser.lease(OBJECT_BYTES)
        _store(mgr, lease, [key])
        leaser.release(lease, FetchOutcome.FINISHED)
    mgr.reserve_read(keys)

    with pytest.raises(LayerwiseContractError):
        leaser.lease(OBJECT_BYTES)
    assert _present(mgr, keys)


def test_an_abandoned_window_is_quarantined_for_the_fetch_timeout(
    one_window: tuple[L1Manager, RdmaWindowLeaser], clock: _FakeClock
) -> None:
    mgr, leaser = one_window
    lease = leaser.lease(OBJECT_BYTES)
    mgr.reserve_write([_key(0)], [False], LAYOUT, mode="new", pool=lease.pool())
    mgr.abort_write([_key(0)])
    leaser.release(lease, FetchOutcome.ABANDONED)

    clock.now += FETCH_TIMEOUT - 1
    with pytest.raises(LayerwiseContractError):
        leaser.lease(OBJECT_BYTES)

    clock.now += 1
    assert leaser.lease(OBJECT_BYTES).window_index == lease.window_index


def test_a_quarantined_window_is_passed_over(
    two_windows: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    _, leaser = two_windows
    abandoned = leaser.lease(OBJECT_BYTES)
    leaser.release(abandoned, FetchOutcome.ABANDONED)

    for _ in range(3):
        lease = leaser.lease(OBJECT_BYTES)
        assert lease.window_index != abandoned.window_index
        leaser.release(lease, FetchOutcome.FINISHED)


def test_a_finished_window_is_reused_at_once(
    one_window: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    mgr, leaser = one_window
    lease = leaser.lease(OBJECT_BYTES)
    _store(mgr, lease, [_key(0)])
    leaser.release(lease, FetchOutcome.FINISHED)

    again = leaser.lease(OBJECT_BYTES)

    assert again.window_index == lease.window_index
    assert again.lease_id != lease.lease_id
    assert mgr.get_object_state(_key(0)) is None


def test_only_the_outstanding_lease_can_be_released(
    two_windows: tuple[L1Manager, RdmaWindowLeaser],
) -> None:
    _, leaser = two_windows
    stale = leaser.lease(OBJECT_BYTES)
    leaser.release(stale, FetchOutcome.FINISHED)
    with pytest.raises(ValueError):
        leaser.release(stale, FetchOutcome.FINISHED)

    current = leaser.lease(OBJECT_BYTES)
    with pytest.raises(ValueError):
        leaser.release(stale, FetchOutcome.ABANDONED)
    leaser.release(current, FetchOutcome.FINISHED)


def test_a_plan_that_disagrees_with_l1_is_refused() -> None:
    mgr = _l1(2)
    try:
        with pytest.raises(ValueError):
            RdmaWindowLeaser(mgr, _rdma_config(3))
        with pytest.raises(ValueError):
            RdmaWindowLeaser(mgr, L1RdmaConfig())
    finally:
        mgr.close()
