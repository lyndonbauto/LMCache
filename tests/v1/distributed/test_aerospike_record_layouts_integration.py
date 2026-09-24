# SPDX-License-Identifier: Apache-2.0
"""Record layouts against a real Aerospike server.

The unit and parity tests prove the writer's arithmetic and the planner's
agree. These prove the records a real server ends up holding are the ones
the planner names: every slot of a planned fetch, looked up by the digest
the native connector computes from the slot's record key, must hold exactly
that slot's bytes.

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
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.layerwise import (
    ChunkPlacement,
    FetchPlanner,
    ModelLayout,
    PlanRequest,
    RecordKeys,
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

        return hasattr(LMCacheAerospikeClient, "record_digest_hex")
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
def native_client(set_name: str) -> Iterator[object]:
    """The native connector on the test's set, for its key-to-digest call."""
    # First Party
    from lmcache.lmcache_aerospike import LMCacheAerospikeClient

    client = LMCacheAerospikeClient(
        f"{AEROSPIKE_HOST}:{AEROSPIKE_PORT}", AEROSPIKE_NAMESPACE, set_name, 1
    )
    try:
        yield client
    finally:
        client.close()


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
        (AEROSPIKE_NAMESPACE, set_name, f"{object_key_to_string(key)}|m")
    )
    return bins


def _record_sizes(
    inspector: object, set_name: str, key: ObjectKey, count: int
) -> list[int]:
    sizes = []
    for index in range(count):
        _, _, bins = inspector.get(  # type: ignore[attr-defined]
            (AEROSPIKE_NAMESPACE, set_name, f"{object_key_to_string(key)}|s|{index}")
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
    adapter: L2AdapterInterface,
    inspector: object,
    native_client: object,
    set_name: str,
) -> None:
    """End to end: planner slot -> record key -> native digest -> stored bytes.

    This is the whole join a layerwise fetch depends on. A slot that named a
    real record holding other bytes would load a plausible, wrong tensor.
    Records are read by the digest the *native* connector derives from each
    slot's key, which is what the pipelined fetch hands the server.
    """
    adapter.set_object_group_layouts({0: HYBRID})
    key = ObjectKey(ObjectKey.IntHash2Bytes(2), MODEL, 0)
    values = _payload(HYBRID_BYTES)
    _store(adapter, key, values)
    payload = values.view(torch.uint8)

    layout = ModelLayout.from_registration({0: HYBRID})
    plan = FetchPlanner(layout).plan(
        PlanRequest(
            placements=(ChunkPlacement(0, 0, 0, 0),),
            node_names=("node",),
            max_record_bytes=MAX_RECORD_BYTES,
        ),
        RecordKeys(layout, MAX_RECORD_BYTES, {(0, 0): object_key_to_string(key)}),
    )

    assert len(plan.slots) == 8
    assert len({slot.record_key for slot in plan.slots}) == 8, (
        "two slots named the same record"
    )
    for slot in plan.slots:
        digest = bytes.fromhex(
            native_client.record_digest_hex(slot.record_key)  # type: ignore[attr-defined]
        )
        _, _, bins = inspector.get(  # type: ignore[attr-defined]
            (AEROSPIKE_NAMESPACE, set_name, None, bytearray(digest))
        )
        expected = payload[slot.offset : slot.offset + slot.length]
        matches = bytes(bins["b"]) == expected.numpy().tobytes()
        assert matches, (
            f"layer {slot.layer_id} plane {slot.plane} piece {slot.piece}: "
            f"{slot.record_key} holds other bytes"
        )


def test_a_request_planned_from_its_cache_lookup_reads_back_its_objects(
    adapter: L2AdapterInterface,
    inspector: object,
    native_client: object,
    set_name: str,
) -> None:
    """C1 end to end: request keys -> plan -> records holding each slot's bytes.

    Objects are stored under the keys the retrieve path resolves for a
    request, the plan is built from that same lookup with the connector's
    own record cap, and every slot is read back by the digest the connector
    derives from the slot's key.
    """
    # First Party
    from lmcache.v1.distributed.api import AttnWindowDesc, ipc_key_to_object_keys
    from lmcache.v1.layerwise.request_fetch import (
        ChunkLocation,
        FetchModel,
        build_request_fetch,
    )
    from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
    from lmcache.v1.multiprocess.token_hasher import TokenHasher

    layouts = {0: HYBRID, 1: UNIFORM}
    kernel_layers = {0: [[0, 1], [2]], 1: [[3, 4, 5, 6]]}
    attn = AttnWindowDesc(num_chunks_in_sw=[-1, 1])
    adapter.set_object_group_layouts(layouts, kernel_layers)
    request = IPCCacheServerKey(
        model_name=MODEL,
        world_size=1,
        worker_id=0,
        token_ids=tuple(range(3 * 16 + 5)),
        start=0,
        end=3 * 16,
        request_id="it-request",
    )
    hasher = TokenHasher(chunk_size=16)
    chunk_hashes = [
        TokenHasher.hash_to_bytes(h)
        for h in hasher.compute_chunk_hashes(list(request.token_ids), end=request.end)
    ]
    keys = ipc_key_to_object_keys(request, chunk_hashes, [0, 1])

    sizes = {0: HYBRID_BYTES, 1: 8 * 2048}
    payloads: dict[tuple[int, int], torch.Tensor] = {}
    for group_id, group_keys in enumerate(keys):
        for chunk_id, key in enumerate(group_keys):
            words = torch.arange(sizes[group_id] // 4, dtype=torch.int32)
            values = (words + (chunk_id * 2 + group_id) * (1 << 24)).view(torch.float32)
            payloads[(chunk_id, group_id)] = values
            _store(adapter, key, values)

    class _Placer:
        def __init__(self) -> None:
            self.next_offset = 0

        def locate(
            self, chunk_id: int, object_group_id: int, object_bytes: int
        ) -> ChunkLocation:
            offset = self.next_offset
            self.next_offset += object_bytes
            return ChunkLocation(node_name="node", dest_offset=offset)

    layout = ModelLayout.from_registration(layouts, kernel_layers)
    fetch = build_request_fetch(
        FetchModel(layout, attn),
        keys,
        native_client.max_record_bytes(),  # type: ignore[attr-defined]
        _Placer(),
    )

    destination_of = {
        (p.chunk_id, p.object_group_id): p.dest_offset for p in fetch.request.placements
    }
    assert set(destination_of) == {(0, 0), (1, 0), (2, 0), (2, 1)}
    for slot in fetch.plan.slots:
        group_id = layout.object_group_of_layer(slot.layer_id)
        start = slot.offset - destination_of[(slot.chunk_id, group_id)]
        payload = payloads[(slot.chunk_id, group_id)].view(torch.uint8)
        digest = bytes.fromhex(
            native_client.record_digest_hex(slot.record_key)  # type: ignore[attr-defined]
        )
        _, _, bins = inspector.get(  # type: ignore[attr-defined]
            (AEROSPIKE_NAMESPACE, set_name, None, bytearray(digest))
        )
        matches = (
            bytes(bins["b"]) == payload[start : start + slot.length].numpy().tobytes()
        )
        assert matches, f"{slot.record_key} does not hold the bytes of {slot}"


def test_the_connector_reports_the_record_cap_it_writes_under(
    native_client: object,
) -> None:
    """The namespace's 1 MiB cap less the connector's 64 KiB margin."""
    assert native_client.max_record_bytes() == MAX_RECORD_BYTES  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "user_key",
    ["model@00000000@0@ab|m", "model@00000001@2@cd|s|17", "salted@0@0@ef@user-1|m"],
)
def test_the_native_digest_is_the_clients_digest_of_the_key(
    native_client: object, set_name: str, user_key: str
) -> None:
    """The connector names the same record the reference client would."""
    # Third Party
    import aerospike

    expected = bytes(aerospike.calc_digest(AEROSPIKE_NAMESPACE, set_name, user_key))
    actual = native_client.record_digest_hex(user_key)  # type: ignore[attr-defined]
    assert actual == expected.hex()
    assert len(actual) == 40 and actual == actual.lower()


def test_an_empty_key_has_no_digest(native_client: object) -> None:
    """An empty key names no record, so the connector refuses it."""
    with pytest.raises(ValueError, match="empty"):
        native_client.record_digest_hex("")  # type: ignore[attr-defined]


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
        (AEROSPIKE_NAMESPACE, set_name, f"{object_key_to_string(key)}|m"),
        {"runs": "2048:2048:4,1228800:614400:3"},
    )
    assert not _load(adapter, key, values)
