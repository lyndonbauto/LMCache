# SPDX-License-Identifier: Apache-2.0
"""Pipelined RDMA fetches against a real Aerospike server (A8).

A real ``StorageManager`` holds a real Aerospike adapter with RDMA reception
on a real device, so every step of a layerwise retrieve is production code:
the window registration (``kv-sink-register``), the placer, the plan, the
``kv-sink-fetch-pipelined`` commands, the server's RDMA writes with
immediate, and the completions. The objects are stored first through a
plain adapter on the same set, then fetched, and every byte the fetch left
in L1 is compared with what was stored.

Requires a server with the kv-sink commands (aerospike-server branch
``feat/kv-sink-fetch-pipelined``), an RDMA device the server also opens
(Soft-RoCE is enough), and the ``BUILD_AEROSPIKE=1`` extension built with
RDMA support. Skipped otherwise; stock CE has no kv-sink commands, so CI's
Docker server does not run these. On the Soft-RoCE VM::

    RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 \\
    AEROSPIKE_TEST_PORT=3000 AEROSPIKE_TEST_NAMESPACE=lmcache \\
    RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \\
    pytest tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py

On AWS EFA, build with ``BUILD_WITH_AEROSPIKE_EFA=1`` and run with
``RDMA_TRANSPORT=SRD RDMA_DEVICE=<efa device> RDMA_GID_INDEX=0``.
"""

# Standard
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
import os
import select
import uuid

# Third Party
import pytest
import torch

# First Party
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
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.layerwise import (
    LayerArrivalSource,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerLoadSink,
)
from lmcache.v1.layerwise.fakes import RecordingLayerLoadSink
from lmcache.v1.layerwise.pipelined_retrieve import (
    RetrieveCompletion,
    run_pipelined_retrieve,
)
from lmcache.v1.layerwise.request_fetch import (
    ObjectToPlace,
    WindowLease,
    objects_to_place,
)
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd

# Local
from ..layerwise.vllm_requests import (
    GROUP_LAYOUTS,
    KERNEL_LAYERS,
    fetch_model,
    resolve_obj_keys,
    vllm_request,
)

AEROSPIKE_HOST = os.environ.get("AEROSPIKE_TEST_HOST", "127.0.0.1")
AEROSPIKE_PORT = int(os.environ.get("AEROSPIKE_TEST_PORT", "3000"))
AEROSPIKE_NAMESPACE = os.environ.get("AEROSPIKE_TEST_NAMESPACE", "lmcache")
RUN_AEROSPIKE_IT = os.environ.get("RUN_AEROSPIKE_INTEGRATION") == "1"
RDMA_DEVICE = os.environ.get("RDMA_DEVICE", "")
RDMA_GID_INDEX = int(os.environ.get("RDMA_GID_INDEX", "0"))
#: ``RC`` (Soft-RoCE, the default) or ``SRD`` (AWS EFA).
RDMA_TRANSPORT = RdmaTransport[os.environ.get("RDMA_TRANSPORT", "RC").upper()]

#: One window holds the request's five full-attention chunks and two
#: sliding-window chunks, page-aligned.
WINDOW_BYTES = 1 << 16
FETCH_TIMEOUT = 30.0
STORE_TIMEOUT = 30.0
#: ``MAX_REGIONS`` in the server's ``as/src/base/kv_sink.c``.
SERVER_MAX_REGIONS = 16


def _aerospike_available() -> bool:
    if not RUN_AEROSPIKE_IT:
        return False
    try:
        # Third Party
        import aerospike

        client = aerospike.client(
            {"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}
        ).connect()
        info = client.info_random_node(f"namespace/{AEROSPIKE_NAMESPACE}")
        client.close()
        return "nsup-period" in info
    except Exception:
        return False


def _native_rdma_available() -> bool:
    try:
        # First Party
        from lmcache.lmcache_aerospike import L1RdmaRegistration
    except ImportError:
        return False
    return hasattr(L1RdmaRegistration(), "transport")


pytestmark = [
    pytest.mark.skipif(
        not RDMA_DEVICE,
        reason="no RDMA device named (set RDMA_DEVICE, and RDMA_GID_INDEX)",
    ),
    pytest.mark.skipif(
        not _aerospike_available(),
        reason=(
            f"Aerospike not available at {AEROSPIKE_HOST}:{AEROSPIKE_PORT} "
            "(set RUN_AEROSPIKE_INTEGRATION=1)"
        ),
    ),
    pytest.mark.skipif(
        not _native_rdma_available(),
        reason="lmcache.lmcache_aerospike extension not built with RDMA",
    ),
]


class _PlanTap:
    """Passes a retrieve's source calls through, keeping the plan it began."""

    def __init__(self, source: LayerArrivalSource) -> None:
        self._source = source
        self.plan: LayerFetchPlan | None = None

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        self.plan = plan
        return self._source.begin_fetch(plan)

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        return self._source.poll_layer(layer_id, generation)

    def finish_fetch(self, generation: int) -> None:
        self._source.finish_fetch(generation)

    def abandon_fetch(self, generation: int) -> None:
        self._source.abandon_fetch(generation)


class _Loader:
    """Loads layers through a recording sink; reloads whole objects for real.

    A whole reload reads the objects back from L2 into general L1 and keeps
    a copy of each object's bytes, then releases the read locks.
    """

    def __init__(self, manager: StorageManager) -> None:
        self._manager = manager
        self.sink = RecordingLayerLoadSink()
        self.reloaded: list[ObjectKey] = []
        self.reloaded_bytes: dict[ObjectKey, bytes] = {}

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        return self.sink

    def wait_for_copies(self) -> None:
        """The recording sink copies nothing."""

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        self.reloaded = [o.key for o in objects]
        loaded = self._manager.load_into_l1(
            self.reloaded, dict(GROUP_LAYOUTS), FETCH_TIMEOUT
        )
        sizes = {o.key: o.object_bytes for o in objects}
        self.reloaded_bytes = {
            key: bytes(obj.byte_array[: sizes[key]]) for key, obj in loaded.items()
        }
        self._manager.finish_read_prefetched(list(loaded))


@dataclass
class _Retrieved:
    completion: RetrieveCompletion
    plan: LayerFetchPlan
    loader: _Loader
    l1_bytes: dict[ObjectKey, bytes] = field(default_factory=dict)


def _request_keys() -> list[list[ObjectKey]]:
    return [list(group) for group in resolve_obj_keys(vllm_request())]


def _objects() -> tuple[ObjectToPlace, ...]:
    return objects_to_place(fetch_model(), _request_keys())


def _payload(index: int, num_bytes: int) -> bytes:
    """Bytes in which every 4-byte word differs, within and across objects."""
    words = torch.arange(num_bytes // 4, dtype=torch.int32) + index * (1 << 20)
    return words.numpy().tobytes()


def _wait_fd(fd: int, timeout: float) -> None:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(timeout * 1000), "timed out waiting for eventfd"
    try:
        consume_fd(fd)
    except BlockingIOError:
        pass


def _store(adapter: L2AdapterInterface, key: ObjectKey, payload: bytes) -> None:
    values = torch.frombuffer(bytearray(payload), dtype=torch.float32)
    obj = TensorMemoryObj(
        values,
        MemoryObjMetadata(
            shape=values.shape,
            dtype=values.dtype,
            address=0,
            phy_size=len(payload),
            fmt=MemoryFormat.KV_2LTD,
            ref_count=1,
        ),
        parent_allocator=None,
    )
    task = adapter.submit_store_task([key], [obj])
    _wait_fd(adapter.get_store_event_fd(), STORE_TIMEOUT)
    assert adapter.pop_completed_store_tasks()[task].is_successful()


def _adapter_config(set_name: str, rdma: L1RdmaConfig) -> AerospikeL2AdapterConfig:
    return AerospikeL2AdapterConfig(
        hosts=f"{AEROSPIKE_HOST}:{AEROSPIKE_PORT}",
        namespace=AEROSPIKE_NAMESPACE,
        set_name=set_name,
        num_workers=2,
        rdma=rdma,
    )


@pytest.fixture
def set_name() -> str:
    """A fresh set per test, so no test reads another's records."""
    return f"pipelined_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def stored(set_name: str) -> dict[ObjectKey, bytes]:
    """Every object the request fetches, stored with its own payload.

    Returns:
        ``{key: payload}`` for each stored object.
    """
    adapter = create_l2_adapter_from_registry(
        _adapter_config(set_name, L1RdmaConfig())
    )
    try:
        adapter.set_object_group_layouts(dict(GROUP_LAYOUTS), KERNEL_LAYERS)
        payloads = {
            o.key: _payload(index, o.object_bytes)
            for index, o in enumerate(_objects())
        }
        for key, payload in payloads.items():
            _store(adapter, key, payload)
    finally:
        adapter.close()
    return payloads


def _build_manager(set_name: str) -> StorageManager:
    """A storage manager whose one RDMA window is registered with the server.

    The ``retain`` prefetch policy keeps every fetched object in L1, so a
    test can read back what a fetch landed.
    """
    rdma = L1RdmaConfig(
        transport=RDMA_TRANSPORT,
        device_name=RDMA_DEVICE,
        gid_index=RDMA_GID_INDEX,
        window_plan=RdmaWindowPlan(window_count=1, window_bytes=WINDOW_BYTES),
        fetch_timeout_seconds=FETCH_TIMEOUT,
    )
    built = StorageManager(
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
            l2_adapter_config=L2AdaptersConfig([_adapter_config(set_name, rdma)]),
            prefetch_policy="retain",
        )
    )
    built.set_object_group_layouts(dict(GROUP_LAYOUTS), KERNEL_LAYERS)
    return built


@pytest.fixture
def manager(set_name: str) -> Iterator[StorageManager]:
    """A storage manager from ``_build_manager``, closed after the test."""
    built = _build_manager(set_name)
    try:
        yield built
    finally:
        built.close()


def _retrieve(manager: StorageManager) -> _Retrieved:
    """Run one pipelined retrieve and read back what it left in L1."""
    tap = _PlanTap(manager.layer_arrival_source())
    loader = _Loader(manager)
    placer = manager.pipelined_window_placer(
        GROUP_LAYOUTS, fetch_model(), len(_request_keys()[0])
    )
    result = run_pipelined_retrieve(
        fetch_model(),
        _request_keys(),
        manager.pipelined_max_record_bytes(),
        placer,
        tap,
        loader,
        layer_timeout_seconds=FETCH_TIMEOUT,
        poll_interval_seconds=0.001,
    )
    assert tap.plan is not None, "the retrieve never began a fetch"
    sizes = {o.key: o.object_bytes for o in _objects()}
    resident = manager.lock_resident_keys(list(sizes))
    l1_bytes = {
        key: bytes(obj.byte_array[: sizes[key]])
        for key, obj in resident.locked.items()
    }
    manager.finish_read_prefetched(list(resident.locked))
    return _Retrieved(result.completion, tap.plan, loader, l1_bytes)


def test_a_pipelined_fetch_lands_every_stored_byte_in_l1(
    stored: dict[ObjectKey, bytes], manager: StorageManager
) -> None:
    """The server writes each slot's record where the plan put it."""
    retrieved = _retrieve(manager)

    assert retrieved.completion is RetrieveCompletion.PIPELINED
    assert retrieved.plan.node_names == (manager.pipelined_fetch_node_name(),)
    sink = retrieved.loader.sink
    assert sink.loaded_layers() == retrieved.plan.layer_ids()
    assert sink.finished_generations() and not sink.abandoned_generations()
    assert retrieved.loader.reloaded == []
    assert set(retrieved.l1_bytes) == set(stored)
    for key, payload in stored.items():
        assert retrieved.l1_bytes[key] == payload, f"{key} holds other bytes"


def test_a_missing_record_falls_back_to_a_whole_reload(
    stored: dict[ObjectKey, bytes], manager: StorageManager, set_name: str
) -> None:
    """A slot the server cannot serve fails the fetch, not the retrieve.

    The server reports the slot whose record is gone as failed; the retrieve
    reloads every object whole, and all but the damaged one read back intact.
    """
    # Third Party
    import aerospike

    damaged = next(iter(stored))
    client = aerospike.client({"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}).connect()
    try:
        client.remove(
            (AEROSPIKE_NAMESPACE, set_name, f"{object_key_to_string(damaged)}|s|0")
        )
    finally:
        client.close()

    retrieved = _retrieve(manager)

    assert retrieved.completion is RetrieveCompletion.FELL_BACK
    assert retrieved.loader.reloaded == list(stored)
    reloaded = retrieved.loader.reloaded_bytes
    assert set(reloaded) == set(stored) - {damaged}
    for key, payload in reloaded.items():
        assert payload == stored[key], f"{key} reloaded other bytes"


def test_closing_releases_the_servers_region(set_name: str) -> None:
    """More client lifetimes than the server has region slots all register.

    The server holds at most ``SERVER_MAX_REGIONS`` regions and refuses the
    next registration, so this passes only if each close gives its region
    back.
    """
    for lifetime in range(SERVER_MAX_REGIONS + 1):
        built = _build_manager(set_name)
        try:
            assert built.pipelined_fetch_node_name(), (
                f"client lifetime {lifetime} could not register"
            )
        finally:
            built.close()
