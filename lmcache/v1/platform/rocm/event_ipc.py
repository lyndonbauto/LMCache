# SPDX-License-Identifier: Apache-2.0
"""ROCm event IPC with CUDA's semantics, built on shared-memory semaphores.

LMCache orders cross-process transfers with interprocess events and relies on
CUDA's rule: a stream wait targets the record the event held when the wait was
enqueued. Native HIP interprocess events break that rule and two others (all
observed on an MI300X with ROCm 10, see
``docs/design/v1/platform/rocm/event_ipc.md``):

- A queued wait follows the event's live signal. If the exporter re-records
  the event before the waiting stream reaches the wait, the wait now blocks on
  the new record. LMCache re-records its per-layer events on every retrieve,
  so with vLLM's async scheduling a forward pass ends up waiting on the next
  step's copy, which is queued behind that same forward pass: a deadlock.
- A process can open a given handle only once.
- Handle bytes repeat once an exporter frees an event.

This backend implements events as 64-bit counters (timeline semaphores) in a
POSIX shared-memory segment that every process registers with HIP:

- ``record_event`` enqueues ``hipStreamWriteValue64(slot, seq)``: the counter
  reaches ``seq`` when the work before it on the stream has finished.
- ``wait_event`` reads the event's latest ``seq`` on the host, now, and
  enqueues ``hipStreamWaitValue64(slot >= seq)``. Later records raise the
  counter further and cannot retarget the wait.
- ``query_event`` and ``synchronize_event`` compare the counter with the same
  target.

A slot is recycled only after its last record has completed. Each reuse bumps
the slot's epoch, and a handle whose epoch is out of date names an event whose
final record is known to have completed, so waiting on it is a no-op.

Both processes must share ``/dev/shm`` (``--ipc host`` between containers), as
the layerwise progress record already requires.
"""

# Future
from __future__ import annotations

# Standard
from collections import deque
from dataclasses import dataclass, field
from multiprocessing import resource_tracker, shared_memory
from typing import Protocol
import ctypes
import enum
import os
import secrets
import struct
import sys
import threading
import weakref

# First Party
from lmcache.v1.platform.base.event_ipc import EventIPCBackend
from lmcache.v1.platform.cuda.utils import _raw_stream_handle, _resolve_device_index
from lmcache.v1.platform.runtime_libs import load_runtime_library

#: Slots per process; each live event holds one.
DEFAULT_SLOT_COUNT = 4096

_SEGMENT_MAGIC = b"LMRSEM01"
_SEGMENT_HEADER = struct.Struct("<8sI")
_HEADER_BYTES = 64
_WORD = 8

#: Handle wire format: magic, version, segment-name length; then the name,
#: then slot and epoch.
_HANDLE_MAGIC = b"LMRS"
_HANDLE_VERSION = 2
_HANDLE_PREFIX = struct.Struct("!4sBB")
_HANDLE_SUFFIX = struct.Struct("!II")

#: A published cell packs ``epoch << 40 | seq``, so one 8-byte read is
#: consistent.
_SEQ_BITS = 40
_SEQ_MASK = (1 << _SEQ_BITS) - 1
_EPOCH_MASK = (1 << 24) - 1

_HIP_STREAM_NON_BLOCKING = 0x1
_HIP_HOST_REGISTER_PORTABLE_MAPPED = 0x1 | 0x2
_HIP_STREAM_WAIT_VALUE_GTE = 0x0
_ALL_BITS = 0xFFFFFFFFFFFFFFFF


def _attach_untracked(name: str) -> shared_memory.SharedMemory:
    """Attach to another process's segment without adopting its lifetime.

    CPython's resource tracker would otherwise unlink an attached segment
    when this process exits, deleting it under its owner.
    """
    if sys.version_info >= (3, 13):
        return shared_memory.SharedMemory(name=name, track=False)
    segment = shared_memory.SharedMemory(name=name)
    if os.name == "posix":
        resource_tracker.unregister(f"/{segment.name}", "shared_memory")
    return segment


def _segment_bytes(slot_count: int) -> int:
    """Return the page-rounded size of a region with ``slot_count`` slots."""
    raw = _HEADER_BYTES + 2 * slot_count * _WORD
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    return -(-raw // page) * page


def _unlink_quietly(segment: shared_memory.SharedMemory) -> None:
    """Remove a segment's name; processes that mapped it keep their mapping."""
    try:
        segment.unlink()
    except FileNotFoundError:
        pass


def _close_region(
    ops: SemaphoreOps,
    base: int,
    views: list[memoryview],
    segment: shared_memory.SharedMemory,
    owner: bool,
) -> None:
    """Unregister, unmap and (for the owner) unlink a region.

    HIP must forget the range before it is unmapped: a later mapping at the
    same address would otherwise fail to register ("already mapped").
    """
    try:
        ops.host_unregister(base)
    except Exception:  # noqa: BLE001 - the runtime may already be torn down
        pass
    for view in views:
        view.release()
    segment.close()
    if owner:
        _unlink_quietly(segment)


def _stream_device_index(stream: object) -> int:
    """Return the device a stream belongs to, or the current device."""
    device = getattr(stream, "device", None)
    index = getattr(device, "index", None)
    if isinstance(index, int):
        return index
    return _resolve_device_index(None)


class SemaphoreOps(Protocol):
    """The HIP calls the backend needs; injectable for testing.

    Addresses are ints; streams are raw ``hipStream_t`` values as ints. Every
    method raises ``RuntimeError`` when the runtime reports an error.
    """

    def host_register(self, address: int, nbytes: int) -> None:
        """Pin and map host memory for device access (``hipHostRegister``)."""
        ...

    def host_unregister(self, address: int) -> None:
        """Undo :meth:`host_register` for the range starting at ``address``."""
        ...

    def device_address(self, address: int, device_index: int) -> int:
        """Return the device-visible address of registered host memory."""
        ...

    def write_value64(self, stream: int, address: int, value: int) -> None:
        """Enqueue a 64-bit store of ``value`` on ``stream``."""
        ...

    def wait_value64_geq(self, stream: int, address: int, value: int) -> None:
        """Enqueue a wait until the 64-bit value at ``address`` is >= ``value``."""
        ...

    def create_stream(self, device_index: int) -> int:
        """Create a non-blocking stream on ``device_index``."""
        ...

    def synchronize_stream(self, stream: int) -> None:
        """Block the host until ``stream`` has drained."""
        ...


class HipSemaphoreOps:
    """:class:`SemaphoreOps` over the HIP runtime PyTorch already loaded.

    Raises:
        RuntimeError: If the HIP runtime cannot be loaded.
    """

    def __init__(self) -> None:
        # Third Party
        import torch

        torch.cuda.init()  # map PyTorch's HIP runtime before looking it up
        lib = load_runtime_library("libamdhip64", ["libamdhip64.so"])
        if lib is None:
            raise RuntimeError("cannot load the HIP runtime (libamdhip64)")
        self._lib = lib
        lib.hipGetErrorString.restype = ctypes.c_char_p
        lib.hipGetErrorString.argtypes = [ctypes.c_int]
        lib.hipHostRegister.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]
        lib.hipHostUnregister.argtypes = [ctypes.c_void_p]
        lib.hipHostGetDevicePointer.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        lib.hipStreamWriteValue64.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint,
        ]
        lib.hipStreamWaitValue64.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint,
            ctypes.c_uint64,
        ]
        lib.hipStreamCreateWithFlags.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint,
        ]
        lib.hipStreamSynchronize.argtypes = [ctypes.c_void_p]

    def host_register(self, address: int, nbytes: int) -> None:
        """Pin and map host memory for every device."""
        self._check(
            self._lib.hipHostRegister(
                ctypes.c_void_p(address),
                ctypes.c_size_t(nbytes),
                _HIP_HOST_REGISTER_PORTABLE_MAPPED,
            ),
            "hipHostRegister",
        )

    def host_unregister(self, address: int) -> None:
        """Unregister host memory registered at ``address``."""
        self._check(
            self._lib.hipHostUnregister(ctypes.c_void_p(address)),
            "hipHostUnregister",
        )

    def device_address(self, address: int, device_index: int) -> int:
        """Return the device address of registered host memory."""
        # Third Party
        import torch

        pointer = ctypes.c_void_p()
        with torch.cuda.device(device_index):
            self._check(
                self._lib.hipHostGetDevicePointer(
                    ctypes.byref(pointer), ctypes.c_void_p(address), 0
                ),
                "hipHostGetDevicePointer",
            )
        return int(pointer.value or 0)

    def write_value64(self, stream: int, address: int, value: int) -> None:
        """Enqueue a 64-bit store on ``stream``."""
        self._check(
            self._lib.hipStreamWriteValue64(
                ctypes.c_void_p(stream), ctypes.c_void_p(address), value, 0
            ),
            "hipStreamWriteValue64",
        )

    def wait_value64_geq(self, stream: int, address: int, value: int) -> None:
        """Enqueue a wait for ``*address >= value`` on ``stream``."""
        self._check(
            self._lib.hipStreamWaitValue64(
                ctypes.c_void_p(stream),
                ctypes.c_void_p(address),
                value,
                _HIP_STREAM_WAIT_VALUE_GTE,
                _ALL_BITS,
            ),
            "hipStreamWaitValue64",
        )

    def create_stream(self, device_index: int) -> int:
        """Create a non-blocking stream on ``device_index``."""
        # Third Party
        import torch

        stream = ctypes.c_void_p()
        with torch.cuda.device(device_index):
            self._check(
                self._lib.hipStreamCreateWithFlags(
                    ctypes.byref(stream), _HIP_STREAM_NON_BLOCKING
                ),
                "hipStreamCreateWithFlags",
            )
        return int(stream.value or 0)

    def synchronize_stream(self, stream: int) -> None:
        """Block until ``stream`` has drained."""
        self._check(
            self._lib.hipStreamSynchronize(ctypes.c_void_p(stream)),
            "hipStreamSynchronize",
        )

    def _check(self, status: int, what: str) -> None:
        """Raise ``RuntimeError`` for a non-zero ``hipError_t``."""
        if status != 0:
            message = self._lib.hipGetErrorString(status)
            text = message.decode() if message else "unknown error"
            raise RuntimeError(f"{what} failed: {text} ({status})")


class _SemaphoreRegion:
    """One process's slots: device-written counters and host-published targets.

    Layout after a 64-byte header: ``values[slot_count]`` (written by the GPU
    when a record completes), then ``published[slot_count]`` (the latest
    ``epoch << 40 | seq`` the owner has enqueued for each slot).

    The region stays registered and mapped for as long as the object lives;
    the backend and every event on it hold a reference. When it is collected
    it is unregistered from HIP, unmapped and, by its owner, unlinked.
    """

    def __init__(
        self,
        segment: shared_memory.SharedMemory,
        slot_count: int,
        ops: SemaphoreOps,
        owner: bool,
    ) -> None:
        self.name = segment.name
        self.slot_count = slot_count
        self._ops = ops
        buffer = segment.buf
        if buffer is None:
            raise RuntimeError(f"shared memory segment {segment.name} is closed")
        words = slot_count * _WORD
        values_bytes = buffer[_HEADER_BYTES : _HEADER_BYTES + words]
        published_bytes = buffer[_HEADER_BYTES + words : _HEADER_BYTES + 2 * words]
        self._values = values_bytes.cast("Q")
        self._published = published_bytes.cast("Q")
        self._base = ctypes.addressof(ctypes.c_char.from_buffer(buffer))
        self._device_bases: dict[int, int] = {}
        self._lock = threading.Lock()
        views = [self._values, self._published, values_bytes, published_bytes]
        try:
            ops.host_register(self._base, _segment_bytes(slot_count))
        except Exception:
            for view in views:
                view.release()
            segment.close()
            if owner:
                _unlink_quietly(segment)
            raise
        weakref.finalize(self, _close_region, ops, self._base, views, segment, owner)

    @classmethod
    def create(cls, ops: SemaphoreOps, slot_count: int) -> _SemaphoreRegion:
        """Create and register this process's region."""
        name = f"lmcache_rocm_evt_{os.getpid()}_{secrets.token_hex(6)}"
        size = _segment_bytes(slot_count)
        segment = shared_memory.SharedMemory(name=name, create=True, size=size)
        buffer = segment.buf
        if buffer is None:
            raise RuntimeError(f"shared memory segment {name} is closed")
        buffer[:size] = bytes(size)
        _SEGMENT_HEADER.pack_into(buffer, 0, _SEGMENT_MAGIC, slot_count)
        return cls(segment, slot_count, ops, owner=True)

    @classmethod
    def attach(cls, ops: SemaphoreOps, name: str) -> _SemaphoreRegion:
        """Attach and register another process's region.

        Raises:
            RuntimeError: If the segment is not a region of this format.
            FileNotFoundError: If the segment does not exist.
        """
        segment = _attach_untracked(name)
        buffer = segment.buf
        slot_count = 0
        if buffer is not None and len(buffer) >= _SEGMENT_HEADER.size:
            magic, slot_count = _SEGMENT_HEADER.unpack_from(buffer, 0)
            if magic != _SEGMENT_MAGIC or len(buffer) < _segment_bytes(slot_count):
                slot_count = 0
        if slot_count == 0:
            segment.close()
            raise RuntimeError(f"{name} is not a ROCm LMCache event region")
        return cls(segment, slot_count, ops, owner=False)

    def read_published(self, slot: int) -> tuple[int, int]:
        """Return ``(epoch, seq)`` last published for ``slot``."""
        cell = self._published[slot]
        return cell >> _SEQ_BITS, cell & _SEQ_MASK

    def write_published(self, slot: int, epoch: int, seq: int) -> None:
        """Publish ``(epoch, seq)`` for ``slot`` in one 8-byte store."""
        self._published[slot] = (epoch << _SEQ_BITS) | seq

    def read_value(self, slot: int) -> int:
        """Return the counter the GPU last wrote for ``slot``."""
        return self._values[slot]

    def value_device_address(self, slot: int, device_index: int) -> int:
        """Return ``values[slot]``'s address as seen from ``device_index``."""
        base = self._device_bases.get(device_index)
        if base is None:
            with self._lock:
                base = self._device_bases.get(device_index)
                if base is None:
                    base = self._ops.device_address(self._base, device_index)
                    self._device_bases[device_index] = base
        return base + _HEADER_BYTES + slot * _WORD


class RocmEventOrigin(enum.Enum):
    """Which side of the wire a :class:`RocmSemaphoreEvent` was created on."""

    LOCAL = "local"
    IMPORTED = "imported"


@dataclass(eq=False)
class RocmSemaphoreEvent:
    """An event: a slot and epoch in some process's semaphore region.

    Created by :class:`RocmEventIPCBackend`; callers treat it as opaque. A
    local event returns its slot to the backend when it is garbage-collected.

    Attributes:
        origin: LOCAL events can be recorded; IMPORTED ones only waited on.
        slot: Index into the region's counters.
        epoch: Which use of the slot this event is.
        region: The semaphore region that holds the slot.
    """

    origin: RocmEventOrigin
    slot: int
    epoch: int
    region: _SemaphoreRegion = field(repr=False)
    _backend: RocmEventIPCBackend = field(repr=False)

    def wait(self, stream: object | None = None) -> None:
        """Make ``stream`` wait for this event (``IPCEvent`` duck method).

        Args:
            stream: Stream that should wait; ``None`` for the current stream.
        """
        self._backend.wait_event(self, stream)

    def __del__(self) -> None:
        if self.origin is RocmEventOrigin.LOCAL:
            self._backend.release_slot(self.slot)


@dataclass(frozen=True)
class _DeviceIndex:
    """Stand-in for ``torch.device`` exposing only ``index``."""

    index: int


class _RawStream:
    """A raw stream handle with its device, shaped like ``torch.cuda.Stream``."""

    def __init__(self, handle: int, device_index: int) -> None:
        self.cuda_stream = handle
        self.device = _DeviceIndex(device_index)


class RocmEventIPCBackend(EventIPCBackend):
    """Interprocess events for ROCm with CUDA's wait semantics.

    See the module docstring for the design.

    Thread safety: every method may be called from any thread. Records of one
    slot are serialized, so its counter only ever increases.

    Args:
        ops: HIP calls; defaults to :class:`HipSemaphoreOps`, created on
            first use. Injectable for testing.
        slot_count: Live events this process can hold at once.
        device_type: Device-type label used in error messages.
    """

    def __init__(
        self,
        ops: SemaphoreOps | None = None,
        slot_count: int = DEFAULT_SLOT_COUNT,
        device_type: str = "cuda",
    ) -> None:
        if slot_count <= 0 or slot_count > (1 << 32) - 1:
            raise ValueError(f"slot_count must be in [1, 2**32), got {slot_count}")
        self.device_type = device_type
        self._ops = ops
        self._slot_count = slot_count
        self._lock = threading.Lock()
        self._local: _SemaphoreRegion | None = None
        self._attached: dict[str, _SemaphoreRegion] = {}
        self._free_slots: deque[int] = deque(range(slot_count))
        self._released_slots: deque[int] = deque()
        self._sync_streams = threading.local()

    def check_event_support(self, device: object) -> None:
        """Round-trip one record and wait through the GPU on ``device``.

        Raises:
            RuntimeError: If the HIP runtime or shared memory is unusable.
        """
        try:
            device_index = _resolve_device_index(device)
            event = self.create_event(device)
            stream = _RawStream(self._sync_stream(device_index), device_index)
            self.record_event(event, stream)
            self.synchronize_event(event, device)
            if not self.query_event(event):
                raise RuntimeError("probe record did not complete")
        except Exception as e:
            raise RuntimeError(
                f"Device backend '{self.device_type}' does not support ROCm "
                f"semaphore event IPC on {device}: {e}"
            ) from e

    def create_event(self, device: object) -> object:
        """Create an event on this process's region.

        Args:
            device: Unused; a region serves every device.

        Returns:
            A local :class:`RocmSemaphoreEvent` that counts as complete until
            it is first recorded.

        Raises:
            RuntimeError: If every slot holds a live event or an unfinished
                record.
        """
        region = self._local_region()
        with self._lock:
            self._reclaim_finished_slots_locked(region)
            if not self._free_slots:
                raise RuntimeError(
                    f"all {self._slot_count} ROCm event slots are in use; "
                    "raise RocmEventIPCBackend(slot_count=...)"
                )
            slot = self._free_slots.popleft()
            epoch, seq = region.read_published(slot)
            epoch = (epoch + 1) & _EPOCH_MASK
            region.write_published(slot, epoch, seq)
        return RocmSemaphoreEvent(RocmEventOrigin.LOCAL, slot, epoch, region, self)

    def export_event(self, event: object, device: object) -> bytes:
        """Serialize ``event`` as its region name, slot and epoch.

        Raises:
            TypeError: If ``event`` did not come from this backend.
        """
        semaphore_event = self._require_event(event, "export_event")
        name = semaphore_event.region.name.encode()
        return (
            _HANDLE_PREFIX.pack(_HANDLE_MAGIC, _HANDLE_VERSION, len(name))
            + name
            + _HANDLE_SUFFIX.pack(semaphore_event.slot, semaphore_event.epoch)
        )

    def import_event(self, handle: bytes, device: object) -> object:
        """Return an event for ``handle``; it can be waited on, never recorded.

        The exporter's region is attached once per process and reused.

        Raises:
            RuntimeError: If ``handle`` was not produced by this backend or
                names a slot outside its region.
        """
        try:
            magic, version, name_len = _HANDLE_PREFIX.unpack_from(handle, 0)
            name = handle[_HANDLE_PREFIX.size : _HANDLE_PREFIX.size + name_len].decode()
            slot, epoch = _HANDLE_SUFFIX.unpack_from(
                handle, _HANDLE_PREFIX.size + name_len
            )
        except (struct.error, UnicodeDecodeError) as e:
            raise RuntimeError(
                f"Not a ROCm LMCache event handle ({len(handle)} bytes): {e}"
            ) from e
        if magic != _HANDLE_MAGIC or version != _HANDLE_VERSION:
            raise RuntimeError(
                "Not a ROCm LMCache event handle; both processes must use "
                "RocmEventIPCBackend of the same version."
            )
        region = self._region_named(name)
        if slot >= region.slot_count:
            raise RuntimeError(
                f"event handle names slot {slot}; region {name} has {region.slot_count}"
            )
        return RocmSemaphoreEvent(RocmEventOrigin.IMPORTED, slot, epoch, region, self)

    def record_event(self, event: object, stream: object) -> None:
        """Enqueue ``event``'s next record on ``stream``.

        The counter write is preceded by a wait for the slot's previous
        record, so records from different streams still complete in order.

        Raises:
            TypeError: If ``event`` did not come from this backend.
            RuntimeError: If ``event`` was imported (the importer never
                records), or has already been released.
        """
        semaphore_event = self._require_event(event, "record_event")
        if semaphore_event.origin is not RocmEventOrigin.LOCAL:
            raise RuntimeError(
                "An imported ROCm event cannot be recorded; only the exporting "
                "process records."
            )
        region = semaphore_event.region
        device_index = _stream_device_index(stream)
        raw_stream = _raw_stream_handle(stream, device_index)
        address = region.value_device_address(semaphore_event.slot, device_index)
        ops = self._get_ops()
        with self._lock:
            epoch, seq = region.read_published(semaphore_event.slot)
            if epoch != semaphore_event.epoch:
                raise RuntimeError("ROCm event recorded after it was released")
            next_seq = seq + 1
            if next_seq > _SEQ_MASK:
                raise RuntimeError(f"ROCm event slot {semaphore_event.slot} wrapped")
            if seq > 0:
                ops.wait_value64_geq(raw_stream, address, seq)
            ops.write_value64(raw_stream, address, next_seq)
            region.write_published(semaphore_event.slot, epoch, next_seq)

    def wait_event(self, event: object, stream: object) -> None:
        """Make ``stream`` wait for ``event``'s latest record as of now."""
        semaphore_event = self._require_event(event, "wait_event")
        target = self._target(semaphore_event)
        if target == 0:
            return
        device_index = _stream_device_index(stream)
        raw_stream = _raw_stream_handle(stream, device_index)
        address = semaphore_event.region.value_device_address(
            semaphore_event.slot, device_index
        )
        self._get_ops().wait_value64_geq(raw_stream, address, target)

    def query_event(self, event: object) -> bool:
        """Return whether ``event``'s latest record has completed."""
        semaphore_event = self._require_event(event, "query_event")
        target = self._target(semaphore_event)
        region = semaphore_event.region
        return target == 0 or region.read_value(semaphore_event.slot) >= target

    def synchronize_event(self, event: object, device: object) -> None:
        """Block the host until ``event``'s latest record has completed.

        The wait runs in the driver on a per-thread stream, so the host does
        not spin.
        """
        semaphore_event = self._require_event(event, "synchronize_event")
        target = self._target(semaphore_event)
        region = semaphore_event.region
        if target == 0 or region.read_value(semaphore_event.slot) >= target:
            return
        device_index = _resolve_device_index(device)
        stream = self._sync_stream(device_index)
        ops = self._get_ops()
        address = region.value_device_address(semaphore_event.slot, device_index)
        ops.wait_value64_geq(stream, address, target)
        ops.synchronize_stream(stream)

    def release_slot(self, slot: int) -> None:
        """Hand back a local event's slot; called when the event is collected.

        The slot is reused only once its last record has completed.
        ``deque.append`` is atomic, so a finalizer on any thread never blocks.

        Args:
            slot: The released event's slot.
        """
        self._released_slots.append(slot)

    def _target(self, event: RocmSemaphoreEvent) -> int:
        """Return the counter value ``event`` completes at; 0 means complete.

        A newer epoch means the event was released and its slot reused, which
        happens only after its last record completed.
        """
        epoch, seq = event.region.read_published(event.slot)
        return seq if epoch == event.epoch else 0

    def _reclaim_finished_slots_locked(self, region: _SemaphoreRegion) -> None:
        """Move released slots whose last record completed to the free list."""
        for _ in range(len(self._released_slots)):
            slot = self._released_slots.popleft()
            _, seq = region.read_published(slot)
            if region.read_value(slot) >= seq:
                self._free_slots.append(slot)
            else:
                self._released_slots.append(slot)

    def _local_region(self) -> _SemaphoreRegion:
        """Return this process's region, creating it on first use."""
        region = self._local
        if region is not None:
            return region
        with self._lock:
            if self._local is None:
                self._local = _SemaphoreRegion.create(self._get_ops(), self._slot_count)
            return self._local

    def _region_named(self, name: str) -> _SemaphoreRegion:
        """Return the local region or an attached one for ``name``."""
        local = self._local
        if local is not None and local.name == name:
            return local
        with self._lock:
            region = self._attached.get(name)
            if region is None:
                region = _SemaphoreRegion.attach(self._get_ops(), name)
                self._attached[name] = region
            return region

    def _get_ops(self) -> SemaphoreOps:
        """Return the HIP calls, loading the runtime on first use."""
        if self._ops is None:
            self._ops = HipSemaphoreOps()
        return self._ops

    def _sync_stream(self, device_index: int) -> int:
        """Return this thread's private stream for host waits on a device."""
        streams: dict[int, int] | None = getattr(self._sync_streams, "streams", None)
        if streams is None:
            streams = {}
            self._sync_streams.streams = streams
        stream = streams.get(device_index)
        if stream is None:
            stream = self._get_ops().create_stream(device_index)
            streams[device_index] = stream
        return stream

    @staticmethod
    def _require_event(event: object, operation: str) -> RocmSemaphoreEvent:
        """Return ``event`` as a :class:`RocmSemaphoreEvent` or raise."""
        if not isinstance(event, RocmSemaphoreEvent):
            raise TypeError(
                f"{operation} expected a RocmSemaphoreEvent from "
                f"RocmEventIPCBackend, got {type(event)!r}"
            )
        return event
