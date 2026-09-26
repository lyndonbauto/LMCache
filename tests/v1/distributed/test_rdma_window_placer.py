# SPDX-License-Identifier: Apache-2.0
"""Tests for ``RdmaWindowPlacer`` and ``WindowPlacement`` against their
docstring contracts and the layerwise ``ChunkPlacer``/``WindowLease``
protocols they implement.

The L1 is small pinned CPU memory with real RDMA windows, so no GPU or RDMA
device is needed. Time comes from a fake clock.
"""

# Standard
from collections.abc import Callable, Iterator

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import AttnWindowDesc, MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.config import L1ManagerConfig, L1MemoryManagerConfig
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.l2_adapters.rdma_window_leaser import RdmaWindowLeaser
from lmcache.v1.distributed.internal_api import L1ManagerListener
from lmcache.v1.distributed.l2_adapters.rdma_window_placer import (
    RdmaWindowPlacer,
    check_window_holds_request,
    retain_none,
)
from lmcache.v1.layerwise.contract import LayerwiseContractError, PlanTooLargeError
from lmcache.v1.layerwise.planner import ModelLayout
from lmcache.v1.layerwise.request_fetch import (
    ChunkPlacer,
    FetchModel,
    LeaseOutcome,
    ObjectToPlace,
    WindowLease,
)

WINDOW_BYTES = 256 * 1024
SLAB_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT = 30.0
NODE = "BB9000000000001"
# Group 0 objects are 64 KiB, group 1 objects 32 KiB.
LAYOUTS = {
    0: MemoryLayoutDesc(shapes=[torch.Size([16, 1024])], dtypes=[torch.float32]),
    1: MemoryLayoutDesc(shapes=[torch.Size([8, 1024])], dtypes=[torch.float32]),
}
GROUP_BYTES = {0: 64 * 1024, 1: 32 * 1024}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _WriteFinishedRecorder(L1ManagerListener):
    """Records the keys the store controller would store to L2."""

    def __init__(self) -> None:
        self.stored: list[ObjectKey] = []

    def on_l1_keys_reserved_read(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_read_finished(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_reserved_write(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_write_finished(self, keys: list[ObjectKey]) -> None:
        self.stored.extend(keys)

    def on_l1_keys_finish_write_and_reserve_read(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_deleted_by_manager(self, keys: list[ObjectKey]) -> None:
        pass

    def on_l1_keys_accessed(self, keys: list[ObjectKey]) -> None:
        pass


def _retain_all(keys: list[ObjectKey]) -> list[bool]:
    return [True] * len(keys)


class _Setup:
    def __init__(
        self,
        window_count: int,
        select_retentions: Callable[[list[ObjectKey]], list[bool]] = retain_none,
    ) -> None:
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
        self.recorder = _WriteFinishedRecorder()
        self.l1.register_listener(self.recorder)
        self.placer = RdmaWindowPlacer(
            self.l1,
            RdmaWindowLeaser(self.l1, rdma, self.clock),
            LAYOUTS,
            NODE,
            select_retentions,
        )


def _key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)


def _obj(chunk_id: int, group: int, key_id: int) -> ObjectToPlace:
    return ObjectToPlace(chunk_id, group, _key(key_id), GROUP_BYTES[group])


def _objects(chunks: int, groups: tuple[int, ...] = (0,)) -> list[ObjectToPlace]:
    return [_obj(c, g, 100 * g + c) for c in range(chunks) for g in groups]


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


def test_the_placer_and_placement_implement_the_layerwise_protocols(
    one_window: _Setup,
) -> None:
    assert isinstance(one_window.placer, ChunkPlacer)
    assert isinstance(one_window.placer.lease(_objects(chunks=1)), WindowLease)


def test_every_object_lands_in_the_leased_window(one_window: _Setup) -> None:
    objects = _objects(chunks=2, groups=(0, 1))

    placement = one_window.placer.lease(objects)

    start, size = placement.window_start(), placement.window_bytes()
    assert size == WINDOW_BYTES
    offsets = set()
    for obj in objects:
        location = placement.locate(obj.chunk_id, obj.object_group_id)
        memory_obj = placement.memory_obj(obj.chunk_id, obj.object_group_id)
        assert location.node_name == NODE
        assert location.dest_offset == memory_obj.meta.address
        assert start <= location.dest_offset
        assert location.dest_offset + obj.object_bytes <= start + size
        offsets.add(location.dest_offset)
    assert len(offsets) == len(objects)
    assert placement.keys() == [o.key for o in objects]


def test_retained_objects_stay_unreadable_until_finished() -> None:
    setup = _Setup(window_count=1, select_retentions=_retain_all)
    try:
        objects = _objects(chunks=2)
        placement = setup.placer.lease(objects)

        assert not any(_readable(setup.l1, o.key) for o in objects)

        placement.release(LeaseOutcome.FINISHED)

        assert all(_readable(setup.l1, o.key) for o in objects)
    finally:
        setup.l1.close()


def test_by_default_a_finished_lease_frees_its_objects(one_window: _Setup) -> None:
    objects = _objects(chunks=2)

    one_window.placer.lease(objects).release(LeaseOutcome.FINISHED)

    assert all(one_window.l1.get_object_state(o.key) is None for o in objects)
    assert one_window.l1.get_rdma_window_object_count(0) == 0


def test_only_the_selected_objects_are_retained() -> None:
    objects = _objects(chunks=2)
    kept = objects[1].key
    setup = _Setup(
        window_count=1, select_retentions=lambda keys: [k == kept for k in keys]
    )
    try:
        setup.placer.lease(objects).release(LeaseOutcome.FINISHED)

        assert _readable(setup.l1, kept)
        assert setup.l1.get_object_state(objects[0].key) is None
    finally:
        setup.l1.close()


@pytest.mark.parametrize("select_retentions", [retain_none, _retain_all])
def test_a_finished_lease_stores_nothing_back_to_l2(
    select_retentions: Callable[[list[ObjectKey]], list[bool]],
) -> None:
    setup = _Setup(window_count=1, select_retentions=select_retentions)
    try:
        setup.placer.lease(_objects(chunks=2)).release(LeaseOutcome.FINISHED)

        assert setup.recorder.stored == []
    finally:
        setup.l1.close()


def test_a_wrong_number_of_retentions_is_refused_without_leasing() -> None:
    setup = _Setup(window_count=1, select_retentions=lambda keys: [True])
    try:
        with pytest.raises(ValueError, match="retentions"):
            setup.placer.lease(_objects(chunks=2))

        assert setup.l1.get_rdma_window_object_count(0) == 0
    finally:
        setup.l1.close()


def test_a_finished_window_is_leased_again_at_once(one_window: _Setup) -> None:
    first = _objects(chunks=2)
    one_window.placer.lease(first).release(LeaseOutcome.FINISHED)

    placement = one_window.placer.lease([_obj(0, 0, 999)])

    assert placement.window_start() == 0
    assert all(one_window.l1.get_object_state(o.key) is None for o in first)


def test_never_fetched_deletes_the_objects_and_frees_the_window_at_once(
    one_window: _Setup,
) -> None:
    objects = _objects(chunks=2)
    one_window.placer.lease(objects).release(LeaseOutcome.NEVER_FETCHED)

    assert all(one_window.l1.get_object_state(o.key) is None for o in objects)
    assert one_window.l1.get_rdma_window_object_count(0) == 0
    one_window.placer.lease(objects)


def test_abandoned_deletes_the_objects_and_quarantines_the_window(
    one_window: _Setup,
) -> None:
    objects = _objects(chunks=2)
    one_window.placer.lease(objects).release(LeaseOutcome.ABANDONED)

    assert all(one_window.l1.get_object_state(o.key) is None for o in objects)
    with pytest.raises(LayerwiseContractError):
        one_window.placer.lease(objects)

    one_window.clock.now += FETCH_TIMEOUT
    one_window.placer.lease(objects)


def test_a_request_larger_than_a_window_is_too_large(one_window: _Setup) -> None:
    too_many = _objects(chunks=WINDOW_BYTES // GROUP_BYTES[0] + 1)

    with pytest.raises(PlanTooLargeError):
        one_window.placer.lease(too_many)

    one_window.placer.lease(_objects(chunks=1))


def test_an_already_cached_key_fails_the_whole_placement(one_window: _Setup) -> None:
    objects = _objects(chunks=2, groups=(0, 1))
    cached = objects[-1].key
    one_window.l1.reserve_write([cached], [False], LAYOUTS[1], mode="new")
    one_window.l1.finish_write([cached])

    with pytest.raises(LayerwiseContractError) as refused:
        one_window.placer.lease(objects)

    assert not isinstance(refused.value, PlanTooLargeError)
    others = [o.key for o in objects if o.key != cached]
    assert all(one_window.l1.get_object_state(key) is None for key in others)
    assert one_window.l1.get_rdma_window_object_count(0) == 0
    one_window.placer.lease(_objects(chunks=1))


def test_one_placement_per_window_at_a_time() -> None:
    setup = _Setup(window_count=2)
    try:
        first = setup.placer.lease(_objects(chunks=1))
        second = setup.placer.lease([_obj(0, 0, 7)])
        assert first.window_start() != second.window_start()
        with pytest.raises(LayerwiseContractError):
            setup.placer.lease([_obj(0, 0, 8)])
        first.release(LeaseOutcome.FINISHED)
        setup.placer.lease([_obj(0, 0, 8)])
    finally:
        setup.l1.close()


@pytest.mark.parametrize("second", list(LeaseOutcome))
def test_a_placement_is_released_once(one_window: _Setup, second: LeaseOutcome) -> None:
    placement = one_window.placer.lease(_objects(chunks=1))
    placement.release(LeaseOutcome.FINISHED)

    with pytest.raises(ValueError):
        placement.release(second)


def test_an_unplaced_object_cannot_be_located(one_window: _Setup) -> None:
    placement = one_window.placer.lease(_objects(chunks=1))

    with pytest.raises(KeyError):
        placement.locate(5, 0)


@pytest.mark.parametrize(
    "objects",
    [
        [],
        [_obj(0, 0, 1), _obj(0, 0, 2)],
        [ObjectToPlace(0, 7, _key(1), 1024)],
        [ObjectToPlace(0, 1, _key(1), GROUP_BYTES[1] + 1)],
    ],
    ids=["empty", "repeated-pair", "missing-layout", "larger-than-layout"],
)
def test_bad_input_is_refused_without_leasing(
    one_window: _Setup, objects: list[ObjectToPlace]
) -> None:
    with pytest.raises(ValueError):
        one_window.placer.lease(objects)

    one_window.placer.lease(_objects(chunks=1))


def _two_group_model() -> FetchModel:
    # One layer per group, as (K/V, layers, tokens, hidden); same sizes as
    # LAYOUTS.
    kernel_layouts = {
        0: MemoryLayoutDesc([torch.Size([2, 1, 16, 512])], [torch.float32]),
        1: MemoryLayoutDesc([torch.Size([2, 1, 16, 256])], [torch.float32]),
    }
    return FetchModel(
        ModelLayout.from_registration(kernel_layouts, {0: [[0]], 1: [[1]]}),
        AttnWindowDesc(
            num_chunks_in_sw=[-1, -1],
            world_size=1,
            group_kinds=("attention", "attention"),
        ),
    )


def test_a_window_that_holds_the_largest_retrieve_passes_the_check() -> None:
    chunks = WINDOW_BYTES // (GROUP_BYTES[0] + GROUP_BYTES[1])

    check_window_holds_request(WINDOW_BYTES, _two_group_model(), chunks, 4096)


def test_a_small_window_fails_the_check_with_the_size_needed() -> None:
    chunks = WINDOW_BYTES // (GROUP_BYTES[0] + GROUP_BYTES[1]) + 1
    needed = chunks * (GROUP_BYTES[0] + GROUP_BYTES[1])

    with pytest.raises(ValueError, match=f"at least {needed}"):
        check_window_holds_request(WINDOW_BYTES, _two_group_model(), chunks, 4096)


def test_the_check_needs_a_positive_chunk_count() -> None:
    model = _two_group_model()

    with pytest.raises(ValueError, match="max_pipelined_chunks"):
        check_window_holds_request(WINDOW_BYTES, model, 0, 4096)


@pytest.mark.parametrize("layouts, node", [({}, NODE), (LAYOUTS, "")])
def test_a_placer_needs_layouts_and_a_node(
    one_window: _Setup, layouts: dict[int, MemoryLayoutDesc], node: str
) -> None:
    leaser = RdmaWindowLeaser(
        one_window.l1,
        L1RdmaConfig(
            transport=RdmaTransport.RC,
            window_plan=RdmaWindowPlan(window_count=1, window_bytes=WINDOW_BYTES),
            fetch_timeout_seconds=FETCH_TIMEOUT,
        ),
        one_window.clock,
    )

    with pytest.raises(ValueError):
        RdmaWindowPlacer(one_window.l1, leaser, layouts, node)
