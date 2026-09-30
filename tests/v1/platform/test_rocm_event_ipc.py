# SPDX-License-Identifier: Apache-2.0
"""Tests for the ROCm event IPC backend (pooled exports, once-only imports).

The fake event module models the HIP rule the backend exists for: a process
can open a given interprocess handle only once. The final test exercises the
real HIP runtime across processes and runs only on ROCm GPUs.
"""

# Standard
from multiprocessing import get_context
from multiprocessing.connection import Connection
import gc
import threading

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.platform.base.event_ipc import EventIPCBackend
from lmcache.v1.platform.ipc_policy import is_isolated_ipc, set_isolated_ipc
from lmcache.v1.platform.rocm import RocmDeviceSpec
from lmcache.v1.platform.rocm.event_ipc import RocmEventIPCBackend, RocmPooledEvent

DEVICE = torch.device("cuda", 0)


class _FakeHipEvents:
    """Event module whose handles can be opened once per importing process."""

    def __init__(self) -> None:
        self.created = 0
        self.opened: list[bytes] = []
        self.attached: set[bytes] = set()
        module = self

        class Event:
            def __init__(self, interprocess: bool = False) -> None:
                module.created += 1
                self.handle = f"native-{module.created}".encode().ljust(64, b"\0")
                self.pending = False
                self.calls: list[tuple[object, ...]] = []

            def ipc_handle(self) -> bytes:
                return self.handle

            @classmethod
            def from_ipc_handle(cls, device: object, handle: bytes) -> "Event":
                if handle in module.attached:
                    raise RuntimeError("hipErrorInvalidValue: already attached")
                module.attached.add(handle)
                module.opened.append(handle)
                event = cls.__new__(cls)
                event.handle = handle
                event.pending = False
                event.calls = []
                return event

            def record(self, stream: object = None) -> None:
                self.calls.append(("record", stream))

            def wait(self, stream: object = None) -> None:
                self.calls.append(("wait", stream))

            def query(self) -> bool:
                return not self.pending

            def synchronize(self) -> None:
                self.calls.append(("synchronize",))

        self.Event = Event


def _backend() -> tuple[RocmEventIPCBackend, _FakeHipEvents]:
    events = _FakeHipEvents()
    return RocmEventIPCBackend(event_module=events, device_type="fake"), events


def test_backend_satisfies_protocol_and_support_check() -> None:
    backend, _ = _backend()
    assert isinstance(backend, EventIPCBackend)
    backend.check_event_support(DEVICE)  # must not raise


def test_repeated_imports_of_one_handle_open_it_once() -> None:
    exporter, _ = _backend()
    importer, importer_events = _backend()
    lease = exporter.create_event(DEVICE)
    exporter.record_event(lease, "STREAM")
    handle = exporter.export_event(lease, DEVICE)

    imported = [importer.import_event(handle, DEVICE) for _ in range(5)]

    assert len(importer_events.opened) == 1
    assert all(event is imported[0] for event in imported)


def test_concurrent_imports_of_one_handle_open_it_once() -> None:
    exporter, _ = _backend()
    importer, importer_events = _backend()
    handle = exporter.export_event(exporter.create_event(DEVICE), DEVICE)
    barrier = threading.Barrier(8)
    results: list[object] = []
    errors: list[BaseException] = []

    def import_once() -> None:
        barrier.wait()
        try:
            results.append(importer.import_event(handle, DEVICE))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=import_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(importer_events.opened) == 1
    assert all(event is results[0] for event in results)


def test_dropped_idle_lease_is_reused_with_the_same_handle() -> None:
    exporter, exporter_events = _backend()
    lease = exporter.create_event(DEVICE)
    first = exporter.export_event(lease, DEVICE)
    del lease
    gc.collect()

    second = exporter.export_event(exporter.create_event(DEVICE), DEVICE)

    assert exporter_events.created == 1
    assert second == first


def test_live_leases_never_share_an_event() -> None:
    exporter, exporter_events = _backend()
    leases = [exporter.create_event(DEVICE) for _ in range(3)]
    handles = {exporter.export_event(lease, DEVICE) for lease in leases}
    assert exporter_events.created == 3
    assert len(handles) == 3


def test_pooled_event_with_pending_work_is_not_handed_out() -> None:
    exporter, exporter_events = _backend()
    lease = exporter.create_event(DEVICE)
    busy_native = lease.native
    busy_native.pending = True
    del lease
    gc.collect()

    fresh = exporter.create_event(DEVICE)
    assert fresh.native is not busy_native
    assert exporter_events.created == 2

    busy_native.pending = False
    del fresh
    gc.collect()
    assert exporter.create_event(DEVICE).native is busy_native
    assert exporter_events.created == 2


def test_pools_are_kept_per_device() -> None:
    exporter, exporter_events = _backend()
    lease = exporter.create_event(torch.device("cuda", 0))
    del lease
    gc.collect()
    exporter.create_event(torch.device("cuda", 1))
    assert exporter_events.created == 2


def test_restarted_exporter_with_reused_native_handle_is_opened_fresh() -> None:
    """Same native bytes from a new exporter process must not hit the cache."""
    old_exporter, _ = _backend()
    new_exporter, _ = _backend()  # its first event has the same native bytes
    importer, importer_events = _backend()
    old_handle = old_exporter.export_event(old_exporter.create_event(DEVICE), DEVICE)
    new_handle = new_exporter.export_event(new_exporter.create_event(DEVICE), DEVICE)
    assert old_handle != new_handle
    assert old_handle[-64:] == new_handle[-64:]

    old_import = importer.import_event(old_handle, DEVICE)
    importer_events.attached.clear()  # the runtime released the dead peer's signal
    new_import = importer.import_event(new_handle, DEVICE)

    assert new_import is not old_import
    assert len(importer_events.opened) == 2


def test_import_rejects_handles_from_other_backends() -> None:
    importer, _ = _backend()
    with pytest.raises(RuntimeError, match="RocmEventIPCBackend"):
        importer.import_event(b"\x01" * 64, DEVICE)
    with pytest.raises(RuntimeError, match="RocmEventIPCBackend"):
        importer.import_event(b"", DEVICE)


def test_export_rejects_events_not_from_create_event() -> None:
    backend, events = _backend()
    with pytest.raises(TypeError, match="create_event"):
        backend.export_event(events.Event(interprocess=True), DEVICE)


def test_event_operations_reach_the_native_event() -> None:
    exporter, _ = _backend()
    importer, _ = _backend()
    lease = exporter.create_event(DEVICE)
    exporter.record_event(lease, "S1")
    lease.wait("S2")
    exporter.wait_event(lease, "S3")
    exporter.synchronize_event(lease, DEVICE)
    assert exporter.query_event(lease) is True
    assert lease.native.calls == [
        ("record", "S1"),
        ("wait", "S2"),
        ("wait", "S3"),
        ("synchronize",),
    ]

    imported = importer.import_event(exporter.export_event(lease, DEVICE), DEVICE)
    importer.wait_event(imported, "S4")
    importer.synchronize_event(imported, DEVICE)
    assert importer.query_event(imported) is True
    assert imported.calls == [("wait", "S4"), ("synchronize",)]


def test_create_event_returns_a_pooled_lease() -> None:
    backend, _ = _backend()
    assert isinstance(backend.create_event(DEVICE), RocmPooledEvent)


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


def _exporter_process(conn: Connection) -> None:
    """Child: export a pooled event, recycle it, and re-record it on request."""
    torch.cuda.set_device(0)
    backend = RocmEventIPCBackend()
    lease = backend.create_event(DEVICE)
    backend.record_event(lease, torch.cuda.current_stream())
    conn.send(backend.export_event(lease, DEVICE))
    del lease
    gc.collect()
    torch.cuda.synchronize()
    lease = backend.create_event(DEVICE)
    conn.recv()
    torch.cuda._sleep(int(2e9))
    backend.record_event(lease, torch.cuda.current_stream())
    conn.send(backend.export_event(lease, DEVICE))
    conn.recv()
    torch.cuda.synchronize()


@pytest.mark.skipif(not _IS_ROCM_GPU, reason="requires a ROCm GPU")
def test_hip_handles_import_repeatedly_and_follow_recycled_records() -> None:
    torch.cuda.set_device(0)
    importer = RocmEventIPCBackend()
    parent, child = get_context("spawn").Pipe()
    process = get_context("spawn").Process(target=_exporter_process, args=(child,))
    process.start()
    try:
        first = parent.recv()
        imported = [importer.import_event(first, DEVICE) for _ in range(4)]
        assert all(event is imported[0] for event in imported)
        for event in imported:
            importer.wait_event(event, torch.cuda.current_stream())
        torch.cuda.synchronize()

        parent.send("record")
        second = parent.recv()
        assert second == first  # the recycled event kept its handle
        event = importer.import_event(second, DEVICE)
        assert event is imported[0]
        assert importer.query_event(event) is False  # ~1 s GPU spin pending
        importer.synchronize_event(event, DEVICE)
        assert importer.query_event(event) is True
        parent.send("done")
    finally:
        process.join(timeout=120)
    assert process.exitcode == 0
