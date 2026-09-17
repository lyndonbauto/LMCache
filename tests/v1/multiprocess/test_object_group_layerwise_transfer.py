# SPDX-License-Identifier: Apache-2.0
"""Tests for layerwise H2D retrieve orchestration."""

# Standard
from types import SimpleNamespace
from unittest.mock import MagicMock

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.multiprocess import object_group_transfer
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule


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
