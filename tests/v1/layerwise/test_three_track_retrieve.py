# SPDX-License-Identifier: Apache-2.0
"""A deferred retrieve through all three tracks' production pieces, on CPU.

Track A: a real ``StorageManager`` whose L1 reserves the RDMA window, with
the fabric-free native client behind a real ``NativeConnectorL2Adapter``; the
placer comes from ``pipelined_window_placer`` and the source from
``layer_arrival_source``. Track C: ``fetch_deferred_objects`` over an
``ObjectTable``, as retrieve calls it. Track B: the sink
``MultiprocessPipelinedSinkFactory`` builds, staging each layer from the
table into real CPU staging buffers.

Only the attention kernel is stubbed. The fabric-free client moves no bytes,
so the test writes each window object's bytes itself when the fetch begins,
standing in for the transport, and checks that those bytes are what the sink
staged.
"""

# Standard
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock
import threading

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed import storage_manager as storage_manager_module
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
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
)
from lmcache.v1.layerwise.deferral import PipelinedFetchConfig, PipelinedModel
from lmcache.v1.layerwise.request_fetch import first_in_window_chunk
from lmcache.v1.memory_management import MemoryObj
from lmcache.v1.mp_observability.errors import LMCacheTimeoutError
from lmcache.v1.multiprocess import object_group_transfer
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerLaunchEventPool,
    LayerProgressError,
    LayerProgressRecord,
    LayerProgressRetrieveFailedError,
    LayerProgressRetrieveProgressTimeoutError,
    LayerProgressWaiter,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.pipelined_loading import (
    DeferredFetchResult,
    ObjectTable,
    PipelinedLoadRequest,
    PipelinedOutcome,
    fetch_deferred_objects,
)
from lmcache.v1.multiprocess.pipelined_sink import MultiprocessPipelinedSinkFactory
from lmcache.v1.multiprocess.transfer_context.worker_transfer import (
    LAYERWISE_WAIT_MARGIN_SECONDS,
)

# Local
from .aerospike_harness import WINDOW_BYTES, FabricFreeClient, fabric_free_connector
from .vllm_requests import (
    ATTN,
    CHUNK_TOKENS,
    GROUP_LAYOUTS,
    KERNEL_LAYERS,
    MAX_RECORD_BYTES,
    NUM_CHUNKS,
    fetch_model,
    resolve_obj_keys,
    vllm_request,
)

FETCH_TIMEOUT = 30.0
JOIN_TIMEOUT = 10.0
RETRIEVE_GENERATION = 7
#: The aux group: never retrieved, so its table row stays empty.
AUX_GROUP = 2
MAX_BATCH = 4
#: Every ``GROUP_LAYOUTS`` shape is ``(planes, layers, tokens, hidden)``.
PLANES = 2
LAYERS_PER_GROUP = 2
#: Short timeouts so a stalled layer resolves quickly. The fabric-free
#: client completes no batch read, so a whole load always takes the full
#: whole-load timeout and then fails.
CONFIG = PipelinedFetchConfig(
    enabled=True, layer_timeout_seconds=0.5, whole_load_timeout_seconds=0.5
)


class _FakeEventBackend:
    """Records nothing; the kernel that would wait on the events is stubbed."""

    device_type = "fake"

    def check_event_support(self, device: object) -> None:
        return None

    def create_event(self, device: object) -> object:
        return object()

    def export_event(self, event: object, device: object) -> bytes:
        return b""

    def import_event(self, handle: bytes, device: object) -> object:
        return handle

    def record_event(self, event: object, stream: object) -> None:
        return None

    def wait_event(self, event: object, stream: object) -> None:
        return None

    def query_event(self, event: object) -> bool:
        return True

    def synchronize_event(self, event: object, device: object) -> None:
        return None


class _HostObject:
    """An L1 object another request already left resident, filled with a mark."""

    def __init__(self, nbytes: int, mark: float) -> None:
        self.raw_tensor = torch.zeros(nbytes, dtype=torch.uint8)
        self.raw_tensor.view(torch.float16).fill_(mark)

    def get_size(self) -> int:
        return self.raw_tensor.nbytes

    def parent(self) -> None:
        """Not from a lazy allocator, so its whole tensor is backed."""
        return None


def _mark(object_group_id: int, chunk_id: int) -> float:
    """A value unique to one object, exact in float16."""
    return float(1 + 10 * object_group_id + chunk_id)


def _group_bytes(object_group_id: int) -> int:
    layout = GROUP_LAYOUTS[object_group_id]
    return int(layout.shapes[0].numel()) * layout.dtypes[0].itemsize


class _Staging:
    """Real CPU staging buffers, one per batch slot and object group.

    Each object group holds one kernel group, so a kernel group's staging
    view is its object group's whole buffer, shaped ``(planes, layers,
    tokens, hidden)`` as ``GROUP_LAYOUTS`` gives it.
    """

    def __init__(self) -> None:
        self._buffers = {
            (slot, g): torch.zeros(_group_bytes(g), dtype=torch.uint8)
            for slot in range(MAX_BATCH)
            for g in GROUP_LAYOUTS
        }

    def object_group_buffer(self, slot: int, object_group_id: int) -> torch.Tensor:
        return self._buffers[(slot, object_group_id)]

    def kernel_group_buffer(self, slot: int, kernel_group_id: int) -> torch.Tensor:
        layout = GROUP_LAYOUTS[kernel_group_id]
        flat = self._buffers[(slot, kernel_group_id)]
        return flat.view(layout.dtypes[0]).view(layout.shapes[0])

    def cache_context(self) -> MagicMock:
        """A cache context over these buffers, laid out as ``fetch_model()``."""
        context = MagicMock()
        context.lmcache_tokens_per_chunk = CHUNK_TOKENS
        context.max_batch_size = MAX_BATCH
        context.device = torch.device("cpu")
        context.stream = MagicMock(name="transfer_stream")
        context.calculate_num_blocks = lambda tokens, _kg: max(1, tokens // 16)
        context.get_temp_object_group_buffer = self.object_group_buffer
        context.get_temp_kernel_group_buffer = self.kernel_group_buffer
        context.get_kernel_group_kv_pointers = MagicMock(return_value=[])
        context.get_shape_desc = MagicMock()
        context.get_slots_per_chunk_in_sw = MagicMock(return_value=16)
        context.get_engine_kv_format = MagicMock(return_value=0)
        manager = MagicMock()
        manager.object_groups = [
            SimpleNamespace(kernel_group_indices=[g]) for g in sorted(KERNEL_LAYERS)
        ]
        manager.get_attn_desc = MagicMock(return_value=ATTN)
        manager.get_subchunk_sw_size_tokens = MagicMock(return_value=CHUNK_TOKENS)
        context.kv_layer_groups_manager = manager
        return context


@dataclass
class _Copies:
    """The mark at the start of every byte range the sink staged."""

    marks: list[float] = field(default_factory=list)


@pytest.fixture
def copies(monkeypatch: pytest.MonkeyPatch) -> _Copies:
    """Record every staging copy (which still runs) and stub the kernel."""
    recorded = _Copies()
    real_copy = object_group_transfer.lmcache_memcpy_async_h2d_range

    def copy(memory_obj: MemoryObj, buffer: torch.Tensor, offset: int, n: int) -> None:
        real_copy(memory_obj, buffer, offset, n)
        staged = buffer[offset : offset + 2].view(torch.float16)
        recorded.marks.append(float(staged.item()))

    def kernel(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(object_group_transfer, "lmcache_memcpy_async_h2d_range", copy)
    monkeypatch.setattr(
        object_group_transfer.device_ops, "multi_layer_block_kv_transfer", kernel
    )
    return recorded


class _Tap:
    """Passes the source through; lets the test act when the fetch begins."""

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

    def read_write_ids(self, cache_keys: Sequence[str]) -> Mapping[str, str]:
        return self._source.read_write_ids(cache_keys)

    def wait(self) -> LayerFetchPlan:
        if not self._begun.wait(JOIN_TIMEOUT) or self.plan is None:
            raise AssertionError("the pump never began a fetch")
        return self.plan


class _TappedStorage:
    """The storage manager, with its layer-arrival source tapped."""

    def __init__(self, manager: StorageManager) -> None:
        self._manager = manager
        self.tap: _Tap | None = None

    def layer_arrival_source(self) -> LayerArrivalSource:
        self.tap = _Tap(self._manager.layer_arrival_source())
        return self.tap

    def load_into_l1(
        self,
        keys: list[ObjectKey],
        group_layout_descs: dict[int, MemoryLayoutDesc],
        timeout_seconds: float,
    ) -> dict[ObjectKey, MemoryObj]:
        return self._manager.load_into_l1(keys, group_layout_descs, timeout_seconds)

    def finish_read_prefetched(
        self, keys: list[ObjectKey], read_locks: int = 1
    ) -> None:
        self._manager.finish_read_prefetched(keys, read_locks)


def _schedule() -> LayerwiseSchedule:
    """The worker's launch order: every kernel group's layers, aux included."""
    return LayerwiseSchedule([KERNEL_LAYERS[g][0] for g in sorted(KERNEL_LAYERS)])


def _request_keys() -> list[list[ObjectKey]]:
    return [list(group) for group in resolve_obj_keys(vllm_request())]


def _in_window() -> dict[int, range]:
    """Each retrieved group's in-window chunks, as retrieve reads them."""
    return {
        g: range(first_in_window_chunk(NUM_CHUNKS, window), NUM_CHUNKS)
        for g, window in enumerate(ATTN.num_chunks_in_sw)
        if g != AUX_GROUP
    }


@dataclass
class _Retrieved:
    result: DeferredFetchResult | None
    error: BaseException | None
    table: ObjectTable
    progress: LayerProgressRecord
    window_positions: list[tuple[int, int]]
    #: Per scheduled layer the worker reached: the error its wait raised, or
    #: ``None`` if the layer was ready. Empty without a worker.
    worker_waits: list[tuple[int, LayerProgressError | None]]


class _Worker:
    """vLLM's side: waits for each scheduled layer in order, as attention does.

    Runs the real :class:`LayerProgressWaiter`. Stops at the first wait that
    raises, as the connector does: a failed retrieve's remaining waits are
    skipped, and a timeout stops the engine.
    """

    def __init__(
        self, progress: LayerProgressRecord, wait_timeout_seconds: float
    ) -> None:
        self._waiter = LayerProgressWaiter(
            progress,
            LayerLaunchEventPool(),
            wait_timeout_seconds=wait_timeout_seconds,
        )
        self.waits: list[tuple[int, LayerProgressError | None]] = []
        self._thread = threading.Thread(target=self._run)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(JOIN_TIMEOUT)
        assert not self._thread.is_alive(), "the worker never finished waiting"

    def _run(self) -> None:
        schedule = _schedule()
        for launch in schedule.launches:
            try:
                self._waiter.wait_for_layer(
                    RETRIEVE_GENERATION, launch.layer_id, schedule
                )
            except LayerProgressError as exc:
                self.waits.append((launch.layer_id, exc))
                return
            self.waits.append((launch.layer_id, None))


@dataclass
class _Daemon:
    manager: StorageManager
    connector: FabricFreeClient

    def model(self) -> PipelinedModel:
        return PipelinedModel(
            fetch_model=fetch_model(),
            placer=self.manager.pipelined_window_placer(
                GROUP_LAYOUTS, fetch_model(), NUM_CHUNKS
            ),
            max_record_bytes=MAX_RECORD_BYTES,
            max_slots=self.manager.pipelined_max_slots_per_request(),
            adapter_id=self.manager.pipelined_adapter_id(),
            max_chunks=NUM_CHUNKS,
        )

    def resident(self, keys: Sequence[ObjectKey]) -> set[ObjectKey]:
        """The keys now readable in L1."""
        locked = self.manager.lock_resident_keys(list(keys)).locked
        if locked:
            self.manager.finish_read_prefetched(list(locked))
        return set(locked)

    def retrieve(
        self,
        in_l1: frozenset[tuple[int, int]] = frozenset(),
        decline_slot: int | None = None,
        stall_layer: int | None = None,
        config: PipelinedFetchConfig = CONFIG,
        worker_wait_seconds: float = 0.0,
    ) -> _Retrieved:
        """Run one deferred retrieve as ``LMCacheDrivenTransferModule`` does.

        Args:
            in_l1: ``(object_group_id, chunk_id)`` positions another request
                already left in L1; the rest of the window is deferred.
            decline_slot: A plan slot the transport declines; every other
                slot lands.
            stall_layer: A layer none of whose slots ever land, nor are
                declined, as when a server stops writing mid-fetch.
            config: The daemon's pipelined settings.
            worker_wait_seconds: If positive, a worker waits on every
                scheduled layer from before the retrieve starts, for this long
                per layer, and its waits are reported.
        """
        keys = _request_keys()
        rows: list[list[MemoryObj | None]] = [[] for _ in keys]
        to_fetch: list[ObjectKey] = []
        for g, chunks in _in_window().items():
            row: list[MemoryObj | None] = [None] * NUM_CHUNKS
            for c in chunks:
                if (g, c) in in_l1:
                    row[c] = cast(MemoryObj, _HostObject(_group_bytes(g), _mark(g, c)))
                else:
                    to_fetch.append(keys[g][c])
            rows[g] = row
        table = ObjectTable(rows)
        progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
        schedule = _schedule()
        launches = schedule.launch_count()
        staging = _Staging()
        request = PipelinedLoadRequest(
            cache_context=staging.cache_context(),
            block_ids_gpu=[torch.arange(NUM_CHUNKS) for _ in KERNEL_LAYERS],
            objects=table,
            skip_first_n_tokens=0,
            schedule=schedule,
            progress=progress,
            event_pool=DaemonLayerLaunchEventPool(
                [object()] * launches, _FakeEventBackend(), launches
            ),
            retrieve_generation=RETRIEVE_GENERATION,
            transfer_key="retrieve-key",
        )
        storage = _TappedStorage(self.manager)
        outcome: list[DeferredFetchResult] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                outcome.append(
                    fetch_deferred_objects(
                        storage,
                        self.model(),
                        keys,
                        to_fetch,
                        MultiprocessPipelinedSinkFactory(),
                        request,
                        GROUP_LAYOUTS,
                        config,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = (
            _Worker(progress, worker_wait_seconds) if worker_wait_seconds > 0 else None
        )
        if worker is not None:
            worker.start()
        thread = threading.Thread(target=run)
        thread.start()
        while storage.tap is None:
            thread.join(0.001)
        plan = storage.tap.wait()
        window_positions = [
            (g, c)
            for g, chunks in _in_window().items()
            for c in chunks
            if (g, c) not in in_l1
        ]
        for g, c in window_positions:
            window_obj = table.get(g, c)
            assert window_obj is not None and window_obj.raw_tensor is not None
            window_obj.raw_tensor.view(torch.float16).fill_(_mark(g, c))
        generation = storage.tap.generation
        for index, slot in enumerate(plan.slots):
            if index == decline_slot:
                self.connector.decline_slot(index, generation)
            elif slot.layer_id != stall_layer:
                self.connector.land_slot(index, generation)
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive(), "retrieve did not finish"
        if worker is not None:
            worker.join()
        return _Retrieved(
            outcome[0] if outcome else None,
            errors[0] if errors else None,
            table,
            progress,
            window_positions,
            worker.waits if worker is not None else [],
        )


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Daemon]:
    """A storage manager with one RDMA window over the fabric-free client.

    ``retain`` keeps fetched objects in L1, so a test can see what a clean
    fetch left behind.
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
    yield _Daemon(manager, connector)
    manager.close()


def _expected_marks(positions: Sequence[tuple[int, int]]) -> list[float]:
    """One staged range per plane per layer of each object, sorted."""
    per_object = PLANES * LAYERS_PER_GROUP
    return sorted(_mark(g, c) for g, c in positions for _ in range(per_object))


def test_every_deferred_layer_reaches_staging_from_the_window(
    daemon: _Daemon, copies: _Copies
) -> None:
    retrieved = daemon.retrieve()

    assert retrieved.error is None and retrieved.result is not None
    assert retrieved.result.outcome is PipelinedOutcome.PIPELINED
    assert retrieved.result.locked_keys == ()
    snapshot = retrieved.progress.read()
    assert snapshot.generation == RETRIEVE_GENERATION
    assert snapshot.watermark == _schedule().launch_count()
    assert not snapshot.retrieve_failed
    assert sorted(copies.marks) == _expected_marks(retrieved.window_positions)
    keys = _request_keys()
    fetched = [keys[g][c] for g, c in retrieved.window_positions]
    assert daemon.resident(fetched) == set(fetched)


def test_l1_and_window_objects_load_in_one_pass(
    daemon: _Daemon, copies: _Copies
) -> None:
    in_l1 = frozenset({(0, 0), (0, 1), (1, 3)})

    retrieved = daemon.retrieve(in_l1=in_l1)

    assert retrieved.error is None and retrieved.result is not None
    assert retrieved.result.outcome is PipelinedOutcome.PIPELINED
    assert sorted(copies.marks) == _expected_marks(
        [*in_l1, *retrieved.window_positions]
    )
    assert not retrieved.progress.read().retrieve_failed


def test_a_declined_slot_with_no_whole_copy_fails_the_retrieve_for_recompute(
    daemon: _Daemon, copies: _Copies
) -> None:
    """The fabric-free client completes no batch read, so the whole load times out.

    The sink has already published the retrieve, so the worker must see it
    failed and recompute rather than read the blocks.
    """
    retrieved = daemon.retrieve(decline_slot=0)

    assert retrieved.result is None
    assert isinstance(retrieved.error, LMCacheTimeoutError)
    snapshot = retrieved.progress.read()
    assert snapshot.generation == RETRIEVE_GENERATION
    assert snapshot.retrieve_failed
    keys = _request_keys()
    assert daemon.resident([keys[g][c] for g, c in retrieved.window_positions]) == set()


def _fetched_layers() -> list[int]:
    """The layers the plan fetches, ascending: every retrieved group's."""
    layout = fetch_model().layout
    return [
        layer_id
        for layer_id in layout.layer_ids()
        if layout.object_group_of_layer(layer_id) != AUX_GROUP
    ]


@pytest.mark.parametrize("position", [0, -1], ids=["first_layer", "last_layer"])
def test_a_stalled_layer_fails_the_retrieve_before_the_worker_gives_up(
    daemon: _Daemon, copies: _Copies, position: int
) -> None:
    """The worst case for one layer: the pump's timeout, then a whole load that
    also times out. A worker that waits the server's budget plus the margin
    sees the failure, which vLLM recovers from, not a timeout, which stops it.
    """
    retrieved = daemon.retrieve(
        stall_layer=_fetched_layers()[position],
        worker_wait_seconds=(
            CONFIG.layer_publish_budget_seconds + LAYERWISE_WAIT_MARGIN_SECONDS
        ),
    )

    assert isinstance(retrieved.error, LMCacheTimeoutError)
    assert retrieved.progress.read().retrieve_failed
    *ready, (_, last_wait) = retrieved.worker_waits
    assert all(error is None for _, error in ready)
    assert isinstance(last_wait, LayerProgressRetrieveFailedError)


def test_a_worker_that_waits_less_than_the_budget_times_out_first(
    daemon: _Daemon, copies: _Copies
) -> None:
    """Why registration refuses such a worker: it gives up while the server is
    still inside its budget, which in vLLM stops the engine."""
    retrieved = daemon.retrieve(
        stall_layer=_fetched_layers()[0],
        worker_wait_seconds=(
            CONFIG.layer_timeout_seconds + CONFIG.whole_load_timeout_seconds / 2
        ),
    )

    assert isinstance(retrieved.error, LMCacheTimeoutError)
    (_, last_wait) = retrieved.worker_waits[-1]
    assert isinstance(last_wait, LayerProgressRetrieveProgressTimeoutError)
