# SPDX-License-Identifier: Apache-2.0

# Standard
import threading

# Third Party
import pytest

# First Party
from lmcache.v1.multiprocess.layer_progress import (
    LayerProgressLayerNotScheduledError,
    LayerProgressRecord,
    LayerProgressRetrieveFailedError,
    LayerProgressStaleGenerationError,
    LayerProgressWaiter,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule


class FakeEventPool:
    """Records which ordinals the worker waited on."""

    def __init__(self) -> None:
        self.waited: list[tuple[int, int]] = []

    def wait_on_ordinal(self, ordinal: int, wait_ordinal: int) -> None:
        self.waited.append((ordinal, wait_ordinal))


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
    events = FakeEventPool()
    waiter = LayerProgressWaiter(
        record,
        events,
        poll_interval_seconds=0.001,
        wait_timeout_seconds=1.0,
    )
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])

    record.begin_retrieve(1)

    def daemon() -> None:
        record.report_launch_recorded(1)
        record.report_launch_recorded(2)

    thread = threading.Thread(target=daemon)
    thread.start()
    waiter.wait_for_layer(1, 1, schedule)
    thread.join()

    assert events.waited == [(1, 2)]


def test_waiter_raises_when_layer_not_scheduled() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(record, FakeEventPool())
    schedule = LayerwiseSchedule([[0, 2], [1, 3]])
    record.begin_retrieve(1)
    with pytest.raises(LayerProgressLayerNotScheduledError):
        waiter.wait_for_layer(1, 99, schedule)


def test_waiter_raises_on_stale_generation() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(
        record,
        FakeEventPool(),
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
        FakeEventPool(),
        poll_interval_seconds=0.001,
        wait_timeout_seconds=0.05,
    )
    schedule = LayerwiseSchedule([[0, 1]])
    record.begin_retrieve(1)
    record.mark_retrieve_failed()
    with pytest.raises(LayerProgressRetrieveFailedError):
        waiter.wait_for_layer(1, 0, schedule)


def test_waiter_times_out_when_watermark_stalls() -> None:
    buf = bytearray(LayerProgressRecord.RECORD_SIZE)
    record = LayerProgressRecord(buf)
    waiter = LayerProgressWaiter(
        record,
        FakeEventPool(),
        poll_interval_seconds=0.001,
        wait_timeout_seconds=0.05,
    )
    schedule = LayerwiseSchedule([[0, 1]])
    record.begin_retrieve(1)
    with pytest.raises(LayerProgressRetrieveFailedError, match="timed out"):
        waiter.wait_for_layer(1, 1, schedule)
