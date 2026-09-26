# SPDX-License-Identifier: Apache-2.0
"""``run_pipelined_retrieve`` over Track A's real window placer and source.

The adapter below is the shape Track A's production ``ChunkPlacer`` needs;
it moves into production code with that placer. The L1 is pinned CPU memory
with real RDMA windows, and the source is the native pipelined session over
the fabric-free client, so no device, fabric or cluster is needed.

Skipped until Track A's placer is on the branch.
"""

# Standard
from collections.abc import Iterator, Sequence
import threading

# Third Party
import pytest

pytest.importorskip("lmcache.v1.distributed.l2_adapters.rdma_window_placer")

# First Party
from lmcache.v1.distributed.api import ObjectKey  # noqa: E402
from lmcache.v1.distributed.config import L1ManagerConfig, L1MemoryManagerConfig
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.layerwise_source import (
    AerospikeLayerArrivalSource,
    NativePlanIssuer,
)
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.l2_adapters.rdma_window_leaser import RdmaWindowLeaser
from lmcache.v1.distributed.l2_adapters.rdma_window_placer import (
    ObjectToPlace as RdmaObjectToPlace,
)
from lmcache.v1.distributed.l2_adapters.rdma_window_placer import (
    RdmaWindowPlacer,
    WindowPlacement,
)
from lmcache.v1.layerwise import (
    NO_GENERATION,
    LayerArrivalPump,
    LayerArrivalSource,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerwiseContractError,
)
from lmcache.v1.layerwise.fakes import RecordingLayerLoadSink
from lmcache.v1.layerwise.pipelined_retrieve import run_pipelined_retrieve
from lmcache.v1.layerwise.request_fetch import (
    ChunkLocation,
    LeaseOutcome,
    ObjectToPlace,
    objects_to_place,
)
from lmcache.v1.memory_management import MemoryObj

# Local
from .aerospike_harness import WINDOW_BYTES, FabricFreeClient, fabric_free_connector
from .conftest import TEST_NODE_NAMES
from .vllm_requests import (
    GROUP_LAYOUTS,
    MAX_RECORD_BYTES,
    fetch_model,
    resolve_obj_keys,
    vllm_request,
)

FETCH_TIMEOUT = 30.0
JOIN_TIMEOUT = 10.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _RdmaLease:
    """Our ``WindowLease`` over Track A's ``WindowPlacement``."""

    def __init__(self, placement: WindowPlacement, node_name: str) -> None:
        self.placement = placement
        self._node_name = node_name

    def window_start(self) -> int:
        return self.placement.lease.base_offset

    def window_bytes(self) -> int:
        return self.placement.lease.size_bytes

    def locate(self, chunk_id: int, object_group_id: int) -> ChunkLocation:
        return ChunkLocation(
            self._node_name, self.placement.dest_offset(chunk_id, object_group_id)
        )

    def memory_obj(self, chunk_id: int, object_group_id: int) -> MemoryObj:
        return self.placement.memory_obj(chunk_id, object_group_id)

    def release(self, outcome: LeaseOutcome) -> None:
        if outcome == LeaseOutcome.FINISHED:
            self.placement.complete()
        else:
            self.placement.abandon()


class _RdmaPlacer:
    """Our ``ChunkPlacer`` over Track A's ``RdmaWindowPlacer``; one node."""

    def __init__(self, placer: RdmaWindowPlacer, node_name: str) -> None:
        self._placer = placer
        self._node_name = node_name
        self.leases: list[_RdmaLease] = []

    def lease(self, objects: Sequence[ObjectToPlace]) -> _RdmaLease:
        placement = self._placer.place(
            [RdmaObjectToPlace(o.chunk_id, o.object_group_id, o.key) for o in objects],
            GROUP_LAYOUTS,
        )
        lease = _RdmaLease(placement, self._node_name)
        self.leases.append(lease)
        return lease


class _Tap:
    """Publishes the generation the pump begins, for the driving thread."""

    def __init__(self, source: LayerArrivalSource) -> None:
        self._source = source
        self._begun = threading.Event()
        self.generation = NO_GENERATION
        self.plan: LayerFetchPlan | None = None

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        self.plan = plan
        self.generation = self._source.begin_fetch(plan)
        self._begun.set()
        return self.generation

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        return self._source.poll_layer(layer_id, generation)

    def finish_fetch(self, generation: int) -> None:
        self._source.finish_fetch(generation)

    def abandon_fetch(self, generation: int) -> None:
        self._source.abandon_fetch(generation)

    def wait(self) -> LayerFetchPlan:
        if not self._begun.wait(JOIN_TIMEOUT) or self.plan is None:
            raise AssertionError("the pump never began a fetch")
        return self.plan


class _Setup:
    def __init__(self, window_count: int) -> None:
        self.clock = _Clock()
        self.l1 = L1Manager(
            L1ManagerConfig(
                # The rdma_window_* fields come with Track A's L1 windows.
                memory_config=L1MemoryManagerConfig(  # type: ignore[call-arg]
                    size_in_bytes=4 * 1024 * 1024,
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
        self.rdma_placer = RdmaWindowPlacer(
            self.l1, RdmaWindowLeaser(self.l1, rdma, self.clock)
        )
        self.placer = _RdmaPlacer(self.rdma_placer, TEST_NODE_NAMES[0])
        self.connector: FabricFreeClient = fabric_free_connector(window_count)
        self.keys = resolve_obj_keys(vllm_request())

    def readable(self, key: ObjectKey) -> bool:
        err, _ = self.l1.reserve_read([key])[key]
        if err != L1Error.SUCCESS:
            return False
        self.l1.finish_read([key])
        return True

    def retrieve(
        self, decline_slot: int | None = None
    ) -> tuple[RecordingLayerLoadSink, list[BaseException], LayerFetchPlan]:
        """Run one retrieve, landing every slot except ``decline_slot``."""
        source = AerospikeLayerArrivalSource(
            self.connector, NativePlanIssuer(self.connector)
        )
        tap = _Tap(source)
        sink = RecordingLayerLoadSink()
        pump = LayerArrivalPump(tap, sink, poll_interval_seconds=0.001)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                run_pipelined_retrieve(
                    fetch_model(), self.keys, MAX_RECORD_BYTES, self.placer, pump
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        plan = tap.wait()
        for index in reversed(range(len(plan.slots))):
            if index == decline_slot:
                self.connector.decline_slot(index, tap.generation)
            else:
                self.connector.land_slot(index, tap.generation)
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), "retrieve did not finish"
        return sink, errors, plan


def _stored_keys() -> list[ObjectKey]:
    objects = objects_to_place(fetch_model(), resolve_obj_keys(vllm_request()))
    return [o.key for o in objects]


@pytest.fixture
def one_window() -> Iterator[_Setup]:
    setup = _Setup(window_count=1)
    yield setup
    setup.l1.close()


@pytest.fixture
def two_windows() -> Iterator[_Setup]:
    setup = _Setup(window_count=2)
    yield setup
    setup.l1.close()


def test_a_landed_retrieve_loads_every_layer_and_keeps_the_data(
    one_window: _Setup,
) -> None:
    sink, errors, plan = one_window.retrieve()

    assert errors == []
    assert sink.loaded_layers() == plan.layer_ids()
    assert sink.finished_generations() and not sink.abandoned_generations()
    assert all(one_window.readable(k) for k in _stored_keys())


def test_a_later_window_is_planned_with_slab_offsets(two_windows: _Setup) -> None:
    held = two_windows.rdma_placer.place(
        [RdmaObjectToPlace(0, 0, ObjectKey(b"held", "m", 0))], GROUP_LAYOUTS
    )
    assert held.lease.base_offset == 0

    sink, errors, plan = two_windows.retrieve()

    assert errors == []
    (lease,) = two_windows.placer.leases
    assert lease.window_start() == WINDOW_BYTES
    assert min(s.offset for s in plan.slots) >= WINDOW_BYTES
    assert max(s.offset + s.length for s in plan.slots) <= 2 * WINDOW_BYTES
    assert sink.loaded_layers() == plan.layer_ids()
    held.abandon()


def test_each_objects_memory_sits_at_its_planned_offset(one_window: _Setup) -> None:
    objects = objects_to_place(fetch_model(), one_window.keys)
    lease = one_window.placer.lease(objects)

    for obj in objects:
        location = lease.locate(obj.chunk_id, obj.object_group_id)
        memory_obj = lease.memory_obj(obj.chunk_id, obj.object_group_id)
        assert memory_obj.meta.address == location.dest_offset
        assert memory_obj.get_size() >= obj.object_bytes
    lease.release(LeaseOutcome.NEVER_FETCHED)


def test_a_declined_slot_falls_back_and_quarantines_the_window(
    one_window: _Setup,
) -> None:
    sink, errors, _ = one_window.retrieve(decline_slot=0)

    assert len(errors) == 1 and isinstance(errors[0], LayerwiseContractError)
    assert sink.abandoned_generations()
    assert not any(one_window.readable(k) for k in _stored_keys())
    with pytest.raises(LayerwiseContractError):
        one_window.placer.lease(objects_to_place(fetch_model(), one_window.keys))

    one_window.clock.now += FETCH_TIMEOUT + 1
    one_window.placer.lease(objects_to_place(fetch_model(), one_window.keys))
