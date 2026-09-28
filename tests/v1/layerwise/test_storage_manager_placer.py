# SPDX-License-Identifier: Apache-2.0
"""A pipelined retrieve over the placer and source one ``StorageManager`` gives.

The fabric-free native client sits behind a real ``NativeConnectorL2Adapter``
inside a real ``StorageManager`` whose L1 reserves the RDMA windows. Each
retrieve takes its placer from ``rdma_window_placer`` and its source from
``layer_arrival_source``, as the registration and retrieve wiring will, so
the node name, the RDMA config and the windows all come from one place.
"""

# Standard
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
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
from lmcache.v1.layerwise.pipelined_retrieve import run_pipelined_retrieve
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


def _retain_all(keys: list[ObjectKey]) -> list[bool]:
    return [True] * len(keys)


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


class _Loader:
    """Loads through a recording sink; notes what the fallback reloads."""

    def __init__(self) -> None:
        self.sink = RecordingLayerLoadSink()
        self.reloaded: list[ObjectToPlace] = []

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        return self.sink

    def wait_for_copies(self) -> None:
        """The recording sink copies nothing."""

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        self.reloaded = list(objects)


@dataclass
class _Retrieved:
    sink: RecordingLayerLoadSink
    reloaded: list[ObjectToPlace]
    errors: list[BaseException]
    plan: LayerFetchPlan


@dataclass
class _Storage:
    manager: StorageManager
    connector: FabricFreeClient

    def placer(self) -> ChunkPlacer:
        return self.manager.rdma_window_placer(GROUP_LAYOUTS, _retain_all)

    def retrieve(
        self, placer: ChunkPlacer, decline_slot: int | None = None
    ) -> _Retrieved:
        """Run one retrieve, landing every slot except ``decline_slot``."""
        tap = _Tap(self.manager.layer_arrival_source())
        loader = _Loader()
        errors: list[BaseException] = []
        keys = resolve_obj_keys(vllm_request())

        def run() -> None:
            try:
                run_pipelined_retrieve(
                    fetch_model(),
                    keys,
                    MAX_RECORD_BYTES,
                    placer,
                    tap,
                    loader,
                    poll_interval_seconds=0.001,
                )
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
        return _Retrieved(loader.sink, loader.reloaded, errors, plan)


def _stored_keys() -> list[ObjectKey]:
    objects = objects_to_place(fetch_model(), resolve_obj_keys(vllm_request()))
    return [o.key for o in objects]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Storage]:
    """A storage manager with one RDMA window and the fabric-free client."""
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
        )
    )
    yield _Storage(manager, connector)
    manager.close()


def test_a_retrieve_lands_in_the_storage_managers_window(storage: _Storage) -> None:
    retrieved = storage.retrieve(storage.placer())

    assert retrieved.errors == []
    assert retrieved.sink.loaded_layers() == retrieved.plan.layer_ids()
    assert retrieved.sink.finished_generations()
    assert not retrieved.sink.abandoned_generations()
    assert retrieved.plan.node_names == (storage.connector.pipelined_fetch_node_name(),)
    assert max(s.offset + s.length for s in retrieved.plan.slots) <= WINDOW_BYTES
    # Every retained object is in L1 and unlocked: deleting removes them all.
    keys = _stored_keys()
    assert storage.manager.delete_l1_keys(keys) == (len(keys), 0)


def test_placers_from_one_storage_manager_share_its_windows(
    storage: _Storage,
) -> None:
    objects = objects_to_place(fetch_model(), resolve_obj_keys(vllm_request()))
    held = storage.placer().lease(objects[:1])

    with pytest.raises(LayerwiseContractError):
        storage.placer().lease(objects)

    held.release(LeaseOutcome.NEVER_FETCHED)
    storage.placer().lease(objects).release(LeaseOutcome.NEVER_FETCHED)


def test_a_declined_slot_quarantines_the_window_for_every_placer(
    storage: _Storage,
) -> None:
    retrieved = storage.retrieve(storage.placer(), decline_slot=0)

    # The load is finished from whole objects rather than abandoned.
    assert retrieved.errors == []
    assert retrieved.sink.finished_generations()
    assert not retrieved.sink.abandoned_generations()
    assert [o.key for o in retrieved.reloaded] == _stored_keys()
    # The abandoned fetch's writes were aborted, so none of its objects exist.
    assert storage.manager.delete_l1_keys(_stored_keys()) == (0, 0)
    objects = objects_to_place(fetch_model(), resolve_obj_keys(vllm_request()))
    with pytest.raises(LayerwiseContractError):
        storage.placer().lease(objects)
