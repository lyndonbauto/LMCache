# SPDX-License-Identifier: Apache-2.0
"""ROCm interprocess event backend that respects HIP's single-attach limit.

ROCm backs an interprocess event with a ROCr IPC signal, and its handle is
essentially the creator's pid plus the signal's address. Two runtime rules
follow, both observed on an MI300X with ROCm 10:

- A process can open a given handle only once. A second
  ``hipIpcOpenEventHandle`` fails with ``hipErrorInvalidValue``, even after the
  first imported event was destroyed, because destruction only queues the
  signal for deferred cleanup.
- Handle bytes are reused once the exporter frees a signal address, and
  exporting a new event onto an address another process still has attached
  fails.

LMCache creates one event per transfer and imports it once per request, so a
step with several requests opens the same handle several times. This backend
keeps the native events (the layerwise path relies on an imported event
following its exporter's re-records) and changes their lifetimes instead:

1. The exporter never destroys an interprocess event. ``create_event`` lends
   one out of a pool; it returns to the pool when the caller drops it.
2. The importer opens each exported handle once and reuses the event.
3. Exports carry a random per-process nonce after the native handle, so a
   restarted peer that reuses a pid and signal address is a cache miss rather
   than a stale hit.

A recycled event can be re-recorded while a peer has not yet waited on its
previous use; that peer then waits for the newer record. The newer record is
enqueued before its handle leaves the exporter, and it only follows work that
was enqueued before it, so the extra wait cannot deadlock.
See ``docs/design/v1/platform/rocm/event_ipc.md``.
"""

# Future
from __future__ import annotations

# Standard
from collections import deque
import os
import threading

# First Party
from lmcache.v1.platform.base.event_ipc import DefaultEventIPCBackend
from lmcache.v1.platform.cuda.utils import _resolve_device_index

#: Prefix of every handle this backend exports: magic and format version.
_EXPORT_MAGIC = b"LMRE\x01"
_NONCE_BYTES = 16


def _native(event: object) -> object:
    """Return the native event behind a pooled lease, or ``event`` itself."""
    return event.native if isinstance(event, RocmPooledEvent) else event


class RocmPooledEvent:
    """An interprocess event on loan from a :class:`RocmEventIPCBackend` pool.

    Pass it to the backend's methods like any other event. When the last
    reference is dropped, the native event goes back to the pool instead of
    being destroyed.

    Args:
        native: The native ``torch.cuda.Event(interprocess=True)``.
        free_list: The pool the native event returns to.
    """

    def __init__(self, native: object, free_list: deque[object]) -> None:
        self._native = native
        self._free_list = free_list

    @property
    def native(self) -> object:
        """The native interprocess event this lease wraps."""
        return self._native

    def wait(self, stream: object | None = None) -> None:
        """Make ``stream`` wait for this event (``IPCEvent`` duck method).

        Args:
            stream: Stream that should wait; ``None`` for the current stream.
        """
        self._native.wait(stream)  # type: ignore[attr-defined]

    def __del__(self) -> None:
        # deque.append is atomic, so a finalizer running on any thread (or
        # inside a garbage collection triggered while the pool lock is held)
        # never blocks.
        self._free_list.append(self._native)


class RocmEventIPCBackend(DefaultEventIPCBackend):
    """HIP interprocess events with pooled exports and once-only imports.

    See the module docstring for the ROCm rules this works around.

    Thread safety: every method may be called from any thread. Imports are
    serialized so two threads cannot open the same handle.

    Args:
        event_module: Object exposing an ``Event`` class with the interface
            described in :class:`DefaultEventIPCBackend`. Defaults to
            ``torch.cuda``; injectable for testing.
        device_type: Device-type label used in error messages.
    """

    def __init__(
        self,
        event_module: object | None = None,
        device_type: str = "cuda",
    ) -> None:
        if event_module is None:
            # Third Party
            import torch

            event_module = torch.cuda
        super().__init__(event_module=event_module, device_type=device_type)
        self._nonce = os.urandom(_NONCE_BYTES)
        self._pool_lock = threading.Lock()
        self._free_lists: dict[int, deque[object]] = {}
        self._import_lock = threading.Lock()
        self._imports: dict[tuple[bytes, int], object] = {}
        self._import_key_by_native: dict[tuple[bytes, int], bytes] = {}

    def create_event(self, device: object) -> object:
        """Lend out an interprocess event for ``device``.

        Reuses a pooled event whose last record has completed, so recording
        it never waits on earlier work; creates a new event otherwise.

        Args:
            device: Device whose streams will record the event.

        Returns:
            A :class:`RocmPooledEvent`.
        """
        free_list = self._free_list(_resolve_device_index(device))
        for _ in range(len(free_list)):
            try:
                native = free_list.popleft()
            except IndexError:
                break
            if native.query():  # type: ignore[attr-defined]
                return RocmPooledEvent(native, free_list)
            free_list.append(native)
        native = self._event_module.Event(interprocess=True)  # type: ignore[attr-defined]
        return RocmPooledEvent(native, free_list)

    def export_event(self, event: object, device: object) -> bytes:
        """Serialize ``event`` as the native handle plus this process's nonce.

        Args:
            event: A :class:`RocmPooledEvent` from :meth:`create_event`.
            device: Device that owns the event (unused).

        Returns:
            ``magic + nonce + native handle`` bytes for :meth:`import_event`.

        Raises:
            TypeError: If ``event`` did not come from :meth:`create_event`.
                Only pooled events are safe to export (see module docstring).
        """
        if not isinstance(event, RocmPooledEvent):
            raise TypeError(
                "RocmEventIPCBackend exports only events from create_event(), "
                f"got {type(event)!r}"
            )
        native_handle = bytes(event.native.ipc_handle())  # type: ignore[attr-defined]
        return _EXPORT_MAGIC + self._nonce + native_handle

    def import_event(self, handle: bytes, device: object) -> object:
        """Return the event for ``handle``, opening it on first use only.

        Args:
            handle: Bytes produced by :meth:`export_event` in another process.
            device: Device on which to import the event.

        Returns:
            The native imported event. Every call with the same ``handle`` and
            device returns the same object.

        Raises:
            RuntimeError: If ``handle`` was not produced by this backend.
        """
        prefix_len = len(_EXPORT_MAGIC) + _NONCE_BYTES
        if len(handle) <= prefix_len or not handle.startswith(_EXPORT_MAGIC):
            raise RuntimeError(
                "Not a ROCm LMCache event handle "
                f"({len(handle)} bytes); both processes must use "
                "RocmEventIPCBackend."
            )
        native_handle = handle[prefix_len:]
        device_index = _resolve_device_index(device)
        key = (handle, device_index)
        native_key = (native_handle, device_index)
        with self._import_lock:
            imported = self._imports.get(key)
            if imported is not None:
                return imported
            stale_handle = self._import_key_by_native.pop(native_key, None)
            if stale_handle is not None:
                # A restarted exporter reused the pid and signal address.
                self._imports.pop((stale_handle, device_index), None)
            imported = self._event_module.Event.from_ipc_handle(  # type: ignore[attr-defined]
                device, native_handle
            )
            self._imports[key] = imported
            self._import_key_by_native[native_key] = handle
            return imported

    def record_event(self, event: object, stream: object) -> None:
        """Record ``event`` on ``stream``."""
        super().record_event(_native(event), stream)

    def wait_event(self, event: object, stream: object) -> None:
        """Make ``stream`` wait for ``event``."""
        super().wait_event(_native(event), stream)

    def query_event(self, event: object) -> bool:
        """Return whether ``event`` has completed."""
        return super().query_event(_native(event))

    def synchronize_event(self, event: object, device: object) -> None:
        """Block the host until ``event`` completes."""
        super().synchronize_event(_native(event), device)

    def _free_list(self, device_index: int) -> deque[object]:
        """Return the pool of idle native events for ``device_index``."""
        with self._pool_lock:
            free_list = self._free_lists.get(device_index)
            if free_list is None:
                free_list = deque()
                self._free_lists[device_index] = free_list
            return free_list
