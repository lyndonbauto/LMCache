# SPDX-License-Identifier: Apache-2.0
"""Lifetime of the layerwise progress segment shared by worker and daemon (B6).

The vLLM worker creates and unlinks the segment; the LMCache daemon only
attaches. These tests use real POSIX shared memory, and two of them start a
separate interpreter, because the failure they guard against -- a process
deleting a segment it does not own when it exits -- only shows up when a
process actually exits.
"""

# Standard
from collections.abc import Callable, Iterator
from multiprocessing import shared_memory
from types import SimpleNamespace
from unittest.mock import MagicMock
import os
import random
import subprocess
import sys

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.multiprocess.custom_types import RegisterKvCacheResponse
from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.layer_progress import (
    LayerProgressRecord,
    LayerProgressRetrieveFailedError,
    attach_layer_progress_shm,
    layer_progress_shm_name,
)
from lmcache.v1.multiprocess.transfer_context import worker_transfer

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX shared memory semantics"
)

#: Scheduled layers in the fake registration; one launch ordinal each.
_LAYERS = [0, 1]

#: Builds a registered worker context: ``(instance_id, server_use_layerwise)``.
_ContextFactory = Callable[..., worker_transfer.LMCacheDrivenTransferContext]


class _FakeEventBackend:
    """Event backend with opaque events and no device work."""

    device_type = "fake"

    def check_event_support(self, device: object) -> None:
        return None

    def create_event(self, device: object) -> object:
        return object()

    def export_event(self, event: object, device: object) -> bytes:
        return b"completion-handle"

    def import_event(self, handle: bytes, device: object) -> object:
        return ("remote", handle)

    def record_event(self, event: object, stream: object) -> None:
        return None

    def wait_event(self, event: object, stream: object) -> None:
        return None

    def query_event(self, event: object) -> bool:
        return True

    def synchronize_event(self, event: object, device: object) -> None:
        return None


def _resolved(result: object) -> MessagingFuture[object]:
    """Return a messaging future already resolved to ``result``."""
    future: MessagingFuture[object] = MessagingFuture()
    future.set_result(result)
    return future


def _segment_exists(instance_id: int) -> bool:
    """Report whether the worker's segment name currently exists."""
    try:
        attach_layer_progress_shm(instance_id).close()
    except FileNotFoundError:
        return False
    return True


def _write_failed_retrieve(instance_id: int, generation: int) -> None:
    """Publish a failed retrieve into the segment, as the daemon would."""
    segment = attach_layer_progress_shm(instance_id)
    try:
        record = LayerProgressRecord(segment.buf)
        record.begin_retrieve(generation)
        record.mark_retrieve_failed()
        del record
    finally:
        segment.close()


def _read_record(instance_id: int) -> tuple[int, int, bool]:
    """Return ``(generation, watermark, failed)`` from the worker's segment."""
    segment = attach_layer_progress_shm(instance_id)
    try:
        snapshot = LayerProgressRecord(segment.buf).read()
        return snapshot.generation, snapshot.watermark, snapshot.retrieve_failed
    finally:
        segment.close()


def _run_child(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter with its own resource tracker."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


@pytest.fixture
def instance_id() -> Iterator[int]:
    """A worker instance id whose segment name no other test uses."""
    iid = random.randint(10**8, 10**9)
    yield iid
    try:
        leftover = shared_memory.SharedMemory(name=layer_progress_shm_name(iid))
    except FileNotFoundError:
        return
    leftover.close()
    leftover.unlink()


@pytest.fixture
def make_context(monkeypatch: pytest.MonkeyPatch) -> _ContextFactory:
    """Build and register a layerwise worker context over real shared memory.

    Returns a callable ``(instance_id, server_use_layerwise=True)`` that yields
    a registered :class:`LMCacheDrivenTransferContext`.
    """
    monkeypatch.setattr(
        worker_transfer, "get_event_ipc_backend", lambda device: _FakeEventBackend()
    )
    monkeypatch.setattr(
        worker_transfer, "wrap_kv_caches", lambda kv_caches: list(kv_caches.values())
    )

    def build(
        iid: int, server_use_layerwise: bool = True
    ) -> worker_transfer.LMCacheDrivenTransferContext:
        client = MagicMock()
        client.register_kv_cache.return_value = _resolved(
            RegisterKvCacheResponse(
                server_use_layerwise=server_use_layerwise,
                layer_event_ipc_handles=[b"handle"] * len(_LAYERS),
            )
        )
        client.retrieve.return_value = MessagingFuture()
        context = worker_transfer.LMCacheDrivenTransferContext(iid, client)
        context.register(
            {"layer_0": torch.empty(1)},
            "model",
            1,
            1,
            1.0,
            engine_group_infos=[SimpleNamespace(layer_indices=_LAYERS)],
            use_layerwise=True,
            layerwise_wait_timeout_seconds=1.0,
        )
        return context

    return build


# ---------------------------------------------------------------------------
# Worker-side teardown
# ---------------------------------------------------------------------------


def test_close_removes_the_segment_after_a_failed_load(
    instance_id: int, make_context: _ContextFactory
) -> None:
    """A failed load, then teardown: the segment goes, and waits stop cleanly."""
    context = make_context(instance_id)
    assert _segment_exists(instance_id)

    context.submit_retrieve("request", "key", {}, [[0]], object(), 1)
    _write_failed_retrieve(instance_id, generation=1)
    with pytest.raises(LayerProgressRetrieveFailedError):
        context.wait_for_layer_load(0)

    context.close()

    assert not _segment_exists(instance_id)
    # After teardown a wait is a no-op, not a read of a released buffer.
    context.wait_for_layer_load(0)


def test_close_tolerates_a_segment_already_removed(
    instance_id: int, make_context: _ContextFactory
) -> None:
    """Another process removing the name first must not break teardown.

    The child interpreter attaches the plain way and exits, which is exactly
    what an LMCache daemon did on exit before ``attach_layer_progress_shm``.
    """
    context = make_context(instance_id)
    name = layer_progress_shm_name(instance_id)
    _run_child(
        "from multiprocessing import shared_memory\n"
        f"shared_memory.SharedMemory(name={name!r}).close()\n"
    )
    assert not _segment_exists(instance_id)

    context.close()

    context.wait_for_layer_load(0)


def test_close_twice_is_harmless(
    instance_id: int, make_context: _ContextFactory
) -> None:
    """Error paths can call close() again without tracking whether they did."""
    context = make_context(instance_id)
    context.close()
    context.close()

    assert not _segment_exists(instance_id)


def test_re_registering_after_teardown_starts_with_a_clean_record(
    instance_id: int, make_context: _ContextFactory
) -> None:
    """A failed retrieve's flag does not survive into the next registration."""
    first = make_context(instance_id)
    _write_failed_retrieve(instance_id, generation=4)
    first.close()

    second = make_context(instance_id)
    try:
        assert _read_record(instance_id) == (0, 0, False)
    finally:
        second.close()


def test_a_stale_segment_from_a_crashed_worker_is_replaced(
    instance_id: int, make_context: _ContextFactory
) -> None:
    """A segment left behind by a dead worker is discarded, flag and all."""
    stale = shared_memory.SharedMemory(
        create=True,
        size=LayerProgressRecord.RECORD_SIZE,
        name=layer_progress_shm_name(instance_id),
    )
    record = LayerProgressRecord(stale.buf)
    record.begin_retrieve(9)
    record.mark_retrieve_failed()
    del record
    stale.close()

    context = make_context(instance_id)
    try:
        assert _read_record(instance_id) == (0, 0, False)
    finally:
        context.close()


def test_a_record_cannot_be_attached_to_a_closed_segment(instance_id: int) -> None:
    """A closed segment has no buffer; attaching must fail loudly."""
    segment = shared_memory.SharedMemory(
        create=True,
        size=LayerProgressRecord.RECORD_SIZE,
        name=layer_progress_shm_name(instance_id),
    )
    segment.close()
    try:
        with pytest.raises(ValueError, match="is closed"):
            LayerProgressRecord.from_shared_memory(segment)
    finally:
        segment.unlink()


def test_a_failed_register_removes_the_segment(
    instance_id: int, make_context: _ContextFactory
) -> None:
    """A registration that fails after creating the segment must not leak it."""
    with pytest.raises(ValueError, match="layerwise config mismatch"):
        make_context(instance_id, server_use_layerwise=False)
    assert not _segment_exists(instance_id)


# ---------------------------------------------------------------------------
# Daemon-side attach
# ---------------------------------------------------------------------------


def test_a_plain_attach_removes_the_segment_when_the_attacher_exits(
    instance_id: int,
) -> None:
    """The hazard: CPython's resource tracker deletes attached segments on exit.

    Kept as evidence that ``attach_layer_progress_shm`` is necessary on this
    interpreter, not as a behaviour anyone relies on.
    """
    owner = shared_memory.SharedMemory(
        create=True,
        size=LayerProgressRecord.RECORD_SIZE,
        name=layer_progress_shm_name(instance_id),
    )
    try:
        _run_child(
            "from multiprocessing import shared_memory\n"
            f"shared_memory.SharedMemory(name={owner.name!r}).close()\n"
        )
        assert not _segment_exists(instance_id)
    finally:
        owner.close()


def test_attaching_through_the_helper_leaves_the_segment_in_place(
    instance_id: int,
) -> None:
    """The daemon exiting must not delete a live worker's segment."""
    owner = shared_memory.SharedMemory(
        create=True,
        size=LayerProgressRecord.RECORD_SIZE,
        name=layer_progress_shm_name(instance_id),
    )
    try:
        result = _run_child(
            "from lmcache.v1.multiprocess.layer_progress import "
            "attach_layer_progress_shm\n"
            f"attach_layer_progress_shm({instance_id}).close()\n"
        )
        assert _segment_exists(instance_id)
        assert "leaked shared_memory" not in result.stderr
    finally:
        owner.close()
        owner.unlink()
