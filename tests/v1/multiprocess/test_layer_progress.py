# SPDX-License-Identifier: Apache-2.0

# Standard

# Third Party
import pytest

# First Party
from lmcache.v1.multiprocess.layer_progress import (
    LayerLaunchEventPool,
    LayerProgressLayerNotScheduledError,
    LayerProgressRecord,
    LayerProgressRetrieveFailedError,
    LayerProgressRetrieveGenerationTimeoutError,
    LayerProgressRetrieveProgressTimeoutError,
    LayerProgressStaleGenerationError,
    LayerProgressWaiter,
    WorkerComputeLayerLaunchEventPool,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule


class _RecordingEventBackend:
    device_type = "fake"

    def check_event_support(self, device: object) -> None:
        return None

    def create_event(self, device: object) -> object:
        return object()

    def export_event(self, event: object, device: object) -> bytes:
        return b""

    def import_event(self, handle: bytes, device: object) -> object:
        return ("event", handle)

    def record_event(self, event: object, stream: object) -> None:
        return None

    def wait_event(self, event: object, stream: object) -> None:
        return None

    def query_event(self, event: object) -> bool:
        return True

    def synchronize_event(self, event: object, device: object) -> None:
        return None


class RecordingEventPool(LayerLaunchEventPool):
    """Records ordinals and validates tight wait counts."""

    def __init__(self, expected_count: int) -> None:
        self.waited: list[int] = []
        self.expected_count = expected_count

    def wait_on_ordinal(self, ordinal: int) -> None:
        if ordinal < 0 or ordinal >= self.expected_count:
            raise ValueError("ordinal out of range")
        self.waited.append(ordinal)


def test_record_begin_and_watermark() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    record.begin_retrieve(3)
    snap = record.read()
    assert snap.generation == 3
    assert snap.watermark == 0
    assert not snap.retrieve_failed

    record.report_launch_recorded(2)
    snap = record.read()
    assert snap.watermark == 2


def test_waiter_waits_for_watermark_then_event() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    events = RecordingEventPool(schedule.launch_count())
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            record.begin_retrieve(1)
            record.report_launch_recorded(2)

    waiter = LayerProgressWaiter(
        record,
        events,
        poll_interval_seconds=0.001,
        wait_timeout_seconds=1.0,
        sleep=fake_sleep,
    )

    waiter.wait_for_layer(1, 1, schedule)

    assert events.waited == [1]


def test_waiter_noop_when_generation_zero() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    events = RecordingEventPool(2)
    waiter = LayerProgressWaiter(record, events)
    schedule = LayerwiseSchedule([[0, 1]])
    waiter.wait_for_layer(0, 0, schedule)
    assert events.waited == []


def test_waiter_raises_when_layer_not_scheduled() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(
        record, RecordingEventPool(4), wait_timeout_seconds=1.0
    )
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    record.begin_retrieve(1)
    with pytest.raises(LayerProgressLayerNotScheduledError):
        waiter.wait_for_layer(1, 99, schedule)


def test_waiter_raises_on_superseded_generation() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(
        record,
        RecordingEventPool(2),
        poll_interval_seconds=0.001,
        wait_timeout_seconds=0.05,
    )
    schedule = LayerwiseSchedule([[0, 1]])
    record.begin_retrieve(2)
    with pytest.raises(LayerProgressStaleGenerationError):
        waiter.wait_for_layer(1, 0, schedule)


def test_waiter_raises_when_retrieve_failed() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(
        record,
        RecordingEventPool(2),
        poll_interval_seconds=0.001,
        wait_timeout_seconds=0.05,
    )
    schedule = LayerwiseSchedule([[0, 1]])
    record.begin_retrieve(1)
    record.mark_retrieve_failed()
    with pytest.raises(LayerProgressRetrieveFailedError):
        waiter.wait_for_layer(1, 0, schedule)


def test_waiter_times_out_waiting_for_generation() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(
        record,
        RecordingEventPool(2),
        poll_interval_seconds=0.001,
        wait_timeout_seconds=0.05,
    )
    schedule = LayerwiseSchedule([[0, 1]])
    with pytest.raises(LayerProgressRetrieveGenerationTimeoutError):
        waiter.wait_for_layer(1, 0, schedule)


def test_waiter_times_out_when_watermark_stalls() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(
        record,
        RecordingEventPool(2),
        poll_interval_seconds=0.001,
        wait_timeout_seconds=0.05,
    )
    schedule = LayerwiseSchedule([[0, 1]])
    record.begin_retrieve(1)
    with pytest.raises(LayerProgressRetrieveProgressTimeoutError, match="timed out"):
        waiter.wait_for_layer(1, 1, schedule)


def test_worker_event_pool_validates_size() -> None:
    backend = _RecordingEventBackend()
    with pytest.raises(ValueError, match="expected"):
        WorkerComputeLayerLaunchEventPool([object()], backend, 2)
