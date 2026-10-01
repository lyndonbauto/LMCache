# SPDX-License-Identifier: Apache-2.0
"""Byte oracle: pipelined RDMA fetches equal plain gets for 100 real keys.

The first 100 object keys of the functional corpus's P-exact set (prompts of
exactly 1, 2, 4 and 64 chunks of 256 tokens) are derived the way the LMCache
server derives them (``TokenHasher`` then ``ipc_key_to_object_keys``), each
stored with its own payload at Llama-3.1-8B's object size (32 layers, 8 KV
heads of 128, bf16: 32 MiB per chunk). Each prompt is then fetched through
the production pipelined RDMA path into L1, and every byte that landed is
compared with a plain (non-RDMA) get of the same key and with the payload
that was stored.

Requires what ``test_aerospike_pipelined_rdma_integration.py`` requires (a
kv-sink server, an RDMA device, the RDMA-enabled extension) and about 3.2 GiB
of server memory. ``RDMA_ORACLE_CORPUS`` names a built corpus
(``functional/harness/build_corpus.py``) whose P-exact token IDs to use;
without it the prompts are synthetic token runs of the same lengths.
``RDMA_ORACLE_MODEL_NAME`` sets the model name in the keys (vLLM sends the
model path it loaded). A second test fetches the same keys in fetches of at
most 4 chunks, the most the kv-sink server sends in one command. With
``RDMA_ORACLE_STORED_SET`` naming the set vLLM stored P-exact into, a third
test reads those production records (written by LMCache from real KV
caches) both ways, without writing anything. On the Soft-RoCE VM::

    RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 \\
    AEROSPIKE_TEST_PORT=3100 AEROSPIKE_TEST_NAMESPACE=lmcache \\
    RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \\
    RDMA_ORACLE_CORPUS=corpus_llama-3.1-8b-instruct.json \\
    pytest tests/v1/distributed/test_aerospike_rdma_byte_oracle_integration.py
"""

# Standard
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import json
import os
import select
import uuid

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import (
    AttnWindowDesc,
    MemoryLayoutDesc,
    ObjectKey,
    ipc_key_to_object_keys,
)
from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.layerwise import (
    LayerArrivalSource,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerLoadSink,
    ModelLayout,
)
from lmcache.v1.layerwise.fakes import RecordingLayerLoadSink
from lmcache.v1.layerwise.pipelined_retrieve import (
    RetrieveCompletion,
    run_pipelined_retrieve,
)
from lmcache.v1.layerwise.request_fetch import (
    FetchModel,
    ObjectToPlace,
    WindowLease,
    objects_to_place,
)
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.token_hasher import TokenHasher
from lmcache.v1.platform import consume_fd

AEROSPIKE_HOST = os.environ.get("AEROSPIKE_TEST_HOST", "127.0.0.1")
AEROSPIKE_PORT = int(os.environ.get("AEROSPIKE_TEST_PORT", "3000"))
AEROSPIKE_NAMESPACE = os.environ.get("AEROSPIKE_TEST_NAMESPACE", "lmcache")
RUN_AEROSPIKE_IT = os.environ.get("RUN_AEROSPIKE_INTEGRATION") == "1"
RDMA_DEVICE = os.environ.get("RDMA_DEVICE", "")
RDMA_GID_INDEX = int(os.environ.get("RDMA_GID_INDEX", "0"))
RDMA_TRANSPORT = RdmaTransport[os.environ.get("RDMA_TRANSPORT", "RC").upper()]
ORACLE_CORPUS = os.environ.get("RDMA_ORACLE_CORPUS", "")
MODEL_NAME = os.environ.get(
    "RDMA_ORACLE_MODEL_NAME", "meta-llama/Llama-3.1-8B-Instruct"
)
#: A set holding the keys as vLLM stored them (the production-key half).
STORED_SET = os.environ.get("RDMA_ORACLE_STORED_SET", "")

CHUNK_TOKENS = 256
NUM_KEYS = 100
#: Chunks per P-exact prompt, in corpus order (``corpus_spec.json``).
P_EXACT_CHUNKS = [1] * 5 + [2] * 5 + [4] * 5 + [64] * 5
#: Llama-3.1-8B: 32 layers, 8 KV heads of 128, bf16, one kernel group.
GROUP_LAYOUTS = {
    0: MemoryLayoutDesc(
        shapes=[torch.Size([2, 32, CHUNK_TOKENS, 8 * 128])], dtypes=[torch.bfloat16]
    )
}
KERNEL_LAYERS = {0: [list(range(32))]}
ATTN = AttnWindowDesc(num_chunks_in_sw=[-1], world_size=1, group_kinds=("attention",))
OBJECT_BYTES = 2 * 32 * CHUNK_TOKENS * 8 * 128 * 2
FETCH_TIMEOUT = 60.0
STORE_TIMEOUT = 60.0


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


def _native_rdma_available() -> bool:
    try:
        # First Party
        from lmcache.lmcache_aerospike import L1RdmaRegistration
    except ImportError:
        return False
    return hasattr(L1RdmaRegistration(), "transport")


pytestmark = [
    pytest.mark.skipif(
        not RDMA_DEVICE,
        reason="no RDMA device named (set RDMA_DEVICE, and RDMA_GID_INDEX)",
    ),
    pytest.mark.skipif(
        not _aerospike_available(),
        reason=(
            f"Aerospike not available at {AEROSPIKE_HOST}:{AEROSPIKE_PORT} "
            "(set RUN_AEROSPIKE_INTEGRATION=1)"
        ),
    ),
    pytest.mark.skipif(
        not _native_rdma_available(),
        reason="lmcache.lmcache_aerospike extension not built with RDMA",
    ),
]


class _PlanTap:
    """Passes a retrieve's source calls through, keeping the plan it began."""

    def __init__(self, source: LayerArrivalSource) -> None:
        self._source = source
        self.plan: LayerFetchPlan | None = None

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        self.plan = plan
        return self._source.begin_fetch(plan)

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        return self._source.poll_layer(layer_id, generation)

    def finish_fetch(self, generation: int) -> None:
        self._source.finish_fetch(generation)

    def abandon_fetch(self, generation: int) -> None:
        self._source.abandon_fetch(generation)


class _Loader:
    """Loads layers through a recording sink; records any whole reload."""

    def __init__(self) -> None:
        self.sink = RecordingLayerLoadSink()
        self.reloaded: list[ObjectKey] = []

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        return self.sink

    def wait_for_copies(self) -> None:
        """The recording sink copies nothing."""

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        self.reloaded = [o.key for o in objects]


@dataclass(frozen=True)
class _Prompt:
    """One P-exact prompt, cut to the keys this test fetches."""

    name: str
    keys: list[ObjectKey]


def _fetch_model() -> FetchModel:
    return FetchModel(ModelLayout.from_registration(GROUP_LAYOUTS, KERNEL_LAYERS), ATTN)


def _token_runs() -> list[tuple[str, list[int]]]:
    """``(name, token_ids)`` of each P-exact prompt, in corpus order."""
    if ORACLE_CORPUS:
        with open(ORACLE_CORPUS) as f:
            corpus = json.load(f)
        return [(p["id"], list(p["token_ids"])) for p in corpus["sets"]["P-exact"]]
    return [
        (f"P-exact-{i:02d}", [(i * 7919 + t) % 120000 for t in range(n * CHUNK_TOKENS)])
        for i, n in enumerate(P_EXACT_CHUNKS)
    ]


def _prompts() -> list[_Prompt]:
    """The prompts covering the first ``NUM_KEYS`` keys; the last is cut short."""
    hasher = TokenHasher(chunk_size=CHUNK_TOKENS)
    prompts: list[_Prompt] = []
    remaining = NUM_KEYS
    for name, tokens in _token_runs():
        if remaining == 0:
            break
        end = min(len(tokens) // CHUNK_TOKENS, remaining) * CHUNK_TOKENS
        key = IPCCacheServerKey(
            model_name=MODEL_NAME,
            world_size=1,
            worker_id=0,
            token_ids=tuple(tokens),
            start=0,
            end=end,
            request_id=name,
            cache_salt="",
        )
        hashes = [
            TokenHasher.hash_to_bytes(h)
            for h in hasher.compute_chunk_hashes(tokens, end=end)
        ]
        keys = ipc_key_to_object_keys(key, hashes, [0])[0]
        prompts.append(_Prompt(name, list(keys)))
        remaining -= len(keys)
    return prompts


def _payload(index: int) -> torch.Tensor:
    """32 MiB in which every 4-byte word differs, within and across objects."""
    words = OBJECT_BYTES // 4
    return torch.arange(words, dtype=torch.int32) + index * words


def _wait_fd(fd: int, timeout: float) -> None:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(timeout * 1000), "timed out waiting for eventfd"
    try:
        consume_fd(fd)
    except BlockingIOError:
        pass


def _tensor_obj(values: torch.Tensor) -> TensorMemoryObj:
    return TensorMemoryObj(
        values,
        MemoryObjMetadata(
            shape=values.shape,
            dtype=values.dtype,
            address=0,
            phy_size=values.numel() * values.element_size(),
            fmt=MemoryFormat.KV_2LTD,
            ref_count=1,
        ),
        parent_allocator=None,
    )


def _store(adapter: L2AdapterInterface, key: ObjectKey, values: torch.Tensor) -> None:
    task = adapter.submit_store_task([key], [_tensor_obj(values)])
    _wait_fd(adapter.get_store_event_fd(), STORE_TIMEOUT)
    assert adapter.pop_completed_store_tasks()[task].is_successful()


def _plain_get(adapter: L2AdapterInterface, key: ObjectKey) -> torch.Tensor:
    """``key`` loaded by the plain (non-RDMA) path into a zeroed buffer."""
    target = torch.zeros(OBJECT_BYTES // 4, dtype=torch.int32)
    task = adapter.submit_load_task([key], [_tensor_obj(target)])
    _wait_fd(adapter.get_load_event_fd(), FETCH_TIMEOUT)
    bitmap = adapter.query_load_result(task)
    assert bitmap is not None and bitmap.test(0), f"plain get missed {key}"
    return target


def _adapter_config(set_name: str, rdma: L1RdmaConfig) -> AerospikeL2AdapterConfig:
    return AerospikeL2AdapterConfig(
        hosts=f"{AEROSPIKE_HOST}:{AEROSPIKE_PORT}",
        namespace=AEROSPIKE_NAMESPACE,
        set_name=set_name,
        num_workers=4,
        rdma=rdma,
    )


def _build_manager(set_name: str, num_chunks: int) -> StorageManager:
    """A storage manager with one RDMA window sized for ``num_chunks`` objects.

    The ``retain`` prefetch policy keeps every fetched object in L1, so the
    test can read back what the fetch landed.
    """
    window_bytes = num_chunks * OBJECT_BYTES
    rdma = L1RdmaConfig(
        transport=RDMA_TRANSPORT,
        device_name=RDMA_DEVICE,
        gid_index=RDMA_GID_INDEX,
        window_plan=RdmaWindowPlan(window_count=1, window_bytes=window_bytes),
        fetch_timeout_seconds=FETCH_TIMEOUT,
    )
    built = StorageManager(
        StorageManagerConfig(
            l1_manager_config=L1ManagerConfig(
                memory_config=L1MemoryManagerConfig(
                    size_in_bytes=window_bytes + 64 * 1024 * 1024,
                    use_lazy=False,
                    align_bytes=4096,
                    shm_name="",
                    rdma_window_count=1,
                    rdma_window_bytes=window_bytes,
                ),
                write_ttl_seconds=600,
            ),
            eviction_config=EvictionConfig(eviction_policy="LRU"),
            l2_adapter_config=L2AdaptersConfig([_adapter_config(set_name, rdma)]),
            prefetch_policy="retain",
        )
    )
    built.set_object_group_layouts(dict(GROUP_LAYOUTS), KERNEL_LAYERS)
    return built


@pytest.fixture
def set_name() -> Iterator[str]:
    """A fresh set, truncated after the test to give the server its memory."""
    name = f"oracle_{uuid.uuid4().hex[:12]}"
    yield name
    # Third Party
    import aerospike

    client = aerospike.client({"hosts": [(AEROSPIKE_HOST, AEROSPIKE_PORT)]}).connect()
    try:
        client.truncate(AEROSPIKE_NAMESPACE, name, 0)
    finally:
        client.close()


def _split(prompts: Sequence[_Prompt], max_chunks: int) -> list[_Prompt]:
    """Each prompt's keys cut, in order, into fetches of at most ``max_chunks``."""
    return [
        _Prompt(
            f"{p.name}[{i}:{i + len(p.keys[i : i + max_chunks])}]",
            p.keys[i : i + max_chunks],
        )
        for p in prompts
        for i in range(0, len(p.keys), max_chunks)
    ]


def _test_payloads(fetches: Sequence[_Prompt]) -> dict[ObjectKey, torch.Tensor]:
    """A distinct payload for every key of ``fetches``, in fetch order."""
    keys = [key for fetch in fetches for key in fetch.keys]
    return {key: _payload(index) for index, key in enumerate(keys)}


def _fetch_and_compare(
    set_name: str,
    fetches: Sequence[_Prompt],
    payloads: dict[ObjectKey, torch.Tensor],
) -> None:
    """Store ``payloads``, then fetch each fetch by pipelined RDMA and compare.

    Every byte that lands is compared with a plain get of the same key and,
    for a key in ``payloads``, with what was stored. ``payloads`` is empty
    when someone else (vLLM) stored the keys. Each fetch gets its own
    storage manager (a fresh registration), so a region the server disables
    during one fetch cannot affect the next.
    """
    keys = [key for fetch in fetches for key in fetch.keys]
    assert len(keys) == NUM_KEYS and len(set(keys)) == NUM_KEYS

    plain = create_l2_adapter_from_registry(_adapter_config(set_name, L1RdmaConfig()))
    try:
        plain.set_object_group_layouts(dict(GROUP_LAYOUTS), KERNEL_LAYERS)
        for key, values in payloads.items():
            _store(plain, key, values)

        compared = 0
        for prompt in fetches:
            manager = _build_manager(set_name, len(prompt.keys))
            try:
                assert manager.pipelined_fetch_node_name(), (
                    f"{prompt.name}: the window did not register"
                )
                tap = _PlanTap(manager.layer_arrival_source())
                loader = _Loader()
                request = [list(prompt.keys)]
                placer = manager.pipelined_window_placer(
                    GROUP_LAYOUTS, _fetch_model(), len(prompt.keys)
                )
                result = run_pipelined_retrieve(
                    _fetch_model(),
                    request,
                    manager.pipelined_max_record_bytes(),
                    placer,
                    tap,
                    loader,
                    layer_timeout_seconds=FETCH_TIMEOUT,
                    poll_interval_seconds=0.001,
                )
                assert result.completion is RetrieveCompletion.PIPELINED, (
                    f"{prompt.name} ({len(prompt.keys)} chunks): "
                    f"{result.completion.value}, "
                    f"{len(loader.reloaded)} object(s) reloaded whole"
                )
                assert tap.plan is not None
                assert loader.sink.loaded_layers() == tap.plan.layer_ids()
                sizes = {
                    o.key: o.object_bytes
                    for o in objects_to_place(_fetch_model(), request)
                }
                assert set(sizes) == set(prompt.keys)
                assert all(size == OBJECT_BYTES for size in sizes.values())
                resident = manager.lock_resident_keys(list(prompt.keys))
                try:
                    assert set(resident.locked) == set(prompt.keys)
                    for key in prompt.keys:
                        landed = torch.frombuffer(
                            bytearray(resident.locked[key].byte_array[:OBJECT_BYTES]),
                            dtype=torch.int32,
                        )
                        got = _plain_get(plain, key)
                        assert torch.equal(landed, got), (
                            f"{prompt.name} {key}: RDMA bytes differ from a plain get"
                        )
                        if key in payloads:
                            assert torch.equal(got, payloads[key]), (
                                f"{prompt.name} {key}: plain get differs from the store"
                            )
                        compared += 1
                finally:
                    manager.finish_read_prefetched(list(resident.locked))
            finally:
                manager.close()
        assert compared == NUM_KEYS
    finally:
        plain.close()


def test_pipelined_rdma_fetches_equal_plain_gets_for_100_p_exact_keys(
    set_name: str,
) -> None:
    """Every byte a pipelined fetch lands equals the plain get and the store."""
    fetches = _prompts()
    _fetch_and_compare(set_name, fetches, _test_payloads(fetches))


def test_rdma_equals_plain_gets_for_100_p_exact_keys_in_fetches_of_4_chunks(
    set_name: str,
) -> None:
    """The same 100 keys, each fetch at most 4 chunks.

    With Llama-3.1-8B's 64 slots per chunk, 4 chunks is 256 slots per fetch;
    the 64-chunk prompts' keys are fetched 4 at a time, in order. The split
    dates from the info-command protocol, where it kept each fetch to one
    server command; on sink batch reads it checks small fetches alongside
    the whole-prompt ones above.
    """
    fetches = _split(_prompts(), 4)
    _fetch_and_compare(set_name, fetches, _test_payloads(fetches))


@pytest.mark.skipif(
    not STORED_SET,
    reason="no set of vLLM-stored P-exact keys named (set RDMA_ORACLE_STORED_SET)",
)
def test_rdma_equals_plain_gets_for_100_keys_stored_by_vllm() -> None:
    """The same 100 keys as stored by vLLM through LMCache, read twice.

    Nothing is written, and the set is left as it was. Fetches are at most
    4 chunks, as above. ``RDMA_ORACLE_MODEL_NAME`` must be the model name
    vLLM sent with the keys.
    """
    _fetch_and_compare(STORED_SET, _split(_prompts(), 4), {})
