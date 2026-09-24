# SPDX-License-Identifier: Apache-2.0
"""Record layouts against a real Aerospike server.

The unit and parity tests prove the writer's arithmetic and the planner's
agree. These prove the records a real server ends up holding are the ones
the planner names: every slot of a planned fetch, looked up by the digest
the Aerospike client computes, must hold exactly that slot's bytes.

Requires Aerospike CE and the BUILD_AEROSPIKE=1 extension; skipped otherwise.
"""

# Standard
from collections.abc import Iterator
import os
import select
import uuid

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.layerwise import (
    ChunkPlacement,
    FetchPlanner,
    ModelLayout,
    PlanRequest,
    RecordKeyDigests,
)
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

#: The connector's record cap: the namespace's max-record-size (1 MiB in
#: tests/aerospike_ce.conf.template) less its 64 KiB safety margin.
MAX_RECORD_BYTES = 1024 * 1024 - 64 * 1024

MODEL = "record-layout-it"

#: A hybrid object group: 2048-byte attention planes beside 1228800-byte
#: planes, which the record cap splits into two 614400-byte pieces.
HYBRID = MemoryLayoutDesc(
    shapes=[torch.Size([2, 2, 16, 32]), torch.Size([2, 1, 300, 1024])],
    dtypes=[torch.float32, torch.float32],
)
HYBRID_BYTES = (4 * 2048) + (2 * 1_228_800)

#: A uniform object group: eight 2048-byte planes.
UNIFORM = MemoryLayoutDesc(shapes=[torch.Size([2, 4, 16, 32])], dtypes=[torch.float32])


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
        from lmcache.lmcache_aerospike import LMCacheAerospikeClient

        return hasattr(LMCacheAerospikeClient, "set_record_layouts")
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
        reason="lmcache.lmcache_aerospike extension not built with record layouts",
    ),
]


def _wait_fd(fd: int, timeout: float = 30.0) -> None:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(timeout * 1000), "timed out waiting for eventfd"
    try:
        consume_fd(fd)
    except BlockingIOError:
        pass


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


def _payload(num_bytes: int) -> torch.Tensor:
    """A payload in which every 4-byte word differs, so misplacement shows."""
    return torch.arange(num_bytes // 4, dtype=torch.int32).view(torch.float32)


def _cache_key(key: ObjectKey) -> str:
    """The connector's key for an unsalted object, per its documented format:
    ``<model>@<kv_rank:08x>@<object_group_id:x>@<chunk_hash_hex>``."""
    return (
        f"{key.model_name}@{key.kv_rank:08x}@{key.object_group_id:x}"
        f"@{key.chunk_hash.hex()}"
    )


@pytest.fixture
def set_name() -> str:
    """A fresh set per test, so no test reads another's records."""
    return f"layouts_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def adapter(set_name: str) -> Iterator[L2AdapterInterface]:
    # First Party
    from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
        AerospikeL2AdapterConfig,
    )

    built = create_l2_adapter_from_registry(
        AerospikeL2AdapterConfig(
            hosts=f"{AEROSPIKE_HOST}:{AEROSPIKE_PORT}",
            namespace=AEROSPIKE_NAMESPACE,
            set_name=set_name,
            num_workers=2,
        )
    )
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def inspector() -> Iterator[object]:
    """A plain Aerospike client, for reading records the way the server has them."""
    # Third Party
    import aerospike

    client = aerospike.client({"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}).connect()
    try:
        yield client
    finally:
        client.close()


def _store(adapter: L2AdapterInterface, key: ObjectKey, values: torch.Tensor) -> None:
    task = adapter.submit_store_task([key], [_tensor_obj(values)])
    _wait_fd(adapter.get_store_event_fd())
    assert adapter.pop_completed_store_tasks()[task].is_successful()


def _load(adapter: L2AdapterInterface, key: ObjectKey, like: torch.Tensor) -> bool:
    """Load ``key`` and return whether it succeeded and matched ``like``."""
    target = torch.zeros_like(like)
    task = adapter.submit_load_task([key], [_tensor_obj(target)])
    _wait_fd(adapter.get_load_event_fd())
    bitmap = adapter.query_load_result(task)
    return bitmap is not None and bitmap.test(0) and torch.equal(target, like)


def _meta(inspector: object, set_name: str, key: ObjectKey) -> dict[str, object]:
    _, _, bins = inspector.get(  # type: ignore[attr-defined]
        (AEROSPIKE_NAMESPACE, set_name, f"{_cache_key(key)}|m")
    )
    return bins


def _record_sizes(
    inspector: object, set_name: str, key: ObjectKey, count: int
) -> list[int]:
    sizes = []
    for index in range(count):
        _, _, bins = inspector.get(  # type: ignore[attr-defined]
            (AEROSPIKE_NAMESPACE, set_name, f"{_cache_key(key)}|s|{index}")
        )
        sizes.append(len(bins["b"]))
    return sizes


def test_a_hybrid_object_is_stored_one_plane_piece_per_record(
    adapter: L2AdapterInterface, inspector: object, set_name: str
) -> None:
    """Records follow each kernel group's own planes, and read back intact."""
    adapter.set_object_group_layouts({0: HYBRID})
    key = ObjectKey(ObjectKey.IntHash2Bytes(1), MODEL, 0)
    values = _payload(HYBRID_BYTES)
    _store(adapter, key, values)

    meta = _meta(inspector, set_name, key)
    assert meta["runs"] == "2048:2048:4,1228800:614400:2"
    assert meta["nseg"] == 8
    assert meta["plane_b"] == 0
    assert _record_sizes(inspector, set_name, key, 8) == [2048] * 4 + [614400] * 4
    assert _load(adapter, key, values)


def test_every_planned_slot_names_a_record_holding_exactly_its_bytes(
    adapter: L2AdapterInterface, inspector: object, set_name: str
) -> None:
    """End to end: planner slot -> record key -> client digest -> stored bytes.

    This is the whole join a layerwise fetch depends on. A slot that named a
    real record holding other bytes would load a plausible, wrong tensor.
    """
    adapter.set_object_group_layouts({0: HYBRID})
    key = ObjectKey(ObjectKey.IntHash2Bytes(2), MODEL, 0)
    values = _payload(HYBRID_BYTES)
    _store(adapter, key, values)
    payload = values.view(torch.uint8)

    # Third Party
    import aerospike

    record_of_digest: dict[bytes, str] = {}

    def digest_of(user_key: str) -> bytes:
        digest = bytes(aerospike.calc_digest(AEROSPIKE_NAMESPACE, set_name, user_key))
        record_of_digest[digest] = user_key
        return digest

    layout = ModelLayout.from_registration({0: HYBRID})
    plan = FetchPlanner(layout).plan(
        PlanRequest(
            placements=(ChunkPlacement(0, 0, 0, 0),),
            node_names=("node",),
            max_record_bytes=MAX_RECORD_BYTES,
        ),
        RecordKeyDigests(
            layout, MAX_RECORD_BYTES, {(0, 0): _cache_key(key)}, digest_of
        ),
    )

    assert len(plan.slots) == 8
    assert len(record_of_digest) == 8, "two slots named the same record"
    for slot in plan.slots:
        _, _, bins = inspector.get(  # type: ignore[attr-defined]
            (AEROSPIKE_NAMESPACE, set_name, None, bytearray(slot.digest))
        )
        expected = payload[slot.offset : slot.offset + slot.length]
        assert bytes(bins["b"]) == expected.numpy().tobytes(), (
            f"layer {slot.layer_id} plane {slot.plane} piece {slot.piece}: "
            f"{record_of_digest[slot.digest]} holds other bytes"
        )


def test_a_uniform_object_keeps_the_meta_record_older_readers_understand(
    adapter: L2AdapterInterface, inspector: object, set_name: str
) -> None:
    """No runs bin, and the three-number plan it always had."""
    adapter.set_object_group_layouts({0: UNIFORM})
    key = ObjectKey(ObjectKey.IntHash2Bytes(3), MODEL, 0)
    values = _payload(8 * 2048)
    _store(adapter, key, values)

    meta = _meta(inspector, set_name, key)
    assert "runs" not in meta
    assert (meta["nseg"], meta["seg_b"], meta["plane_b"]) == (8, 2048, 2048)
    assert _record_sizes(inspector, set_name, key, 8) == [2048] * 8
    assert _load(adapter, key, values)


def test_an_ambiguous_payload_size_is_byte_count_sharded(
    adapter: L2AdapterInterface, inspector: object, set_name: str
) -> None:
    """Two layouts of one size: the writer must not pick either."""
    same_size_other_layout = MemoryLayoutDesc(
        shapes=[torch.Size([1, 1, 64, 64])], dtypes=[torch.float32]
    )
    adapter.set_object_group_layouts({0: UNIFORM, 1: same_size_other_layout})
    key = ObjectKey(ObjectKey.IntHash2Bytes(4), MODEL, 0)
    values = _payload(8 * 2048)
    _store(adapter, key, values)

    meta = _meta(inspector, set_name, key)
    assert "runs" not in meta
    assert meta["plane_b"] == 0
    assert meta["nseg"] == 1
    assert _load(adapter, key, values)


def test_a_corrupt_runs_bin_fails_the_read(
    adapter: L2AdapterInterface, inspector: object, set_name: str
) -> None:
    """Runs that disagree with the object must fail, not produce ranges."""
    adapter.set_object_group_layouts({0: HYBRID})
    key = ObjectKey(ObjectKey.IntHash2Bytes(5), MODEL, 0)
    values = _payload(HYBRID_BYTES)
    _store(adapter, key, values)
    assert _load(adapter, key, values)

    inspector.put(  # type: ignore[attr-defined]
        (AEROSPIKE_NAMESPACE, set_name, f"{_cache_key(key)}|m"),
        {"runs": "2048:2048:4,1228800:614400:3"},
    )
    assert not _load(adapter, key, values)
