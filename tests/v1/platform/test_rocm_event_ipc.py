# SPDX-License-Identifier: Apache-2.0
"""Tests for the ROCm semaphore event backend.

``_FakeHip`` stands in for the HIP stream-memory calls. Each stream is a queue
that runs only when a test drains it, so the tests can check the semantics
directly: which record a queued wait targets, the order records complete in,
and when a slot may be reused. The tests at the end use the real HIP runtime
across processes and run only on ROCm GPUs.
"""

# Standard
from multiprocessing import get_context, shared_memory
from multiprocessing.connection import Connection
import ctypes
import gc
import threading
import time

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.platform.base.event_ipc import EventIPCBackend
from lmcache.v1.platform.ipc_policy import is_isolated_ipc, set_isolated_ipc
from lmcache.v1.platform.rocm import RocmDeviceSpec
from lmcache.v1.platform.rocm.event_ipc import (
    RocmEventIPCBackend,
    RocmEventOrigin,
    RocmSemaphoreEvent,
)

DEVICE = torch.device("cuda", 0)


class _Stream:
    """Stream-like object with a raw handle and a device, like torch's."""

    def __init__(self, handle: int) -> None:
        self.cuda_stream = handle
        self.device = DEVICE


class _FakeHip:
    """Queued stream memory operations over real host memory."""

    def __init__(self) -> None:
        self.queues: dict[int, list[tuple[str, int, int]]] = {}
        self._next_stream = 1000
        self.registered: list[tuple[int, int]] = []
        self.live: set[int] = set()

    def host_register(self, address: int, nbytes: int) -> None:
        if address in self.live:  # HIP: hipErrorHostMemoryAlreadyRegistered
            raise RuntimeError("hipHostRegister failed: already mapped (712)")
        self.live.add(address)
        self.registered.append((address, nbytes))

    def host_unregister(self, address: int) -> None:
        self.live.remove(address)

    def device_address(self, address: int, device_index: int) -> int:
        return address

    def write_value64(self, stream: int, address: int, value: int) -> None:
        self.queues.setdefault(stream, []).append(("write", address, value))

    def wait_value64_geq(self, stream: int, address: int, value: int) -> None:
        self.queues.setdefault(stream, []).append(("wait", address, value))

    def create_stream(self, device_index: int) -> int:
        self._next_stream += 1
        return self._next_stream

    def synchronize_stream(self, stream: int) -> None:
        if not self.drain(stream):
            raise AssertionError(f"stream {stream} would block forever")

    def drain(self, stream: int) -> bool:
        """Run ``stream`` until it empties or blocks; return True if empty."""
        queue = self.queues.setdefault(stream, [])
        while queue:
            kind, address, value = queue[0]
            cell = ctypes.c_uint64.from_address(address)
            if kind == "wait" and cell.value < value:
                return False
            if kind == "write":
                cell.value = value
            queue.pop(0)
        return True

    def waits(self, stream: int) -> list[int]:
        return [
            value for kind, _, value in self.queues.get(stream, []) if kind == "wait"
        ]


def _pair() -> tuple[RocmEventIPCBackend, RocmEventIPCBackend, _FakeHip]:
    """An exporter and an importer backend sharing one fake HIP."""
    hip = _FakeHip()
    return RocmEventIPCBackend(ops=hip), RocmEventIPCBackend(ops=hip), hip


def test_backend_satisfies_protocol_and_support_check() -> None:
    backend = RocmEventIPCBackend(ops=_FakeHip())
    assert isinstance(backend, EventIPCBackend)
    backend.check_event_support(DEVICE)  # must not raise


def test_unrecorded_event_is_complete_on_both_sides() -> None:
    exporter, importer, hip = _pair()
    event = exporter.create_event(DEVICE)
    imported = importer.import_event(exporter.export_event(event, DEVICE), DEVICE)
    assert exporter.query_event(event) is True
    assert importer.query_event(imported) is True
    importer.wait_event(imported, _Stream(7))
    assert hip.waits(7) == []


def test_imported_event_completes_when_the_record_lands() -> None:
    exporter, importer, hip = _pair()
    event = exporter.create_event(DEVICE)
    exporter.record_event(event, _Stream(1))
    imported = importer.import_event(exporter.export_event(event, DEVICE), DEVICE)
    assert importer.query_event(imported) is False
    hip.drain(1)
    assert importer.query_event(imported) is True
    importer.synchronize_event(imported, DEVICE)  # returns at once


def test_a_queued_wait_is_not_retargeted_by_a_later_record() -> None:
    """The bug on native HIP events: the next record must not move a wait."""
    exporter, importer, hip = _pair()
    event = exporter.create_event(DEVICE)
    imported = importer.import_event(exporter.export_event(event, DEVICE), DEVICE)
    exporter.record_event(event, _Stream(1))
    importer.wait_event(imported, _Stream(2))  # queued, not yet run
    hip.drain(1)
    exporter.record_event(event, _Stream(1))  # re-record before stream 2 runs
    assert hip.drain(2) is True  # satisfied by the first record alone
    importer.wait_event(imported, _Stream(3))
    assert hip.waits(3) == [2]  # a new wait targets the new record


def test_repeated_imports_share_one_region_attachment() -> None:
    exporter, importer, hip = _pair()
    handles = [
        exporter.export_event(exporter.create_event(DEVICE), DEVICE) for _ in range(5)
    ]
    for handle in handles * 3:
        importer.import_event(handle, DEVICE)
    # One registration for the exporter's region, one for the importer's
    # attachment of it; none per import.
    exporter.create_event(DEVICE)
    assert len(hip.registered) == 2


def test_dropped_backends_unregister_and_remove_their_regions() -> None:
    """A region is unregistered before it is unmapped, so the next mapping at
    the same address registers cleanly (HIP refuses a double registration)."""
    hip = _FakeHip()
    names = []
    for _ in range(50):
        exporter = RocmEventIPCBackend(ops=hip)
        importer = RocmEventIPCBackend(ops=hip)
        event = exporter.create_event(DEVICE)
        imported = importer.import_event(exporter.export_event(event, DEVICE), DEVICE)
        names.append(event.region.name)
        del exporter, importer, event, imported
        gc.collect()
    assert hip.live == set()
    for name in names:
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)


def test_records_from_different_streams_complete_in_order() -> None:
    exporter, _, hip = _pair()
    event = exporter.create_event(DEVICE)
    exporter.record_event(event, _Stream(1))
    exporter.record_event(event, _Stream(2))
    assert hip.drain(2) is False  # stream 2 waits for stream 1's record
    assert exporter.query_event(event) is False
    hip.drain(1)
    assert hip.drain(2) is True
    assert exporter.query_event(event) is True


def test_slot_with_an_unfinished_record_is_not_reused() -> None:
    hip = _FakeHip()
    exporter = RocmEventIPCBackend(ops=hip, slot_count=1)
    importer = RocmEventIPCBackend(ops=hip)
    busy = exporter.create_event(DEVICE)
    exporter.record_event(busy, _Stream(1))
    handle = exporter.export_event(busy, DEVICE)
    del busy
    gc.collect()

    with pytest.raises(RuntimeError, match="slots are in use"):
        exporter.create_event(DEVICE)
    imported = importer.import_event(handle, DEVICE)
    assert importer.query_event(imported) is False  # still waits for its record
    hip.drain(1)
    assert importer.query_event(imported) is True
    assert exporter.create_event(DEVICE).slot == 0  # reusable once it landed


def test_released_slot_is_reused_with_a_new_epoch() -> None:
    hip = _FakeHip()
    exporter = RocmEventIPCBackend(ops=hip, slot_count=1)
    importer = RocmEventIPCBackend(ops=hip)
    first = exporter.create_event(DEVICE)
    exporter.record_event(first, _Stream(1))
    old_handle = exporter.export_event(first, DEVICE)
    hip.drain(1)
    del first
    gc.collect()

    second = exporter.create_event(DEVICE)
    assert second.slot == 0 and second.epoch == 2
    exporter.record_event(second, _Stream(1))  # pending
    stale = importer.import_event(old_handle, DEVICE)
    assert importer.query_event(stale) is True  # its last record completed
    importer.wait_event(stale, _Stream(5))
    assert hip.waits(5) == []


def test_running_out_of_slots_raises() -> None:
    backend = RocmEventIPCBackend(ops=_FakeHip(), slot_count=2)
    held = [backend.create_event(DEVICE), backend.create_event(DEVICE)]
    with pytest.raises(RuntimeError, match="slots are in use"):
        backend.create_event(DEVICE)
    assert len(held) == 2


def test_imported_event_cannot_be_recorded() -> None:
    exporter, importer, _ = _pair()
    handle = exporter.export_event(exporter.create_event(DEVICE), DEVICE)
    imported = importer.import_event(handle, DEVICE)
    assert imported.origin is RocmEventOrigin.IMPORTED
    with pytest.raises(RuntimeError, match="only the exporting process records"):
        importer.record_event(imported, _Stream(1))


def test_foreign_events_and_handles_are_rejected() -> None:
    exporter, importer, _ = _pair()
    with pytest.raises(TypeError, match="RocmSemaphoreEvent"):
        exporter.export_event(object(), DEVICE)
    with pytest.raises(RuntimeError, match="ROCm LMCache event handle"):
        importer.import_event(b"\x01" * 64, DEVICE)
    with pytest.raises(RuntimeError, match="ROCm LMCache event handle"):
        importer.import_event(b"", DEVICE)


def test_handle_naming_a_slot_outside_the_region_is_rejected() -> None:
    hip = _FakeHip()
    exporter = RocmEventIPCBackend(ops=hip, slot_count=4)
    importer = RocmEventIPCBackend(ops=hip)
    handle = bytearray(exporter.export_event(exporter.create_event(DEVICE), DEVICE))
    handle[-8:-4] = (9).to_bytes(4, "big")
    with pytest.raises(RuntimeError, match="slot 9"):
        importer.import_event(bytes(handle), DEVICE)


def test_concurrent_records_get_distinct_increasing_sequence_numbers() -> None:
    exporter, _, hip = _pair()
    event = exporter.create_event(DEVICE)
    barrier = threading.Barrier(8)

    def record() -> None:
        barrier.wait()
        for _ in range(25):
            exporter.record_event(event, _Stream(1))

    threads = [threading.Thread(target=record) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writes = [value for kind, _, value in hip.queues[1] if kind == "write"]
    assert writes == list(range(1, 201))


def test_ipc_event_duck_wait_goes_through_the_backend() -> None:
    exporter, importer, hip = _pair()
    event = exporter.create_event(DEVICE)
    exporter.record_event(event, _Stream(1))
    imported = importer.import_event(exporter.export_event(event, DEVICE), DEVICE)
    assert isinstance(imported, RocmSemaphoreEvent)
    imported.wait(_Stream(4))
    assert hip.waits(4) == [1]


@pytest.fixture
def restore_isolated_ipc():
    """Restore the process-global isolated-IPC switch after the test."""
    previous = is_isolated_ipc()
    yield
    set_isolated_ipc(previous)


def test_rocm_device_spec_uses_the_rocm_backend(restore_isolated_ipc) -> None:
    set_isolated_ipc(False)
    spec = RocmDeviceSpec()
    assert isinstance(spec.event_ipc_backend, RocmEventIPCBackend)
    assert spec.event_ipc_backend is spec.event_ipc_backend


_IS_ROCM_GPU = torch.cuda.is_available() and torch.version.hip is not None
requires_rocm = pytest.mark.skipif(not _IS_ROCM_GPU, reason="requires a ROCm GPU")

#: About one second of ``torch.cuda._sleep`` on an MI300X.
_SPIN_PER_SECOND = int(2e9)


def _retarget_producer(conn: Connection) -> None:
    """Child: record behind a spin, then re-record as soon as it lands."""
    torch.cuda.set_device(0)
    backend = RocmEventIPCBackend()
    event = backend.create_event(DEVICE)
    conn.send(backend.export_event(event, DEVICE))
    conn.recv()
    torch.cuda._sleep(1 * _SPIN_PER_SECOND)
    backend.record_event(event, torch.cuda.current_stream())
    conn.send("recorded")
    conn.recv()  # the consumer has queued its wait
    backend.synchronize_event(event, DEVICE)
    torch.cuda._sleep(8 * _SPIN_PER_SECOND)
    backend.record_event(event, torch.cuda.current_stream())
    conn.send("re-recorded")
    conn.recv()
    torch.cuda.synchronize()


@requires_rocm
def test_hip_wait_queued_before_a_re_record_is_not_retargeted() -> None:
    """Native HIP events fail this: the marker lands ~9 s in, not ~3 s."""
    torch.cuda.set_device(0)
    backend = RocmEventIPCBackend()
    parent, child = get_context("spawn").Pipe()
    process = get_context("spawn").Process(target=_retarget_producer, args=(child,))
    process.start()
    try:
        imported = backend.import_event(parent.recv(), DEVICE)
        parent.send("go")
        parent.recv()
        start = time.monotonic()
        stream = torch.cuda.current_stream()
        torch.cuda._sleep(3 * _SPIN_PER_SECOND)  # busy ahead of the wait
        backend.wait_event(imported, stream)
        marker = torch.cuda.Event()
        marker.record(stream)
        parent.send("queued")
        parent.recv()
        marker.synchronize()
        elapsed = time.monotonic() - start
        parent.send("done")
    finally:
        process.join(timeout=120)
    assert process.exitcode == 0
    assert elapsed < 6.0, f"wait followed the re-record ({elapsed:.1f} s)"


def _data_producer(conn: Connection, rounds: int) -> None:
    """Child: fill a shared tensor behind a spin, then record, many times."""
    torch.cuda.set_device(0)
    backend = RocmEventIPCBackend()
    data = torch.zeros(1 << 20, dtype=torch.int32, device=DEVICE)
    event = backend.create_event(DEVICE)
    conn.send((data, backend.export_event(event, DEVICE)))
    for value in range(1, rounds + 1):
        conn.recv()
        torch.cuda._sleep(
            _SPIN_PER_SECOND // 5
        )  # a wait that didn't block reads stale data
        data.fill_(value)
        backend.record_event(event, torch.cuda.current_stream())
        if value % 2 == 0:
            event = backend.create_event(DEVICE)  # recycle slots as well
        conn.send(backend.export_event(event, DEVICE) if value % 2 == 0 else b"")
    conn.recv()
    torch.cuda.synchronize()


@requires_rocm
def test_hip_consumer_sees_data_written_before_each_record() -> None:
    torch.cuda.set_device(0)
    backend = RocmEventIPCBackend()
    rounds = 20
    parent, child = get_context("spawn").Pipe()
    process = get_context("spawn").Process(target=_data_producer, args=(child, rounds))
    process.start()
    try:
        data, handle = parent.recv()
        imported = backend.import_event(handle, DEVICE)
        stream = torch.cuda.current_stream()
        for value in range(1, rounds + 1):
            parent.send("go")
            next_handle = parent.recv()
            backend.wait_event(imported, stream)
            assert int(data.min().item()) == value
            assert int(data.max().item()) == value
            if next_handle:
                imported = backend.import_event(next_handle, DEVICE)
        parent.send("done")
    finally:
        process.join(timeout=120)
    assert process.exitcode == 0
