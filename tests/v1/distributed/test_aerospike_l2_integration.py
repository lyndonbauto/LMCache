# SPDX-License-Identifier: Apache-2.0
"""
Integration tests for the Aerospike L2 adapter (native connector).

Requires Aerospike CE and BUILD_AEROSPIKE=1 extension. Skipped otherwise.
"""

# Standard
import os
import select
import time
import uuid

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd

_EMPTY_LAYOUT = MemoryLayoutDesc(shapes=[], dtypes=[])

AEROSPIKE_HOST = os.environ.get("AEROSPIKE_TEST_HOST", "127.0.0.1")
AEROSPIKE_PORT = int(os.environ.get("AEROSPIKE_TEST_PORT", "3000"))
AEROSPIKE_NAMESPACE = os.environ.get("AEROSPIKE_TEST_NAMESPACE", "lmcache")
RUN_AEROSPIKE_IT = os.environ.get("RUN_AEROSPIKE_INTEGRATION") == "1"


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


requires_aerospike = pytest.mark.skipif(
    not _aerospike_available(),
    reason=(
        f"Aerospike not available at {AEROSPIKE_HOST}:{AEROSPIKE_PORT} "
        "(set RUN_AEROSPIKE_INTEGRATION=1)"
    ),
)
requires_native = pytest.mark.skipif(
    not _native_extension_available(),
    reason="lmcache.lmcache_aerospike extension not built",
)


def _wait_fd(fd: int, timeout: float = 30.0) -> None:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    events = poller.poll(timeout * 1000)
    assert events, "timed out waiting for eventfd"
    try:
        consume_fd(fd)
    except BlockingIOError:
        pass


def _make_tensor_obj(size: int, fill: float) -> TensorMemoryObj:
    raw_data = torch.empty(size, dtype=torch.float32)
    raw_data.fill_(fill)
    metadata = MemoryObjMetadata(
        shape=torch.Size([size]),
        dtype=torch.float32,
        address=0,
        phy_size=size * 4,
        fmt=MemoryFormat.KV_2LTD,
        ref_count=1,
    )
    return TensorMemoryObj(raw_data, metadata, parent_allocator=None)


def _object_key(suffix: int) -> ObjectKey:
    return ObjectKey(
        chunk_hash=ObjectKey.IntHash2Bytes(suffix),
        model_name="aerospike-it",
        kv_rank=0,
    )


def _namespace_info(command: str) -> str:
    # Third Party
    import aerospike

    client = aerospike.client({"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}).connect()
    try:
        return client.info_random_node(command).split("\t")[-1].strip()
    finally:
        client.close()


def _namespace_stat(name: str) -> int:
    fields = dict(
        field.split("=", 1)
        for field in _namespace_info(f"namespace/{AEROSPIKE_NAMESPACE}").split(";")
        if "=" in field
    )
    return int(fields[name])


def _set_read_touch_pct(pct: int) -> bool:
    reply = _namespace_info(
        f"set-config:context=namespace;id={AEROSPIKE_NAMESPACE};"
        f"default-read-touch-ttl-pct={pct}"
    )
    return reply.lower().startswith("ok")


def _adapter_config(num_workers: int = 2):
    # First Party
    from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
        AerospikeL2AdapterConfig,
    )

    return AerospikeL2AdapterConfig(
        hosts=f"{AEROSPIKE_HOST}:{AEROSPIKE_PORT}",
        namespace=AEROSPIKE_NAMESPACE,
        set_name="kv_chunks_aerospike_it",
        num_workers=num_workers,
    )


@requires_aerospike
@requires_native
class TestAerospikeL2Integration:
    def test_store_lookup_load_roundtrip(self):
        adapter = create_l2_adapter_from_registry(_adapter_config())
        try:
            key = _object_key(9001)
            store_obj = _make_tensor_obj(64, 42.0)
            load_obj = _make_tensor_obj(64, 0.0)

            tid = adapter.submit_store_task([key], [store_obj])
            _wait_fd(adapter.get_store_event_fd())
            done = adapter.pop_completed_store_tasks()
            assert done[tid].is_successful()

            lookup_tid = adapter.submit_lookup_and_lock_task([key], {0: _EMPTY_LAYOUT})
            _wait_fd(adapter.get_lookup_and_lock_event_fd())
            lookup_bm = adapter.query_lookup_and_lock_result(lookup_tid)
            assert lookup_bm is not None
            assert lookup_bm.test(0)

            load_tid = adapter.submit_load_task([key], [load_obj])
            _wait_fd(adapter.get_load_event_fd())
            load_bm = adapter.query_load_result(load_tid)
            assert load_bm is not None
            assert load_bm.test(0)
            assert torch.all(load_obj.tensor == 42.0)

            adapter.submit_unlock([key])
        finally:
            adapter.close()

    def test_lookup_of_many_keys_reports_each_key_in_request_order(self):
        """T-LKP-01/02: a 10,000-key lookup, with one worker so the single
        tile exceeds one batch call, reports exactly the stored keys."""
        adapter = create_l2_adapter_from_registry(_adapter_config(num_workers=1))
        run = uuid.uuid4().hex
        keys = [
            ObjectKey(
                chunk_hash=ObjectKey.IntHash2Bytes(i),
                model_name=f"aerospike-it-lookup-{run}",
                kv_rank=0,
            )
            for i in range(10_000)
        ]
        stored_indices = list(range(0, len(keys), 3))
        # Past the 1 MiB record cap, so it is a meta record plus segments.
        sharded_index = 1
        try:
            stored = [keys[i] for i in stored_indices]
            objs = [_make_tensor_obj(64, 1.0) for _ in stored]
            stored.append(keys[sharded_index])
            objs.append(_make_tensor_obj(768 * 1024, 2.0))
            tid = adapter.submit_store_task(stored, objs)
            _wait_fd(adapter.get_store_event_fd(), timeout=120.0)
            done = adapter.pop_completed_store_tasks()
            assert done[tid].is_successful()

            lookup_tid = adapter.submit_lookup_and_lock_task(keys, {0: _EMPTY_LAYOUT})
            _wait_fd(adapter.get_lookup_and_lock_event_fd())
            found = adapter.query_lookup_and_lock_result(lookup_tid)
            assert found is not None

            expected = set(stored_indices) | {sharded_index}
            wrong = [i for i in range(len(keys)) if found.test(i) != (i in expected)]
            assert wrong == [], f"{len(wrong)} keys misreported, first {wrong[:5]}"

            adapter.submit_unlock(stored)
        finally:
            adapter.close()

    def test_read_touch_extends_every_record_a_load_reads_and_none_on_lookup(
        self,
    ):
        """With default-read-touch-ttl-pct set, a lookup never touches, and a
        whole load touches every record it reads: the meta record and each
        segment, so an object's records stay alive together."""
        try:
            previous_pct = _namespace_stat("default-read-touch-ttl-pct")
        except KeyError:
            pytest.skip("server has no default-read-touch-ttl-pct (needs 7.1+)")
        # 100: any read that may touch does, so touches are countable.
        if not _set_read_touch_pct(100):
            pytest.skip("server refused default-read-touch-ttl-pct")
        adapter = create_l2_adapter_from_registry(_adapter_config())
        try:
            run = uuid.uuid4().int & 0xFFFFFFFF
            inline_key, sharded_key = _object_key(run), _object_key(run + 1)
            keys = [inline_key, sharded_key]
            store_objs = [
                _make_tensor_obj(64, 1.0),
                # Past the 1 MiB record cap: a meta record plus segments.
                _make_tensor_obj(768 * 1024, 2.0),
            ]
            tid = adapter.submit_store_task(keys, store_objs)
            _wait_fd(adapter.get_store_event_fd())
            assert adapter.pop_completed_store_tasks()[tid].is_successful()
            # The server's touch threshold is in whole seconds, so a read
            # within a record's first second may not touch it even at 100.
            time.sleep(2.0)

            touched_before = _namespace_stat("read_touch_success")
            lookup_tid = adapter.submit_lookup_and_lock_task(keys, {0: _EMPTY_LAYOUT})
            _wait_fd(adapter.get_lookup_and_lock_event_fd())
            found = adapter.query_lookup_and_lock_result(lookup_tid)
            assert found is not None and found.test(0) and found.test(1)
            time.sleep(2.0)
            assert _namespace_stat("read_touch_success") == touched_before

            reads_before = _namespace_stat("client_read_success")
            load_objs = [
                _make_tensor_obj(64, 0.0),
                _make_tensor_obj(768 * 1024, 0.0),
            ]
            load_tid = adapter.submit_load_task(keys, load_objs)
            _wait_fd(adapter.get_load_event_fd())
            loaded = adapter.query_load_result(load_tid)
            assert loaded is not None and loaded.test(0) and loaded.test(1)
            records_read = _namespace_stat("client_read_success") - reads_before
            # The inline object is one record; the sharded one is its meta
            # record plus at least one segment.
            assert records_read >= 3

            # Touches are applied asynchronously after the read replies.
            deadline = time.monotonic() + 10.0
            touched = 0
            while time.monotonic() < deadline:
                touched = _namespace_stat("read_touch_success") - touched_before
                if touched >= records_read:
                    break
                time.sleep(0.2)
            time.sleep(1.0)
            touched = _namespace_stat("read_touch_success") - touched_before
            assert touched == records_read

            adapter.submit_unlock(keys)
        finally:
            adapter.close()
            _set_read_touch_pct(previous_pct)
