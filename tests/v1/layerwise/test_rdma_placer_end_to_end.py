# SPDX-License-Identifier: Apache-2.0
"""``run_pipelined_retrieve`` over Track A's real window placer and source.

``RdmaWindowPlacer`` is Track A's production ``ChunkPlacer``. The L1 is
pinned CPU memory with real RDMA windows, and the source is the native
pipelined session over the fabric-free client, so no device, fabric or
cluster is needed.
"""

# Standard
from collections.abc import Iterator, Sequence
import threading

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ObjectKey
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
    LeaseOutcome,
    ObjectToPlace,
    objects_to_place,
)

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


class _RecordingPlacer:
    """Records the leases the production placer hands out."""

    def __init__(self, placer: RdmaWindowPlacer) -> None:
        self._placer = placer
        self.leases: list[WindowPlacement] = []

    def lease(self, objects: Sequence[ObjectToPlace]) -> WindowPlacement:
        lease = self._placer.lease(objects)
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
                memory_config=L1MemoryManagerConfig(
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
            self.l1,
            RdmaWindowLeaser(self.l1, rdma, self.clock),
            GROUP_LAYOUTS,
            TEST_NODE_NAMES[0],
        )
        self.placer = _RecordingPlacer(self.rdma_placer)
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
    held_object = ObjectToPlace(0, 0, ObjectKey(b"held", "m", 0), 1)
    held = two_windows.rdma_placer.lease([held_object])
    assert held.window_start() == 0

    sink, errors, plan = two_windows.retrieve()

    assert errors == []
    (lease,) = two_windows.placer.leases
    assert lease.window_start() == WINDOW_BYTES
    assert min(s.offset for s in plan.slots) >= WINDOW_BYTES
    assert max(s.offset + s.length for s in plan.slots) <= 2 * WINDOW_BYTES
    assert sink.loaded_layers() == plan.layer_ids()
    held.release(LeaseOutcome.ABANDONED)


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
