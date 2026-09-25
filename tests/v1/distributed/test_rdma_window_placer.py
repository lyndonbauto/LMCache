# SPDX-License-Identifier: Apache-2.0
"""Tests for ``RdmaWindowPlacer`` and ``WindowPlacement`` against their
docstring contracts.

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
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.l2_adapters.rdma_window_leaser import RdmaWindowLeaser
from lmcache.v1.distributed.l2_adapters.rdma_window_placer import (
    ObjectToPlace,
    RdmaWindowPlacer,
)
from lmcache.v1.layerwise.contract import LayerwiseContractError, PlanTooLargeError

WINDOW_BYTES = 256 * 1024
SLAB_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT = 30.0
# Group 0 objects are 64 KiB, group 1 objects 32 KiB.
LAYOUTS = {
    0: MemoryLayoutDesc(shapes=[torch.Size([16, 1024])], dtypes=[torch.float32]),
    1: MemoryLayoutDesc(shapes=[torch.Size([8, 1024])], dtypes=[torch.float32]),
}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Setup:
    def __init__(self, window_count: int) -> None:
        self.clock = _FakeClock()
        self.l1 = L1Manager(
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
        rdma = L1RdmaConfig(
            transport=RdmaTransport.RC,
            window_plan=RdmaWindowPlan(
                window_count=window_count, window_bytes=WINDOW_BYTES
            ),
            fetch_timeout_seconds=FETCH_TIMEOUT,
        )
        self.placer = RdmaWindowPlacer(
            self.l1, RdmaWindowLeaser(self.l1, rdma, self.clock)
        )


def _key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)


def _objects(chunks: int, groups: tuple[int, ...] = (0,)) -> list[ObjectToPlace]:
    return [
        ObjectToPlace(chunk_id=c, object_group_id=g, key=_key(100 * g + c))
        for c in range(chunks)
        for g in groups
    ]


def _readable(l1: L1Manager, key: ObjectKey) -> bool:
    err, _ = l1.reserve_read([key])[key]
    if err != L1Error.SUCCESS:
        return False
    l1.finish_read([key])
    return True


@pytest.fixture
def one_window() -> Iterator[_Setup]:
    setup = _Setup(window_count=1)
    yield setup
    setup.l1.close()


def test_every_object_lands_in_the_leased_window(one_window: _Setup) -> None:
    objects = _objects(chunks=2, groups=(0, 1))

    placement = one_window.placer.place(objects, LAYOUTS)

    lease = placement.lease
    offsets = set()
    for obj in objects:
        offset = placement.dest_offset(obj.chunk_id, obj.object_group_id)
        memory_obj = placement.memory_obj(obj.chunk_id, obj.object_group_id)
        assert offset == memory_obj.meta.address
        assert lease.base_offset <= offset < lease.base_offset + lease.size_bytes
        offsets.add(offset)
    assert len(offsets) == len(objects)
    assert placement.keys() == [o.key for o in objects]


def test_placed_objects_stay_unreadable_until_complete(one_window: _Setup) -> None:
    objects = _objects(chunks=2)
    placement = one_window.placer.place(objects, LAYOUTS)

    assert not any(_readable(one_window.l1, o.key) for o in objects)

    placement.complete()

    assert all(_readable(one_window.l1, o.key) for o in objects)


def test_a_completed_window_is_leased_again_at_once(one_window: _Setup) -> None:
    first = _objects(chunks=2)
    one_window.placer.place(first, LAYOUTS).complete()

    second = [ObjectToPlace(0, 0, _key(999))]
    placement = one_window.placer.place(second, LAYOUTS)

    assert placement.lease.window_index == 0
    assert all(one_window.l1.get_object_state(o.key) is None for o in first)


def test_abandon_deletes_the_objects_and_quarantines_the_window(
    one_window: _Setup,
) -> None:
    objects = _objects(chunks=2)
    one_window.placer.place(objects, LAYOUTS).abandon()

    assert all(one_window.l1.get_object_state(o.key) is None for o in objects)
    with pytest.raises(LayerwiseContractError):
        one_window.placer.place(objects, LAYOUTS)

    one_window.clock.now += FETCH_TIMEOUT
    one_window.placer.place(objects, LAYOUTS)


def test_a_request_larger_than_a_window_is_too_large(one_window: _Setup) -> None:
    too_many = _objects(chunks=WINDOW_BYTES // (64 * 1024) + 1)

    with pytest.raises(PlanTooLargeError):
        one_window.placer.place(too_many, LAYOUTS)

    one_window.placer.place(_objects(chunks=1), LAYOUTS)


def test_an_already_cached_key_fails_the_whole_placement(one_window: _Setup) -> None:
    objects = _objects(chunks=2, groups=(0, 1))
    cached = objects[-1].key
    one_window.l1.reserve_write([cached], [False], LAYOUTS[1], mode="new")
    one_window.l1.finish_write([cached])

    with pytest.raises(LayerwiseContractError) as refused:
        one_window.placer.place(objects, LAYOUTS)

    assert not isinstance(refused.value, PlanTooLargeError)
    others = [o.key for o in objects if o.key != cached]
    assert all(one_window.l1.get_object_state(key) is None for key in others)
    assert one_window.l1.get_rdma_window_object_count(0) == 0
    one_window.placer.place(_objects(chunks=1), LAYOUTS)


def test_one_placement_per_window_at_a_time() -> None:
    setup = _Setup(window_count=2)
    try:
        first = setup.placer.place(_objects(chunks=1), LAYOUTS)
        second = setup.placer.place([ObjectToPlace(0, 0, _key(7))], LAYOUTS)
        assert first.lease.window_index != second.lease.window_index
        with pytest.raises(LayerwiseContractError):
            setup.placer.place([ObjectToPlace(0, 0, _key(8))], LAYOUTS)
        first.complete()
        setup.placer.place([ObjectToPlace(0, 0, _key(8))], LAYOUTS)
    finally:
        setup.l1.close()


@pytest.mark.parametrize("second", ["complete", "abandon"])
def test_a_placement_ends_once(one_window: _Setup, second: str) -> None:
    placement = one_window.placer.place(_objects(chunks=1), LAYOUTS)
    placement.complete()

    with pytest.raises(ValueError):
        getattr(placement, second)()


def test_an_unplaced_object_has_no_offset(one_window: _Setup) -> None:
    placement = one_window.placer.place(_objects(chunks=1), LAYOUTS)

    with pytest.raises(KeyError):
        placement.dest_offset(5, 0)


@pytest.mark.parametrize(
    "objects",
    [
        [],
        [ObjectToPlace(0, 0, _key(1)), ObjectToPlace(0, 0, _key(2))],
        [ObjectToPlace(0, 7, _key(1))],
    ],
    ids=["empty", "repeated-pair", "missing-layout"],
)
def test_bad_input_is_refused_without_leasing(
    one_window: _Setup, objects: list[ObjectToPlace]
) -> None:
    with pytest.raises(ValueError):
        one_window.placer.place(objects, LAYOUTS)

    one_window.placer.place(_objects(chunks=1), LAYOUTS)
