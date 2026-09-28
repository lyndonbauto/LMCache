# SPDX-License-Identifier: Apache-2.0
"""Tests for building a layerwise placer and reading the record cap through
the storage manager (``pipelined_window_placer``,
``pipelined_max_record_bytes``).

The L2 adapter is ``NativeConnectorL2Adapter`` over a native-client stub, and
the L1 is small CPU memory with real RDMA windows, so no RDMA device or
Aerospike server is needed.
"""

# Standard
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed import storage_manager as storage_manager_module
from lmcache.v1.distributed.api import AttnWindowDesc, MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.config import (
    L2AdapterConfigBase,
    L2AdaptersConfig,
)
from lmcache.v1.distributed.l2_adapters.mock_l2_adapter import MockL2AdapterConfig
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
)
from lmcache.v1.distributed.l2_adapters.rdma_registration import (
    L1RdmaConfig,
    RdmaTransport,
    RdmaWindowPlan,
)
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.layerwise import LayerwiseContractError
from lmcache.v1.layerwise.planner import ModelLayout
from lmcache.v1.layerwise.request_fetch import (
    ChunkPlacer,
    FetchModel,
    LeaseOutcome,
    ObjectToPlace,
)
from tests.v1.distributed.utils import PipelinedNativeClientStub

WINDOW_BYTES = 256 * 1024
WINDOW_COUNT = 2
# One object is (K/V, 1 layer, 16 tokens, 512) float32 = 64 KiB.
LAYOUT = MemoryLayoutDesc([torch.Size([2, 1, 16, 512])], [torch.float32])
OBJECT_BYTES = 64 * 1024
CHUNKS_PER_WINDOW = WINDOW_BYTES // OBJECT_BYTES


def _model() -> FetchModel:
    return FetchModel(
        ModelLayout.from_registration({0: LAYOUT}, {0: [[0]]}),
        AttnWindowDesc(num_chunks_in_sw=[-1], world_size=1, group_kinds=("attention",)),
    )


def _key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)


def _rdma_adapter_config() -> AerospikeL2AdapterConfig:
    return AerospikeL2AdapterConfig(
        hosts="stub:3000",
        rdma=L1RdmaConfig(
            transport=RdmaTransport.RC,
            window_plan=RdmaWindowPlan(
                window_count=WINDOW_COUNT, window_bytes=WINDOW_BYTES
            ),
            fetch_timeout_seconds=30.0,
        ),
    )


@contextmanager
def _storage_manager(
    adapters: list[L2AdapterConfigBase],
    window_count: int = WINDOW_COUNT,
    prefetch_policy: str = "default",
) -> Iterator[StorageManager]:
    sm = StorageManager(
        StorageManagerConfig(
            l1_manager_config=L1ManagerConfig(
                memory_config=L1MemoryManagerConfig(
                    size_in_bytes=4 << 20,
                    use_lazy=False,
                    init_size_in_bytes=4 << 20,
                    align_bytes=0x1000,
                    shm_name="",
                    rdma_window_count=window_count,
                    rdma_window_bytes=WINDOW_BYTES if window_count else 0,
                ),
                write_ttl_seconds=600,
                read_ttl_seconds=300,
            ),
            eviction_config=EvictionConfig(eviction_policy="LRU"),
            l2_adapter_config=L2AdaptersConfig(adapters=adapters),
            prefetch_policy=prefetch_policy,
        )
    )
    try:
        yield sm
    finally:
        sm.close()


@pytest.fixture
def stub_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], PipelinedNativeClientStub]:
    """Make every configured adapter a native adapter over one stub client."""
    client = PipelinedNativeClientStub()
    monkeypatch.setattr(
        storage_manager_module,
        "create_l2_adapter",
        lambda config, l1_memory_desc: NativeConnectorL2Adapter(
            native_client=client, type_name="stub"
        ),
    )
    return lambda: client


def _objects(chunks: int) -> list[ObjectToPlace]:
    return [ObjectToPlace(c, 0, _key(c), OBJECT_BYTES) for c in range(chunks)]


def test_the_placer_leases_inside_an_rdma_window(
    stub_adapters: Callable[[], PipelinedNativeClientStub],
) -> None:
    with _storage_manager([_rdma_adapter_config()]) as sm:
        placer = sm.pipelined_window_placer({0: LAYOUT}, _model(), CHUNKS_PER_WINDOW)
        assert isinstance(placer, ChunkPlacer)

        lease = placer.lease(_objects(2))
        location = lease.locate(1, 0)
        start = lease.window_start()

        assert location.node_name == PipelinedNativeClientStub.NODE_NAME
        assert start <= location.dest_offset < start + WINDOW_BYTES
        assert lease.window_bytes() == WINDOW_BYTES
        lease.release(LeaseOutcome.NEVER_FETCHED)


@pytest.mark.parametrize(("policy", "kept"), [("default", False), ("retain", True)])
def test_the_placer_follows_the_prefetch_policys_retention(
    stub_adapters: Callable[[], PipelinedNativeClientStub], policy: str, kept: bool
) -> None:
    """Under ``default`` a finished fetch frees its objects; ``retain`` keeps them."""
    with _storage_manager([_rdma_adapter_config()], prefetch_policy=policy) as sm:
        placer = sm.pipelined_window_placer({0: LAYOUT}, _model(), 1)
        placer.lease(_objects(1)).release(LeaseOutcome.FINISHED)

        resident = sm.lock_resident_keys([_key(0)])
        assert list(resident.locked) == ([_key(0)] if kept else [])
        sm.finish_read_prefetched(list(resident.locked))


def test_resident_keys_are_split_into_locked_busy_and_absent(
    stub_adapters: Callable[[], PipelinedNativeClientStub],
) -> None:
    with _storage_manager([_rdma_adapter_config()], prefetch_policy="retain") as sm:
        placer = sm.pipelined_window_placer({0: LAYOUT}, _model(), 2)
        placer.lease(_objects(1)).release(LeaseOutcome.FINISHED)
        in_flight = placer.lease([ObjectToPlace(0, 0, _key(1), OBJECT_BYTES)])

        resident = sm.lock_resident_keys([_key(2), _key(1), _key(0)])

        assert list(resident.locked) == [_key(0)]
        assert resident.busy == (_key(1),)
        assert resident.absent == (_key(2),)
        sm.finish_read_prefetched([_key(0)])
        in_flight.release(LeaseOutcome.NEVER_FETCHED)


def test_the_record_cap_comes_from_the_pipelined_adapter(
    stub_adapters: Callable[[], PipelinedNativeClientStub],
) -> None:
    with _storage_manager([_rdma_adapter_config()]) as sm:
        assert (
            sm.pipelined_max_record_bytes()
            == PipelinedNativeClientStub.MAX_RECORD_BYTES
        )


def test_without_a_pipelined_adapter_there_is_no_record_cap() -> None:
    with _storage_manager(
        [MockL2AdapterConfig(max_size_gb=0.01, mock_bandwidth_gb=10.0)],
        window_count=0,
    ) as sm:
        with pytest.raises(LayerwiseContractError, match="MockL2Adapter"):
            sm.pipelined_max_record_bytes()


def test_without_rdma_windows_there_is_no_placer(
    stub_adapters: Callable[[], PipelinedNativeClientStub],
) -> None:
    with _storage_manager(
        [MockL2AdapterConfig(max_size_gb=0.01, mock_bandwidth_gb=10.0)],
        window_count=0,
    ) as sm:
        with pytest.raises(LayerwiseContractError, match="RDMA"):
            sm.pipelined_window_placer({0: LAYOUT}, _model(), 1)


def test_a_path_that_is_not_ready_gives_no_placer(
    stub_adapters: Callable[[], PipelinedNativeClientStub],
) -> None:
    stub_adapters().init_error = "cluster has 3 nodes"
    with _storage_manager([_rdma_adapter_config()]) as sm:
        with pytest.raises(LayerwiseContractError, match="3 nodes"):
            sm.pipelined_window_placer({0: LAYOUT}, _model(), 1)


def test_a_window_too_small_for_the_chunk_cap_is_refused(
    stub_adapters: Callable[[], PipelinedNativeClientStub],
) -> None:
    with _storage_manager([_rdma_adapter_config()]) as sm:
        with pytest.raises(ValueError, match="rdma_window_bytes"):
            sm.pipelined_window_placer({0: LAYOUT}, _model(), CHUNKS_PER_WINDOW + 1)
