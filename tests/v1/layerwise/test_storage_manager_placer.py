# SPDX-License-Identifier: Apache-2.0
"""A pipelined retrieve over the placer and source one ``StorageManager`` gives.

The fabric-free native client sits behind a real ``NativeConnectorL2Adapter``
inside a real ``StorageManager`` whose L1 reserves the RDMA windows. Each
retrieve takes its placer from ``pipelined_window_placer`` and its source
from ``layer_arrival_source``, as the registration and retrieve wiring do, so
the node name, the leaser and the windows all come from the storage manager.
"""

# Standard
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
import threading

# Third Party
import pytest

# First Party
from lmcache.v1.distributed import storage_manager as storage_manager_module
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
)
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.storage_manager import StorageManager
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
    ChunkPlacer,
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
    """Loads through a recording sink; asks the storage manager at reload.

    The fabric-free client has no batch reads, so a real whole-object reload
    would find nothing in L2. Instead the reload records what
    ``lock_resident_keys`` reports for the window's keys at that moment.
    """

    def __init__(self, manager: StorageManager) -> None:
        self._manager = manager
        self.sink = RecordingLayerLoadSink()
        self.reloaded: list[ObjectKey] = []
        self.absent_at_reload: tuple[ObjectKey, ...] = ()

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        return self.sink

    def wait_for_copies(self) -> None:
        """The recording sink copies nothing."""

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        self.reloaded = [o.key for o in objects]
        resident = self._manager.lock_resident_keys(self.reloaded)
        self.absent_at_reload = resident.absent
        if resident.locked:
            self._manager.finish_read_prefetched(list(resident.locked))


@dataclass
class _Retrieved:
    loader: _Loader
    plan: LayerFetchPlan
    errors: list[BaseException] = field(default_factory=list)
    completions: list[RetrieveCompletion] = field(default_factory=list)


def _request_keys() -> list[list[ObjectKey]]:
    return [list(group) for group in resolve_obj_keys(vllm_request())]


def _stored_keys() -> list[ObjectKey]:
    return [o.key for o in objects_to_place(fetch_model(), _request_keys())]


@dataclass
class _Storage:
    manager: StorageManager
    connector: FabricFreeClient

    def placer(self) -> ChunkPlacer:
        max_chunks = len(_request_keys()[0])
        return self.manager.pipelined_window_placer(
            GROUP_LAYOUTS, fetch_model(), max_chunks
        )

    def retrieve(
        self, placer: ChunkPlacer, decline_slot: int | None = None
    ) -> _Retrieved:
        """Run one retrieve, landing every slot except ``decline_slot``."""
        tap = _Tap(self.manager.layer_arrival_source())
        loader = _Loader(self.manager)
        errors: list[BaseException] = []
        completions: list[RetrieveCompletion] = []

        def run() -> None:
            try:
                result = run_pipelined_retrieve(
                    fetch_model(),
                    _request_keys(),
                    MAX_RECORD_BYTES,
                    placer,
                    tap,
                    loader,
                    poll_interval_seconds=0.001,
                )
                completions.append(result.completion)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        plan = tap.wait()
        for index in range(len(plan.slots)):
            if index == decline_slot:
                self.connector.decline_slot(index, tap.generation)
            else:
                self.connector.land_slot(index, tap.generation)
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), "retrieve did not finish"
        return _Retrieved(loader, plan, errors, completions)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Storage]:
    """A storage manager with one RDMA window over the fabric-free client.

    The ``retain`` prefetch policy keeps every fetched object in L1, so a
    test can check what a clean fetch left behind.
    """
    connector = fabric_free_connector()
    monkeypatch.setattr(
        storage_manager_module,
        "create_l2_adapter",
        lambda config, l1_memory_desc: NativeConnectorL2Adapter(
            native_client=connector, type_name="fabric-free"
        ),
    )
    rdma = L1RdmaConfig(
        transport=RdmaTransport.RC,
        window_plan=RdmaWindowPlan(window_count=1, window_bytes=WINDOW_BYTES),
        fetch_timeout_seconds=FETCH_TIMEOUT,
    )
    manager = StorageManager(
        StorageManagerConfig(
            l1_manager_config=L1ManagerConfig(
                memory_config=L1MemoryManagerConfig(
                    size_in_bytes=4 * 1024 * 1024,
                    use_lazy=False,
                    align_bytes=4096,
                    shm_name="",
                    rdma_window_count=1,
                    rdma_window_bytes=WINDOW_BYTES,
                ),
                write_ttl_seconds=600,
            ),
            eviction_config=EvictionConfig(eviction_policy="LRU"),
            l2_adapter_config=L2AdaptersConfig(
                [AerospikeL2AdapterConfig(hosts="127.0.0.1:3000", rdma=rdma)]
            ),
            prefetch_policy="retain",
        )
    )
    yield _Storage(manager, connector)
    manager.close()


def test_a_retrieve_lands_in_the_storage_managers_window(storage: _Storage) -> None:
    retrieved = storage.retrieve(storage.placer())

    assert retrieved.errors == []
    assert retrieved.completions == [RetrieveCompletion.PIPELINED]
    sink = retrieved.loader.sink
    assert sink.loaded_layers() == retrieved.plan.layer_ids()
    assert sink.finished_generations() and not sink.abandoned_generations()
    assert retrieved.plan.node_names == (storage.connector.pipelined_fetch_node_name(),)
    assert max(s.offset + s.length for s in retrieved.plan.slots) <= WINDOW_BYTES
    resident = storage.manager.lock_resident_keys(_stored_keys())
    assert set(resident.locked) == set(_stored_keys())
    storage.manager.finish_read_prefetched(list(resident.locked))


def test_placers_from_one_storage_manager_share_its_windows(
    storage: _Storage,
) -> None:
    objects = objects_to_place(fetch_model(), _request_keys())
    held = storage.placer().lease(objects[:1])

    with pytest.raises(LayerwiseContractError):
        storage.placer().lease(objects)

    held.release(LeaseOutcome.NEVER_FETCHED)
    storage.placer().lease(objects).release(LeaseOutcome.NEVER_FETCHED)


def test_a_declined_slot_reloads_free_keys_and_quarantines_the_window(
    storage: _Storage,
) -> None:
    retrieved = storage.retrieve(storage.placer(), decline_slot=0)

    assert retrieved.errors == []
    assert retrieved.completions == [RetrieveCompletion.FELL_BACK]
    assert retrieved.loader.sink.loaded_layers() == retrieved.plan.layer_ids()
    assert retrieved.loader.reloaded == _stored_keys()
    assert set(retrieved.loader.absent_at_reload) == set(_stored_keys())
    objects = objects_to_place(fetch_model(), _request_keys())
    with pytest.raises(LayerwiseContractError):
        storage.placer().lease(objects)
