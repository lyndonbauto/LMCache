# SPDX-License-Identifier: Apache-2.0
"""``run_pipelined_retrieve`` over Track A's real window placer and source.

``RdmaWindowPlacer`` is Track A's production ``ChunkPlacer``. The L1 is
pinned CPU memory with real RDMA windows, and the source is the native
pipelined session over the fabric-free client, so no device, fabric or
cluster is needed.
"""

# Standard
from collections.abc import Iterator, Mapping, Sequence
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
    LayerArrivalSource,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerLoadSink,
    LayerwiseContractError,
)
from lmcache.v1.layerwise.fakes import RecordingLayerLoadSink
from lmcache.v1.layerwise.pipelined_retrieve import (
    RetrieveCompletion,
    run_pipelined_retrieve,
)
from lmcache.v1.layerwise.request_fetch import (
    LeaseOutcome,
    ObjectToPlace,
    WindowLease,
    objects_to_place,
)

# Local
from .aerospike_harness import WINDOW_BYTES, FabricFreeClient, fabric_free_connector
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

    def read_write_ids(self, cache_keys: Sequence[str]) -> Mapping[str, str]:
        return self._source.read_write_ids(cache_keys)

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


class _Loader:
    """Loads through a recording sink; notes each key's L1 state at reload."""

    def __init__(self, l1: L1Manager) -> None:
        self._l1 = l1
        self.sink = RecordingLayerLoadSink()
        self.reloaded: list[ObjectToPlace] = []
        self.key_states_at_reload: list[L1Error] = []

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        return self.sink

    def wait_for_copies(self) -> None:
        """The recording sink copies nothing."""

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        self.reloaded = list(objects)
        results = self._l1.reserve_read([o.key for o in objects])
        self.key_states_at_reload = [err for err, _ in results.values()]
        locked = [key for key, (err, _) in results.items() if err == L1Error.SUCCESS]
        if locked:
            self._l1.finish_read(locked)


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
        self.connector: FabricFreeClient = fabric_free_connector(window_count)
        # Retain every fetched object, so a test can read back what landed.
        self.rdma_placer = RdmaWindowPlacer(
            self.l1,
            RdmaWindowLeaser(self.l1, rdma, self.clock),
            GROUP_LAYOUTS,
            self.connector.pipelined_fetch_node_name(),
            lambda keys: [True] * len(keys),
        )
        self.placer = _RecordingPlacer(self.rdma_placer)
        self.keys = resolve_obj_keys(vllm_request())
        self.completions: list[RetrieveCompletion] = []

    def readable(self, key: ObjectKey) -> bool:
        err, _ = self.l1.reserve_read([key])[key]
        if err != L1Error.SUCCESS:
            return False
        self.l1.finish_read([key])
        return True

    def retrieve(
        self, decline_slot: int | None = None
    ) -> tuple[_Loader, list[BaseException], LayerFetchPlan]:
        """Run one retrieve, landing every slot except ``decline_slot``."""
        source = AerospikeLayerArrivalSource(
            self.connector, NativePlanIssuer(self.connector)
        )
        tap = _Tap(source)
        loader = _Loader(self.l1)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                result = run_pipelined_retrieve(
                    fetch_model(),
                    self.keys,
                    MAX_RECORD_BYTES,
                    self.placer,
                    tap,
                    loader,
                    poll_interval_seconds=0.001,
                )
                self.completions.append(result.completion)
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
        return loader, errors, plan


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
    loader, errors, plan = one_window.retrieve()

    assert errors == []
    assert one_window.completions == [RetrieveCompletion.PIPELINED]
    sink = loader.sink
    assert sink.loaded_layers() == plan.layer_ids()
    assert sink.finished_generations() and not sink.abandoned_generations()
    assert all(one_window.readable(k) for k in _stored_keys())


def test_a_later_window_is_planned_with_slab_offsets(two_windows: _Setup) -> None:
    held_object = ObjectToPlace(0, 0, ObjectKey(b"held", "m", 0), 1)
    held = two_windows.rdma_placer.lease([held_object])
    assert held.window_start() == 0

    loader, errors, plan = two_windows.retrieve()

    assert errors == []
    (lease,) = two_windows.placer.leases
    assert lease.window_start() == WINDOW_BYTES
    assert min(s.offset for s in plan.slots) >= WINDOW_BYTES
    assert max(s.offset + s.length for s in plan.slots) <= 2 * WINDOW_BYTES
    assert loader.sink.loaded_layers() == plan.layer_ids()
    held.release(LeaseOutcome.ABANDONED)


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
    """The load finishes from whole objects, whose keys the window freed."""
    loader, errors, plan = one_window.retrieve(decline_slot=0)

    assert errors == []
    assert one_window.completions == [RetrieveCompletion.FELL_BACK]
    assert loader.sink.loaded_layers() == plan.layer_ids()
    assert loader.sink.finished_generations()
    assert not loader.sink.abandoned_generations()
    assert [o.key for o in loader.reloaded] == _stored_keys()
    assert set(loader.key_states_at_reload) == {L1Error.KEY_NOT_EXIST}
    assert not any(one_window.readable(k) for k in _stored_keys())
    with pytest.raises(LayerwiseContractError):
        one_window.placer.lease(objects_to_place(fetch_model(), one_window.keys))

    one_window.clock.now += FETCH_TIMEOUT + 1
    one_window.placer.lease(objects_to_place(fetch_model(), one_window.keys))
