# SPDX-License-Identifier: Apache-2.0
"""Storage integrity of the Aerospike adapter against a real server.

An object larger than one record is stored as segment records followed by a
meta record, and the meta record alone says the object exists. These tests
damage what a real server holds -- the meta record missing, a segment
missing, the meta record wrong, keys that differ in one field -- and check
that the adapter and the storage manager above it report a miss or a failed
read, never a present-but-wrong object.

Functional test plan IDs: T-STO-03 (= T-FLT-09), T-STO-04 (= T-EVT-02's
mechanism), T-STO-05, T-STO-07 and T-LKP-06.

Requires Aerospike CE and the BUILD_AEROSPIKE=1 extension; skipped otherwise::

    RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 \\
    AEROSPIKE_TEST_PORT=3000 AEROSPIKE_TEST_NAMESPACE=lmcache \\
    pytest tests/v1/distributed/test_aerospike_storage_integrity_integration.py
"""

# Standard
from collections.abc import Iterator
from pathlib import Path
import os
import random
import select
import signal
import subprocess
import sys
import time
import uuid

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey, PrefetchRequestSpec
from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd

AEROSPIKE_HOST = os.environ.get("AEROSPIKE_TEST_HOST", "127.0.0.1")
AEROSPIKE_PORT = int(os.environ.get("AEROSPIKE_TEST_PORT", "3000"))
AEROSPIKE_NAMESPACE = os.environ.get("AEROSPIKE_TEST_NAMESPACE", "lmcache")
RUN_AEROSPIKE_IT = os.environ.get("RUN_AEROSPIKE_INTEGRATION") == "1"

MODEL = "storage-integrity-it"
#: Past the 1 MiB record cap (less the connector's 64 KiB margin): a meta
#: record plus four segments.
SHARDED_BYTES = 3 * 1024 * 1024
#: Under the record cap: one meta record holding the payload inline.
INLINE_BYTES = 64 * 1024
#: Nine segments, so a writer killed at a random moment is most likely
#: between its first segment and its meta record.
KILL_OBJECT_BYTES = 8 * 1024 * 1024
TIMEOUT = 30.0
_EMPTY_LAYOUT = MemoryLayoutDesc(shapes=[], dtypes=[])

#: Stores objects 0, 1, 2, ... one at a time and appends each finished index
#: to a file, so the parent knows which stores completed before the kill.
_WRITER = r"""
import sys
import torch
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.memory_management import (
    MemoryFormat, MemoryObjMetadata, TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd
import select

hosts, namespace, set_name, model, num_bytes, progress = sys.argv[1:7]
num_bytes = int(num_bytes)
adapter = create_l2_adapter_from_registry(
    AerospikeL2AdapterConfig(
        hosts=hosts, namespace=namespace, set_name=set_name, num_workers=1
    )
)
out = open(progress, "a", buffering=1)
out.write("ready\n")
poller = select.poll()
poller.register(adapter.get_store_event_fd(), select.POLLIN)
index = 0
while True:
    words = torch.arange(num_bytes // 4, dtype=torch.int32) + index * (1 << 20)
    values = words.view(torch.float32)
    obj = TensorMemoryObj(
        values,
        MemoryObjMetadata(
            shape=values.shape, dtype=values.dtype, address=0,
            phy_size=num_bytes, fmt=MemoryFormat.KV_2LTD, ref_count=1,
        ),
        parent_allocator=None,
    )
    key = ObjectKey(ObjectKey.IntHash2Bytes(index), model, 0)
    task = adapter.submit_store_task([key], [obj])
    while task not in (done := adapter.pop_completed_store_tasks()):
        poller.poll(1000)
        try:
            consume_fd(adapter.get_store_event_fd())
        except BlockingIOError:
            pass
    if not done[task].is_successful():
        out.write(f"failed {index}\n")
        break
    out.write(f"stored {index}\n")
    index += 1
"""


def _aerospike_available() -> bool:
    if not RUN_AEROSPIKE_IT:
        return False
    try:
        # Third Party
        import aerospike

        client = aerospike.client(
            {"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}
        ).connect()
        info = client.info_random_node(f"namespace/{AEROSPIKE_NAMESPACE}")
        client.close()
        return "nsup-period" in info
    except Exception:
        return False


def _native_extension_available() -> bool:
    try:
        # First Party
        from lmcache.lmcache_aerospike import LMCacheAerospikeClient  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _aerospike_available(),
        reason=(
            f"Aerospike not available at {AEROSPIKE_HOST}:{AEROSPIKE_PORT} "
            "(set RUN_AEROSPIKE_INTEGRATION=1)"
        ),
    ),
    pytest.mark.skipif(
        not _native_extension_available(),
        reason="lmcache.lmcache_aerospike extension not built",
    ),
]


def _wait_fd(fd: int, timeout: float = TIMEOUT) -> None:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(timeout * 1000), "timed out waiting for eventfd"
    try:
        consume_fd(fd)
    except BlockingIOError:
        pass


def _payload(index: int, num_bytes: int) -> torch.Tensor:
    """Words that differ within and across objects, so misplacement shows."""
    words = torch.arange(num_bytes // 4, dtype=torch.int32) + index * (1 << 20)
    return words.view(torch.float32)


def _tensor_obj(values: torch.Tensor) -> TensorMemoryObj:
    metadata = MemoryObjMetadata(
        shape=values.shape,
        dtype=values.dtype,
        address=0,
        phy_size=values.numel() * values.element_size(),
        fmt=MemoryFormat.KV_2LTD,
        ref_count=1,
    )
    return TensorMemoryObj(values, metadata, parent_allocator=None)


def _key(index: int, object_group_id: int = 0) -> ObjectKey:
    return ObjectKey(
        ObjectKey.IntHash2Bytes(index), MODEL, 0, object_group_id=object_group_id
    )


def _adapter_config(set_name: str, default_ttl_seconds: int = 86400):
    # First Party
    from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
        AerospikeL2AdapterConfig,
    )

    return AerospikeL2AdapterConfig(
        hosts=f"{AEROSPIKE_HOST}:{AEROSPIKE_PORT}",
        namespace=AEROSPIKE_NAMESPACE,
        set_name=set_name,
        num_workers=2,
        default_ttl_seconds=default_ttl_seconds,
    )


def _store(adapter: L2AdapterInterface, key: ObjectKey, values: torch.Tensor) -> None:
    task = adapter.submit_store_task([key], [_tensor_obj(values)])
    _wait_fd(adapter.get_store_event_fd())
    assert adapter.pop_completed_store_tasks()[task].is_successful()


def _exists(adapter: L2AdapterInterface, key: ObjectKey) -> bool:
    task = adapter.submit_lookup_and_lock_task([key], {0: _EMPTY_LAYOUT})
    _wait_fd(adapter.get_lookup_and_lock_event_fd())
    found = adapter.query_lookup_and_lock_result(task)
    assert found is not None
    present = found.test(0)
    if present:
        adapter.submit_unlock([key])
    return present


def _load(adapter: L2AdapterInterface, key: ObjectKey, num_bytes: int):
    """Load ``key`` into a zeroed buffer.

    Returns:
        ``(loaded, buffer)``: whether the adapter reported the key loaded,
        and the buffer it loaded into.
    """
    target = torch.zeros(num_bytes // 4, dtype=torch.float32)
    task = adapter.submit_load_task([key], [_tensor_obj(target)])
    _wait_fd(adapter.get_load_event_fd())
    bitmap = adapter.query_load_result(task)
    return bitmap is not None and bitmap.test(0), target


def _meta_key(set_name: str, key: ObjectKey) -> tuple[str, str, str]:
    return (AEROSPIKE_NAMESPACE, set_name, f"{object_key_to_string(key)}|m")


def _segment_key(set_name: str, key: ObjectKey, index: int) -> tuple[str, str, str]:
    return (AEROSPIKE_NAMESPACE, set_name, f"{object_key_to_string(key)}|s|{index}")


def _record_exists(inspector: object, record_key: tuple[str, str, str]) -> bool:
    # Third Party
    import aerospike

    try:
        _, meta = inspector.exists(record_key)  # type: ignore[attr-defined]
    except aerospike.exception.RecordNotFound:
        return False
    return meta is not None


def _namespace_field(inspector: object, name: str) -> str:
    reply = inspector.info_random_node(  # type: ignore[attr-defined]
        f"namespace/{AEROSPIKE_NAMESPACE}"
    )
    fields = dict(
        f.split("=", 1) for f in reply.split("\t")[-1].strip().split(";") if "=" in f
    )
    return fields[name]


def _truncate(set_name: str) -> None:
    # Third Party
    import aerospike

    client = aerospike.client({"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}).connect()
    try:
        client.truncate(AEROSPIKE_NAMESPACE, set_name, 0)
    finally:
        client.close()


@pytest.fixture
def set_name() -> Iterator[str]:
    """A fresh set per test, so no test reads another's records; emptied
    afterwards, since the kill test writes until it is stopped."""
    name = f"integrity_{uuid.uuid4().hex[:12]}"
    yield name
    _truncate(name)


@pytest.fixture
def adapter(set_name: str) -> Iterator[L2AdapterInterface]:
    built = create_l2_adapter_from_registry(_adapter_config(set_name))
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def inspector() -> Iterator[object]:
    """A plain Aerospike client, for reading and damaging records directly."""
    # Third Party
    import aerospike

    client = aerospike.client({"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}).connect()
    try:
        yield client
    finally:
        client.close()


def _storage_manager(set_name: str) -> StorageManager:
    """A storage manager with an empty L1 over the test's set."""
    return StorageManager(
        StorageManagerConfig(
            l1_manager_config=L1ManagerConfig(
                memory_config=L1MemoryManagerConfig(
                    size_in_bytes=64 * 1024 * 1024,
                    use_lazy=False,
                    align_bytes=4096,
                    shm_name="",
                ),
                write_ttl_seconds=600,
            ),
            eviction_config=EvictionConfig(eviction_policy="LRU"),
            l2_adapter_config=L2AdaptersConfig([_adapter_config(set_name)]),
        )
    )


def _raw(values: torch.Tensor) -> bytes:
    return values.numpy().tobytes()


def _bytes_of(obj: object, like: torch.Tensor) -> bytes:
    """The first ``like``-sized bytes of an L1 memory object."""
    return bytes(obj.byte_array[: like.numel() * like.element_size()])  # type: ignore[attr-defined]


def _layouts(num_bytes: int) -> dict[int, MemoryLayoutDesc]:
    return {
        0: MemoryLayoutDesc(
            shapes=[torch.Size([num_bytes // 4])], dtypes=[torch.float32]
        )
    }


# T-STO-03 / T-FLT-09: the meta record is written last.


def test_segments_without_their_meta_record_are_absent_and_unreadable(
    adapter: L2AdapterInterface, inspector: object, set_name: str
) -> None:
    """The state a writer killed between its segments and its meta record
    leaves: every segment on the server, the meta record never written.
    Lookup must say absent and a load must fail."""
    key = _key(1)
    values = _payload(1, SHARDED_BYTES)
    _store(adapter, key, values)
    inspector.remove(_meta_key(set_name, key))  # type: ignore[attr-defined]
    assert _record_exists(inspector, _segment_key(set_name, key, 0))

    assert not _exists(adapter, key)
    loaded, _ = _load(adapter, key, SHARDED_BYTES)
    assert not loaded


def test_a_writer_killed_mid_store_never_leaves_an_entry_reported_present(
    inspector: object, set_name: str, tmp_path: Path
) -> None:
    """SIGKILL a real writer process, storing nine-segment objects back to
    back, as soon as one object's first segment lands on the server (after
    a random delay). Afterwards every object is either
    present and byte-identical, or absent and unreadable; and at least one
    object was caught with segments written but no meta record."""
    orphans = 0
    for attempt in range(5):
        attempt_set = f"{set_name}_{attempt}"
        progress = tmp_path / f"progress_{attempt}.txt"
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WRITER,
                f"{AEROSPIKE_HOST}:{AEROSPIKE_PORT}",
                AEROSPIKE_NAMESPACE,
                attempt_set,
                MODEL,
                str(KILL_OBJECT_BYTES),
                str(progress),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 120.0
            while "ready" not in (progress.read_text() if progress.exists() else ""):
                assert writer.poll() is None, "writer exited before storing"
                assert time.monotonic() < deadline, "writer never became ready"
                time.sleep(0.05)
            time.sleep(random.uniform(0.3, 1.0))
            # Kill as soon as a later object's first segment lands: the
            # writer is then most likely still writing that object's
            # segments, before its meta record.
            reported = [int(v) for v in progress.read_text().split() if v.isdigit()]
            target = _key(max(reported, default=-1) + 2)
            first_segment = _segment_key(attempt_set, target, 0)
            deadline = time.monotonic() + 30.0
            while not _record_exists(inspector, first_segment):
                assert writer.poll() is None, "writer exited while storing"
                assert time.monotonic() < deadline, "writer stalled"
            writer.send_signal(signal.SIGKILL)
        finally:
            writer.kill()
            writer.wait()

        lines = progress.read_text().split()
        assert "failed" not in lines, "a store failed before the kill"
        stored = {
            int(v) for k, v in zip(lines, lines[1:], strict=False) if k == "stored"
        }
        assert stored, "the writer was killed before its first store finished"

        reader = create_l2_adapter_from_registry(_adapter_config(attempt_set))
        try:
            # The object after the last one reported stored was in flight.
            for index in range(max(stored) + 3):
                key = _key(index)
                meta = _record_exists(inspector, _meta_key(attempt_set, key))
                segment = _record_exists(inspector, _segment_key(attempt_set, key, 0))
                present = _exists(reader, key)
                loaded, target = _load(reader, key, KILL_OBJECT_BYTES)
                assert present == meta, f"object {index}: lookup disagrees with meta"
                if index in stored:
                    assert present, f"object {index} was stored but is absent"
                if present:
                    assert loaded, f"object {index} is present but unreadable"
                    assert torch.equal(target, _payload(index, KILL_OBJECT_BYTES))
                else:
                    assert not loaded, f"object {index} is absent but loaded"
                if segment and not meta:
                    orphans += 1
        finally:
            reader.close()
            _truncate(attempt_set)
        if orphans:
            return
    pytest.skip("no kill landed between an object's segments and its meta record")


# T-STO-04 / T-EVT-02: a segment missing under an intact meta record.


def test_a_missing_segment_fails_the_load(
    adapter: L2AdapterInterface,
    inspector: object,
    set_name: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The meta record still says present, but the load fails and says why."""
    key = _key(2)
    values = _payload(2, SHARDED_BYTES)
    _store(adapter, key, values)
    inspector.remove(_segment_key(set_name, key, 1))  # type: ignore[attr-defined]

    assert _exists(adapter, key)
    loaded, _ = _load(adapter, key, SHARDED_BYTES)
    assert not loaded
    assert "missing segment payload" in capfd.readouterr().err


def test_the_storage_manager_treats_a_missing_segment_as_a_miss(
    adapter: L2AdapterInterface, inspector: object, set_name: str
) -> None:
    """Three objects, the middle one damaged: a prefix prefetch serves only
    the first, a sparse load serves the first and the third, and every
    object served holds its own bytes."""
    keys = [_key(10 + i) for i in range(3)]
    payloads = [_payload(10 + i, SHARDED_BYTES) for i in range(3)]
    for key, values in zip(keys, payloads, strict=True):
        _store(adapter, key, values)
    inspector.remove(_segment_key(set_name, keys[1], 2))  # type: ignore[attr-defined]

    manager = _storage_manager(set_name)
    try:
        handle = manager.submit_prefetch_task(
            PrefetchRequestSpec(keys=keys, group_layout_descs=_layouts(SHARDED_BYTES))
        )
        assert manager.wait_prefetch_status(handle, TIMEOUT)
        found = manager.query_prefetch_status(handle)
        assert found is not None
        assert [found.test(i) for i in range(3)] == [True, False, False]
        with manager.read_prefetched_results(keys[:1]) as objs:
            assert objs is not None
            assert _bytes_of(objs[0], payloads[0]) == _raw(payloads[0])

        loaded = manager.load_into_l1(keys, _layouts(SHARDED_BYTES), TIMEOUT)
        try:
            assert set(loaded) == {keys[0], keys[2]}
            for index in (0, 2):
                served = _bytes_of(loaded[keys[index]], payloads[index])
                assert served == _raw(payloads[index])
        finally:
            manager.finish_read_prefetched(list(loaded))
    finally:
        manager.close()


# T-STO-05: a corrupt meta record fails as corrupt, never as a short read.


@pytest.mark.parametrize(
    ("num_bytes", "bins", "reason"),
    [
        pytest.param(
            SHARDED_BYTES,
            {"tot_b": SHARDED_BYTES + 4096},
            "total size",
            id="sharded-total-too-large",
        ),
        pytest.param(
            SHARDED_BYTES,
            {"tot_b": SHARDED_BYTES - 4096},
            "total size",
            id="sharded-total-too-small",
        ),
        pytest.param(
            INLINE_BYTES,
            {"tot_b": INLINE_BYTES - 4096},
            "total size",
            id="inline-total-too-small",
        ),
        pytest.param(
            SHARDED_BYTES,
            {"runs": "not-a-runs-string"},
            "meta record runs",
            id="runs-garbage",
        ),
        pytest.param(
            SHARDED_BYTES,
            {"runs": "4096:4096:2"},
            "meta record runs",
            id="runs-too-short",
        ),
    ],
)
def test_a_corrupt_meta_record_fails_the_load_as_corrupt(
    adapter: L2AdapterInterface,
    inspector: object,
    set_name: str,
    capfd: pytest.CaptureFixture[str],
    num_bytes: int,
    bins: dict[str, object],
    reason: str,
) -> None:
    """The load fails, names the corrupt field, and is not a size-mismatch
    of the payload (which a short read would be)."""
    key = _key(3)
    values = _payload(3, num_bytes)
    _store(adapter, key, values)
    inspector.put(_meta_key(set_name, key), bins)  # type: ignore[attr-defined]
    capfd.readouterr()

    loaded, _ = _load(adapter, key, num_bytes)
    err = capfd.readouterr().err
    assert not loaded
    assert reason in err, err
    assert "payload size mismatch" not in err
    assert "segment read size mismatch" not in err


# T-STO-07: records carry the configured TTL.


def test_every_record_carries_the_configured_ttl(
    inspector: object, set_name: str
) -> None:
    """The meta record and every segment get ``default_ttl_seconds``."""
    ttl = 600
    adapter = create_l2_adapter_from_registry(_adapter_config(set_name, ttl))
    try:
        sharded, inline = _key(4), _key(5)
        _store(adapter, sharded, _payload(4, SHARDED_BYTES))
        _store(adapter, inline, _payload(5, INLINE_BYTES))
    finally:
        adapter.close()

    records = [_meta_key(set_name, inline), _meta_key(set_name, sharded)] + [
        _segment_key(set_name, sharded, i) for i in range(4)
    ]
    for record in records:
        _, meta = inspector.exists(record)  # type: ignore[attr-defined]
        assert ttl - 10 <= meta["ttl"] <= ttl, f"{record[2]}: ttl {meta['ttl']}"


def test_a_zero_ttl_takes_the_namespace_default(
    inspector: object, set_name: str
) -> None:
    """``default_ttl_seconds=0`` defers to the namespace's ``default-ttl``."""
    namespace_ttl = int(_namespace_field(inspector, "default-ttl"))
    if namespace_ttl == 0:
        pytest.skip("namespace default-ttl is 0 (never expire): nothing to compare")
    adapter = create_l2_adapter_from_registry(_adapter_config(set_name, 0))
    try:
        key = _key(6)
        _store(adapter, key, _payload(6, SHARDED_BYTES))
    finally:
        adapter.close()

    for record in [_meta_key(set_name, key), _segment_key(set_name, key, 0)]:
        _, meta = inspector.exists(record)  # type: ignore[attr-defined]
        assert namespace_ttl - 10 <= meta["ttl"] <= namespace_ttl


# T-LKP-06: keys differing only in object_group_id.


def test_keys_differing_only_in_object_group_are_stored_apart(
    adapter: L2AdapterInterface,
) -> None:
    """Hybrid models store one object per group for the same chunk: each is
    its own entry, and deleting one leaves the other."""
    full, sliding = _key(7, object_group_id=0), _key(7, object_group_id=1)
    full_values, sliding_values = (
        _payload(70, SHARDED_BYTES),
        _payload(71, SHARDED_BYTES),
    )
    _store(adapter, full, full_values)
    assert not _exists(adapter, sliding)
    _store(adapter, sliding, sliding_values)

    for key, values in ((full, full_values), (sliding, sliding_values)):
        loaded, target = _load(adapter, key, SHARDED_BYTES)
        assert loaded and torch.equal(target, values)

    adapter.delete([full])
    assert not _exists(adapter, full)
    loaded, target = _load(adapter, sliding, SHARDED_BYTES)
    assert loaded and torch.equal(target, sliding_values)
