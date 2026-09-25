# SPDX-License-Identifier: Apache-2.0
"""Tests for layerwise H2D retrieve orchestration."""

# Standard
from types import SimpleNamespace
from unittest.mock import MagicMock
import threading
import time

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.gpu_connector import gpu_ops
from lmcache.v1.layerwise import (
    LayerArrivalPump,
    LayerFetchPlan,
    LayerUnservableError,
    LayerwiseContractError,
    ScriptedLayerArrivalSource,
    SlotPlacement,
    UnservableLayerArrivalSource,
)
from lmcache.v1.memory_management import GDSMemoryObject
from lmcache.v1.multiprocess import object_group_transfer
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerLaunchEventPool,
    LayerProgressRecord,
    LayerProgressRetrieveFailedError,
    LayerProgressWaiter,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.layerwise_sink import MultiprocessLayerLoadSink


def _make_cache_context() -> MagicMock:
    """Build a one-object-group, two-kernel-group cache context stub."""
    cache_context = MagicMock()
    cache_context.lmcache_tokens_per_chunk = 16
    cache_context.max_batch_size = 4
    cache_context.device = torch.device("cpu")
    cache_context.stream = object()
    cache_context.calculate_num_blocks = lambda tokens, _gid: max(1, tokens // 16)
    cache_context.get_temp_object_group_buffer = MagicMock()
    cache_context.get_temp_kernel_group_buffer = MagicMock(
        return_value=SimpleNamespace(data_ptr=lambda: 0)
    )
    cache_context.get_kernel_group_kv_pointers = MagicMock(return_value=[])
    cache_context.get_shape_desc = MagicMock()
    cache_context.get_slots_per_chunk_in_sw = MagicMock(return_value=16)
    cache_context.get_engine_kv_format = MagicMock(return_value=0)

    kg_manager = MagicMock()
    kg_manager.object_groups = [SimpleNamespace(kernel_group_indices=[0, 1])]
    attn = MagicMock()
    attn.is_full_attention = MagicMock(return_value=True)
    attn.num_chunks_in_sw = [-1]
    kg_manager.get_attn_desc = MagicMock(return_value=attn)
    kg_manager.get_subchunk_sw_size_tokens = MagicMock(return_value=16)
    cache_context.kv_layer_groups_manager = kg_manager
    return cache_context


def _make_retrieve(
    schedule: LayerwiseSchedule,
    progress: LayerProgressRecord,
    retrieve_generation: int = 1,
) -> object_group_transfer.LayerwiseH2DRetrieve:
    """Build a retrieve over one stub memory object for ordering/failure tests.

    Uses whole-object staging because these tests stub the copies out; the
    per-layer staging path is covered with real tensors further down.
    """
    return object_group_transfer.LayerwiseH2DRetrieve(
        _make_cache_context(),
        [torch.tensor([0, 1]), torch.tensor([0, 1])],
        [[MagicMock()]],
        0,
        schedule,
        progress,
        _RecordingEventPool(schedule.launch_count()),
        retrieve_generation,
        staging=object_group_transfer.LayerStaging.WHOLE_OBJECT,
    )


#: Staging geometry for the real-tensor tests: two kernel groups of two layers
#: each (global layers [0, 2] and [1, 3]), four slots, hidden size eight.
_KV_SIZE = 2
_LAYERS_PER_GROUP = 2
_SLOTS = 4
_HIDDEN = 8
_SENTINEL = -1.0


def _layer_view(kernel_group_view: torch.Tensor, position: int) -> torch.Tensor:
    """Select one layer from a 4-D ``(kv, L, S, H)`` or 3-D ``(L, S, H)`` view."""
    if kernel_group_view.dim() == 4:
        return kernel_group_view[:, position]
    return kernel_group_view[position]


class _RealStaging:
    """Real CPU tensors standing in for one chunk's host object and staging.

    The staging buffer and the host object are flat byte buffers holding two
    kernel-group regions back to back, exactly as the CUDA cache context lays
    out an object group. ``kv_size == 0`` selects the 3-D layer-major layout.
    """

    def __init__(self, kv_size: int, device: str = "cpu") -> None:
        """Allocate zeroed host bytes ("nothing arrived") and sentinel staging.

        Args:
            kv_size: Planes per layer; 0 for the ``(L, S, H)`` layout.
            device: Where the staging buffer lives. On ``"cuda"`` the host
                object is pinned, so staging copies are real async H2D copies.
        """
        if kv_size:
            self.kernel_group_shape: tuple[int, ...] = (
                kv_size,
                _LAYERS_PER_GROUP,
                _SLOTS,
                _HIDDEN,
            )
        else:
            self.kernel_group_shape = (_LAYERS_PER_GROUP, _SLOTS, _HIDDEN)
        numel = 1
        for dim in self.kernel_group_shape:
            numel *= dim
        self.kernel_group_bytes = numel * 4
        self.host = torch.zeros(
            2 * self.kernel_group_bytes,
            dtype=torch.uint8,
            pin_memory=device != "cpu",
        )
        self.staging = torch.zeros(
            2 * self.kernel_group_bytes, dtype=torch.uint8, device=device
        )
        self.staging.view(torch.float32).fill_(_SENTINEL)
        self.memory_obj = MagicMock()
        self.memory_obj.raw_tensor = self.host
        self.memory_obj.get_size = lambda: self.host.nbytes

    def kernel_group(self, flat: torch.Tensor, kernel_group_id: int) -> torch.Tensor:
        """Return one kernel group's shaped float32 view into ``flat``."""
        start = kernel_group_id * self.kernel_group_bytes
        region = flat[start : start + self.kernel_group_bytes]
        return region.view(torch.float32).view(self.kernel_group_shape)

    def arrive(self, kernel_group_id: int, position: int, value: float) -> None:
        """Write one layer into the host object, as the transport would."""
        _layer_view(self.kernel_group(self.host, kernel_group_id), position).fill_(
            value
        )

    def staged(self, kernel_group_id: int, position: int) -> torch.Tensor:
        """Return one layer as it currently sits in GPU staging."""
        return _layer_view(self.kernel_group(self.staging, kernel_group_id), position)

    def cache_context(self) -> MagicMock:
        """Build a cache context whose staging getters return the real views."""
        cache_context = _make_cache_context()
        cache_context.get_temp_object_group_buffer = lambda _slot, _og: self.staging
        cache_context.get_temp_kernel_group_buffer = lambda _slot, kernel_group_id: (
            self.kernel_group(self.staging, kernel_group_id)
        )
        return cache_context

    def retrieve(
        self,
        schedule: LayerwiseSchedule,
        staging: object_group_transfer.LayerStaging,
    ) -> object_group_transfer.LayerwiseH2DRetrieve:
        """Build a retrieve over this chunk with the given staging mode."""
        return object_group_transfer.LayerwiseH2DRetrieve(
            self.cache_context(),
            [torch.tensor([0, 1]), torch.tensor([0, 1])],
            [[self.memory_obj]],
            0,
            schedule,
            LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE)),
            _RecordingEventPool(schedule.launch_count()),
            1,
            staging=staging,
        )


@pytest.fixture
def kernel_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stub the device copies and return the layer position of each kernel."""
    calls: list[int] = []

    def fake_transfer(*args: object, **kwargs: object) -> None:
        # Positional argument 9 is the layer's position within its group.
        position = args[9]
        assert isinstance(position, int)
        calls.append(position)

    monkeypatch.setattr(
        object_group_transfer.device_ops,
        "multi_layer_block_kv_transfer",
        fake_transfer,
    )
    monkeypatch.setattr(
        object_group_transfer, "lmcache_memcpy_async_h2d", lambda *a, **k: None
    )
    return calls


class _FakeEventBackend:
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


class _RecordingEventPool(DaemonLayerLaunchEventPool):
    def __init__(self, expected: int) -> None:
        super().__init__([object()] * expected, _FakeEventBackend(), expected)
        self.recorded: list[tuple[int, object]] = []
        self.publication_order: list[tuple[str, int]] = []

    def record_ordinal(self, ordinal: int, stream: object) -> None:
        self.publication_order.append(("record", ordinal))
        self.recorded.append((ordinal, stream))
        super().record_ordinal(ordinal, stream)


def test_transfer_kv_layerwise_records_before_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    progress = LayerProgressRecord(buf)
    pool = _RecordingEventPool(schedule.launch_count())
    order: list[str] = []

    def fake_transfer(*args: object, **kwargs: object) -> None:
        order.append("transfer")

    def fake_report(watermark: int) -> None:
        order.append(f"watermark:{watermark}")
        pool.publication_order.append(("watermark", watermark))
        LayerProgressRecord.report_launch_recorded(progress, watermark)

    monkeypatch.setattr(
        object_group_transfer.device_ops,
        "multi_layer_block_kv_transfer",
        fake_transfer,
    )
    monkeypatch.setattr(
        object_group_transfer, "lmcache_memcpy_async_h2d", lambda *a, **k: None
    )
    monkeypatch.setattr(progress, "report_launch_recorded", fake_report)

    cache_context = MagicMock()
    cache_context.lmcache_tokens_per_chunk = 16
    cache_context.max_batch_size = 4
    cache_context.device = torch.device("cpu")
    cache_context.stream = object()
    cache_context.calculate_num_blocks = lambda tokens, _gid: max(1, tokens // 16)
    cache_context.get_temp_object_group_buffer = MagicMock()
    cache_context.get_temp_kernel_group_buffer = MagicMock(
        return_value=SimpleNamespace(data_ptr=lambda: 0)
    )
    cache_context.get_kernel_group_kv_pointers = MagicMock(return_value=[])
    cache_context.get_shape_desc = MagicMock()
    cache_context.get_slots_per_chunk_in_sw = MagicMock(return_value=16)
    cache_context.get_engine_kv_format = MagicMock(return_value=0)

    kg_manager = MagicMock()
    kg_manager.num_kernel_groups = 2
    kg_manager.object_groups = [
        SimpleNamespace(kernel_group_indices=[0, 1]),
    ]
    attn = MagicMock()
    attn.is_full_attention = MagicMock(return_value=True)
    attn.num_chunks_in_sw = [-1]
    kg_manager.get_attn_desc = MagicMock(return_value=attn)
    kg_manager.get_subchunk_sw_size_tokens = MagicMock(return_value=16)
    cache_context.kv_layer_groups_manager = kg_manager

    memory_obj = MagicMock()
    memory_objs = [[memory_obj]]
    block_ids = [torch.tensor([0, 1]), torch.tensor([0, 1])]

    object_group_transfer.transfer_kv_layerwise_h2d(
        cache_context,
        block_ids,
        memory_objs,
        0,
        schedule,
        progress,
        pool,
        1,
        transfer_key="k",
    )

    assert pool.recorded
    for idx, entry in enumerate(order):
        if entry.startswith("watermark:"):
            assert idx > 0
            assert order[idx - 1] == "transfer" or idx == 1

    for ordinal in range(schedule.launch_count()):
        watermark = ordinal + 1
        record_step = next(
            step for step in pool.publication_order if step == ("record", ordinal)
        )
        watermark_step = ("watermark", watermark)
        record_index = pool.publication_order.index(record_step)
        watermark_index = pool.publication_order.index(watermark_step)
        assert record_index < watermark_index, (
            "event must be recorded before the watermark reaches that ordinal"
        )


def test_transfer_kv_layerwise_batch_setup_once_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-batch setup must not scale with layer count."""
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    progress = LayerProgressRecord(buf)
    pool = _RecordingEventPool(schedule.launch_count())

    monkeypatch.setattr(
        object_group_transfer.device_ops,
        "multi_layer_block_kv_transfer",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        object_group_transfer, "lmcache_memcpy_async_h2d", lambda *a, **k: None
    )

    num_blocks_calls = 0
    temp_og_buffer_calls = 0

    def counting_calculate_num_blocks(tokens: int, _gid: int) -> int:
        nonlocal num_blocks_calls
        num_blocks_calls += 1
        return max(1, tokens // 16)

    def counting_temp_og_buffer(slot: int, _og: int) -> object:
        nonlocal temp_og_buffer_calls
        temp_og_buffer_calls += 1
        return object()

    cache_context = MagicMock()
    cache_context.lmcache_tokens_per_chunk = 16
    cache_context.max_batch_size = 4
    cache_context.device = torch.device("cpu")
    cache_context.stream = object()
    cache_context.calculate_num_blocks = counting_calculate_num_blocks
    cache_context.get_temp_object_group_buffer = counting_temp_og_buffer
    cache_context.get_temp_kernel_group_buffer = MagicMock(
        return_value=SimpleNamespace(data_ptr=lambda: 0)
    )
    cache_context.get_kernel_group_kv_pointers = MagicMock(return_value=[])
    cache_context.get_shape_desc = MagicMock()
    cache_context.get_slots_per_chunk_in_sw = MagicMock(return_value=16)
    cache_context.get_engine_kv_format = MagicMock(return_value=0)

    kg_manager = MagicMock()
    kg_manager.object_groups = [
        SimpleNamespace(kernel_group_indices=[0, 1]),
    ]
    attn = MagicMock()
    attn.is_full_attention = MagicMock(return_value=True)
    attn.num_chunks_in_sw = [-1]
    kg_manager.get_attn_desc = MagicMock(return_value=attn)
    kg_manager.get_subchunk_sw_size_tokens = MagicMock(return_value=16)
    cache_context.kv_layer_groups_manager = kg_manager

    memory_obj = MagicMock()
    memory_objs = [[memory_obj]]
    block_ids = [torch.tensor([0, 1]), torch.tensor([0, 1])]

    object_group_transfer.transfer_kv_layerwise_h2d(
        cache_context,
        block_ids,
        memory_objs,
        0,
        schedule,
        progress,
        pool,
        1,
        transfer_key="k",
    )

    num_kernel_groups = 2
    num_batches = 1
    assert temp_og_buffer_calls == num_batches
    assert num_blocks_calls == 3 * num_kernel_groups * num_batches
    assert schedule.launch_count() == 4


def test_launching_one_layer_publishes_only_that_layer(
    kernel_calls: list[int],
) -> None:
    """One launch copies one layer and advances the watermark by exactly one."""
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    retrieve = _make_retrieve(schedule, progress)

    retrieve.begin()
    assert progress.read().watermark == 0

    retrieve.launch_layer(0)

    snapshot = progress.read()
    assert snapshot.generation == 1
    assert snapshot.watermark == 1
    assert not snapshot.retrieve_failed
    assert kernel_calls == [schedule.launch_for(0).position_in_group]


def test_layers_launch_one_at_a_time_in_schedule_order(
    kernel_calls: list[int],
) -> None:
    """Hybrid groups interleave: layers go 0, 1, 2, 3 across the two groups."""
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    retrieve = _make_retrieve(schedule, progress)
    retrieve.begin()

    for expected_watermark, launch in enumerate(schedule.launches, start=1):
        retrieve.launch_layer(launch.layer_id)
        assert progress.read().watermark == expected_watermark

    assert kernel_calls == [launch.position_in_group for launch in schedule.launches]


def test_a_layer_launched_out_of_order_is_rejected_without_advancing(
    kernel_calls: list[int],
) -> None:
    """Skipping ahead would make an earlier layer look ready, so it is refused."""
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    retrieve = _make_retrieve(schedule, progress)
    retrieve.begin()

    with pytest.raises(ValueError, match="expected layer 0"):
        retrieve.launch_layer(2)

    assert progress.read().watermark == 0
    assert not progress.read().retrieve_failed
    assert kernel_calls == []
    retrieve.launch_layer(0)
    assert progress.read().watermark == 1


def test_launching_before_begin_is_rejected(kernel_calls: list[int]) -> None:
    """Nothing may be copied before the generation is published."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    retrieve = _make_retrieve(schedule, progress)

    with pytest.raises(RuntimeError, match="not_begun"):
        retrieve.launch_layer(0)
    assert kernel_calls == []


def test_launching_past_the_last_layer_is_rejected(kernel_calls: list[int]) -> None:
    """A completed retrieve accepts no more launches."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    retrieve = _make_retrieve(schedule, progress)
    retrieve.begin()
    retrieve.launch_layer(0)
    retrieve.launch_layer(1)

    with pytest.raises(RuntimeError, match="complete"):
        retrieve.launch_layer(0)
    assert progress.read().watermark == 2


def test_a_failed_copy_marks_the_retrieve_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy error wakes waiters with a failure and stops further launches."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))

    def failing_transfer(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated kernel failure")

    monkeypatch.setattr(
        object_group_transfer.device_ops,
        "multi_layer_block_kv_transfer",
        failing_transfer,
    )
    monkeypatch.setattr(
        object_group_transfer, "lmcache_memcpy_async_h2d", lambda *a, **k: None
    )
    retrieve = _make_retrieve(schedule, progress)
    retrieve.begin()

    with pytest.raises(RuntimeError, match="simulated kernel failure"):
        retrieve.launch_layer(0)

    snapshot = progress.read()
    assert snapshot.retrieve_failed
    assert snapshot.watermark == 0
    with pytest.raises(RuntimeError, match="failed"):
        retrieve.launch_layer(0)


def test_failing_before_begin_publishes_this_generation() -> None:
    """A failure is attributed to this retrieve even if it never began."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    retrieve = _make_retrieve(schedule, progress, retrieve_generation=7)

    retrieve.mark_failed()

    snapshot = progress.read()
    assert snapshot.generation == 7
    assert snapshot.retrieve_failed


def test_the_real_sink_and_retrieve_let_a_worker_consume_every_layer(
    kernel_calls: list[int],
) -> None:
    """Pump -> sink -> real retrieve -> real waiter: every layer is consumable."""
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    sink = MultiprocessLayerLoadSink(schedule, _make_retrieve(schedule, progress))
    source = ScriptedLayerArrivalSource()
    plan = LayerFetchPlan(
        tuple(
            SlotPlacement(launch.layer_id, 0, 0, b"digest", 0, 64)
            for launch in schedule.launches
        )
    )
    errors: list[BaseException] = []

    def run_pump() -> None:
        try:
            LayerArrivalPump(source, sink).run(plan)
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors
            errors.append(exc)

    thread = threading.Thread(target=run_pump, name="layerwise-pump")
    thread.start()
    for launch in schedule.launches:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                source.deliver_layer(launch.layer_id)
                break
            except LayerwiseContractError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.001)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert errors == []
    waiter = LayerProgressWaiter(
        progress, LayerLaunchEventPool(), wait_timeout_seconds=1.0
    )
    for launch in schedule.launches:
        waiter.wait_for_layer(1, launch.layer_id, schedule)
    assert progress.read().watermark == schedule.launch_count()


def test_an_unservable_fetch_wakes_a_waiting_worker_with_a_failure(
    kernel_calls: list[int],
) -> None:
    """A declined fetch reaches the worker as a failure, not a timeout (B4)."""
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    sink = MultiprocessLayerLoadSink(schedule, _make_retrieve(schedule, progress))
    plan = LayerFetchPlan(
        tuple(
            SlotPlacement(launch.layer_id, 0, 0, b"digest", 0, 64)
            for launch in schedule.launches
        )
    )

    with pytest.raises(LayerUnservableError):
        LayerArrivalPump(UnservableLayerArrivalSource(), sink).run(plan)

    # Timeout is long; a correct abandon fails the wait immediately instead.
    waiter = LayerProgressWaiter(
        progress, LayerLaunchEventPool(), wait_timeout_seconds=30.0
    )
    started = time.monotonic()
    with pytest.raises(LayerProgressRetrieveFailedError):
        waiter.wait_for_layer(1, 0, schedule)
    assert time.monotonic() - started < 1.0
    assert kernel_calls == []


def test_a_late_failure_leaves_a_newer_retrieve_untouched() -> None:
    """Retrieve 3 failing after retrieve 5 began must not fail or rewind 5."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    old_retrieve = _make_retrieve(schedule, progress, retrieve_generation=3)
    progress.begin_retrieve(5)
    progress.report_launch_recorded(1)

    old_retrieve.mark_failed()

    snapshot = progress.read()
    assert snapshot.generation == 5
    assert snapshot.watermark == 1
    assert not snapshot.retrieve_failed


def test_failing_after_an_older_generation_publishes_this_one() -> None:
    """If the record still holds an older retrieve, the failure moves it on."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    progress.begin_retrieve(2)
    progress.report_launch_recorded(2)
    retrieve = _make_retrieve(schedule, progress, retrieve_generation=3)

    retrieve.mark_failed()

    snapshot = progress.read()
    assert snapshot.generation == 3
    assert snapshot.watermark == 0
    assert snapshot.retrieve_failed


def _check_per_layer_staging(kv_size: int, device: str) -> None:
    """Run the arrive-then-launch sequence and check staging after each launch.

    Args:
        kv_size: Planes per layer; 0 for the ``(L, S, H)`` layout.
        device: Where the staging buffer lives.
    """
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    real = _RealStaging(kv_size, device)
    retrieve = real.retrieve(schedule, object_group_transfer.LayerStaging.PER_LAYER)
    retrieve.begin()

    launched: list[tuple[int, int, float]] = []
    for value, launch in enumerate(schedule.launches, start=10):
        kernel_group_id = launch.kernel_group_index
        position = launch.position_in_group
        real.arrive(kernel_group_id, position, float(value))
        retrieve.launch_layer(launch.layer_id)
        launched.append((kernel_group_id, position, float(value)))

        for staged_group, staged_position, staged_value in launched:
            assert torch.all(real.staged(staged_group, staged_position) == staged_value)
        for other in schedule.launches:
            key = (other.kernel_group_index, other.position_in_group)
            if key not in {(group, pos) for group, pos, _ in launched}:
                assert torch.all(real.staged(*key) == _SENTINEL), (
                    f"layer {other.layer_id} was staged before it launched"
                )


@pytest.mark.parametrize("kv_size", [2, 0], ids=["kv-planes", "layer-major"])
def test_per_layer_staging_copies_each_layer_as_it_arrives(
    kernel_calls: list[int], kv_size: int
) -> None:
    """Each launch stages exactly its own layer, read at launch time (R1).

    Layers arrive in the host object one at a time, between launches. Every
    layer must reach staging with the value it had when *it* launched, and a
    launch must not touch any other layer's staging bytes.
    """
    _check_per_layer_staging(kv_size, "cpu")


@pytest.mark.cuda
@pytest.mark.skipif(
    not torch.cuda.is_available(), reason="requires an available CUDA runtime"
)
@pytest.mark.parametrize("kv_size", [2, 0], ids=["kv-planes", "layer-major"])
def test_per_layer_staging_on_gpu(kernel_calls: list[int], kv_size: int) -> None:
    """Same as the CPU test, with pinned host memory and a real GPU buffer.

    Exercises the actual asynchronous host-to-device range copies. Reads of
    the staging buffer run on the same stream as the copies, so they observe
    them in order.
    """
    _check_per_layer_staging(kv_size, "cuda")


def test_whole_object_staging_would_miss_a_late_arrival(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why per-layer staging exists: whole-object staging snapshots too early.

    With real whole-object copies, layer 2's bytes are copied when layer 0
    launches -- before layer 2 has arrived -- and never refreshed.
    """
    monkeypatch.setattr(
        object_group_transfer.device_ops,
        "multi_layer_block_kv_transfer",
        lambda *args, **kwargs: None,
    )
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    real = _RealStaging(_KV_SIZE)
    retrieve = real.retrieve(schedule, object_group_transfer.LayerStaging.WHOLE_OBJECT)
    retrieve.begin()

    real.arrive(0, 0, 10.0)
    retrieve.launch_layer(0)
    real.arrive(1, 0, 11.0)
    retrieve.launch_layer(1)
    real.arrive(0, 1, 12.0)  # layer 2 lands after the object was staged
    retrieve.launch_layer(2)

    assert torch.all(real.staged(0, 1) == 0.0), (
        "whole-object staging kept the pre-arrival bytes for layer 2"
    )


def test_per_layer_staging_rejects_gds_objects_before_publishing() -> None:
    """GDS objects only transfer whole, so per-layer setup refuses them."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))
    retrieve = object_group_transfer.LayerwiseH2DRetrieve(
        _make_cache_context(),
        [torch.tensor([0, 1]), torch.tensor([0, 1])],
        [[MagicMock(spec=GDSMemoryObject)]],
        0,
        schedule,
        progress,
        _RecordingEventPool(schedule.launch_count()),
        1,
    )

    with pytest.raises(ValueError, match="GDS"):
        retrieve.begin()
    assert progress.read().generation == 0


def test_range_copy_moves_only_the_requested_bytes() -> None:
    """The byte-range staging copy writes exactly its range."""
    host = torch.arange(16, dtype=torch.uint8)
    memory_obj = MagicMock()
    memory_obj.raw_tensor = host
    memory_obj.get_size = lambda: host.nbytes
    device = torch.zeros(16, dtype=torch.uint8)

    gpu_ops.lmcache_memcpy_async_h2d_range(memory_obj, device, 4, 3)

    assert device.tolist() == [0] * 4 + [4, 5, 6] + [0] * 9


@pytest.mark.parametrize(
    ("offset", "nbytes"),
    [(-1, 4), (0, 0), (14, 4)],
    ids=["negative", "empty", "overrun"],
)
def test_range_copy_rejects_bad_ranges(offset: int, nbytes: int) -> None:
    """Out-of-bounds or empty ranges raise instead of corrupting memory."""
    host = torch.zeros(16, dtype=torch.uint8)
    memory_obj = MagicMock()
    memory_obj.raw_tensor = host
    memory_obj.get_size = lambda: host.nbytes

    with pytest.raises(ValueError):
        gpu_ops.lmcache_memcpy_async_h2d_range(
            memory_obj, torch.zeros(16, dtype=torch.uint8), offset, nbytes
        )


def test_a_non_positive_generation_is_rejected() -> None:
    """Generation 0 means "no retrieve" to the worker, so it cannot be used."""
    schedule = LayerwiseSchedule([[0, 1]])
    progress = LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE))

    with pytest.raises(ValueError, match="must be positive"):
        _make_retrieve(schedule, progress, retrieve_generation=0)
