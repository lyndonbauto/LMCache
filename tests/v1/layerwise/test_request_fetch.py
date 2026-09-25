# SPDX-License-Identifier: Apache-2.0
"""C1: planning a layerwise fetch from a real retrieve request.

The request is shaped the way vLLM sends one: an ``IPCCacheServerKey`` with
the prompt's token ids, a worker id, and a range ending on the last full
chunk. Its object keys come from the production hasher and key expansion, so
the only stand-in is the :class:`ChunkPlacer` -- node ownership and window
offsets, which are not the request's to decide.
"""

# Standard
from collections.abc import Sequence
import re

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
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.layerwise import ModelLayout, pipelined_fetch_arguments
from lmcache.v1.layerwise.request_fetch import (
    ChunkLocation,
    FetchModel,
    FetchModelRegistry,
    RequestFetch,
    build_request_fetch,
    first_in_window_chunk,
    request_cache_keys,
)
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.token_hasher import TokenHasher

CHUNK_TOKENS = 16
NUM_CHUNKS = 5
MODEL_NAME = "google/gemma-3-hybrid"
#: Small enough that the attention planes are cut into several records.
MAX_RECORD_BYTES = 3000

#: Group 0: full attention, 2 layers. Group 1: sliding window of 2 chunks,
#: 2 layers. Group 2: a connector-private aux group, never retrieved.
GROUP_LAYOUTS = {
    0: MemoryLayoutDesc(shapes=[torch.Size([2, 2, 16, 64])], dtypes=[torch.float16]),
    1: MemoryLayoutDesc(shapes=[torch.Size([2, 2, 16, 32])], dtypes=[torch.float16]),
    2: MemoryLayoutDesc(shapes=[torch.Size([1, 1, 16, 8])], dtypes=[torch.float16]),
}
KERNEL_LAYERS = {0: [[0, 2]], 1: [[1, 3]], 2: [[4]]}
ATTN = AttnWindowDesc(
    num_chunks_in_sw=[-1, 2, -1],
    world_size=2,
    group_kinds=("attention", "attention", "aux"),
)


class PackingPlacer:
    """Packs objects back to back in the window; chunks alternate nodes."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.offsets: dict[tuple[int, int], int] = {}
        self._next_offset = 4096

    def locate(
        self, chunk_id: int, object_group_id: int, object_bytes: int
    ) -> ChunkLocation:
        """Record the call and place the object after the previous one."""
        self.calls.append((chunk_id, object_group_id, object_bytes))
        offset = self._next_offset
        self.offsets[(chunk_id, object_group_id)] = offset
        self._next_offset += object_bytes
        return ChunkLocation(node_name=f"node-{chunk_id % 2}", dest_offset=offset)


def vllm_request(cache_salt: str = "") -> IPCCacheServerKey:
    """A prompt of five full chunks plus a partial one, from worker 1 of 2."""
    tokens = tuple(range(1000, 1000 + NUM_CHUNKS * CHUNK_TOKENS + 7))
    return IPCCacheServerKey(
        model_name=MODEL_NAME,
        world_size=2,
        worker_id=1,
        token_ids=tokens,
        start=0,
        end=NUM_CHUNKS * CHUNK_TOKENS,
        request_id="cmpl-7f3a",
        cache_salt=cache_salt,
    )


def resolve_obj_keys(key: IPCCacheServerKey) -> list[list[ObjectKey]]:
    """What ``MPCacheServerContext.resolve_obj_keys`` returns for ``key``."""
    hasher = TokenHasher(chunk_size=CHUNK_TOKENS)
    chunk_hashes = [
        TokenHasher.hash_to_bytes(h)
        for h in hasher.compute_chunk_hashes(list(key.token_ids), end=key.end)
    ]
    return ipc_key_to_object_keys(
        key, chunk_hashes, list(range(ATTN.num_object_groups))
    )


def fetch_model() -> FetchModel:
    return FetchModel(ModelLayout.from_registration(GROUP_LAYOUTS, KERNEL_LAYERS), ATTN)


def plan_for(
    keys: Sequence[Sequence[ObjectKey]], placer: PackingPlacer | None = None
) -> RequestFetch:
    return build_request_fetch(
        fetch_model(), keys, MAX_RECORD_BYTES, placer or PackingPlacer()
    )


_RECORD_SUFFIX = re.compile(r"\|(?:m|s\|(\d+))$")


def split_record_key(record_key: str) -> tuple[str, int]:
    """Split a record key into its object's key and record index."""
    match = _RECORD_SUFFIX.search(record_key)
    assert match is not None, record_key
    return record_key[: match.start()], int(match.group(1) or 0)


def test_the_plan_reads_exactly_what_the_retrieve_would() -> None:
    """Full attention reads every chunk, the window its last two, aux none."""
    fetch = plan_for(resolve_obj_keys(vllm_request()))

    placed = {(p.chunk_id, p.object_group_id) for p in fetch.request.placements}
    assert placed == {(c, 0) for c in range(NUM_CHUNKS)} | {(3, 1), (4, 1)}
    assert {s.layer_id for s in fetch.plan.slots} == {0, 1, 2, 3}


def test_every_slot_names_a_record_of_the_object_the_request_resolved() -> None:
    """Each object is named by the key the retrieve would have read it by."""
    keys = resolve_obj_keys(vllm_request())
    fetch = plan_for(keys)
    layout = fetch_model().layout

    records_by_object: dict[tuple[int, int], list[int]] = {}
    for slot in fetch.plan.slots:
        group_id = layout.object_group_of_layer(slot.layer_id)
        object_key, index = split_record_key(slot.record_key)
        assert object_key == object_key_to_string(keys[group_id][slot.chunk_id])
        records_by_object.setdefault((slot.chunk_id, group_id), []).append(index)

    for (chunk_id, group_id), indices in records_by_object.items():
        count = layout.record_count(group_id, MAX_RECORD_BYTES)
        assert sorted(indices) == list(range(count)), (chunk_id, group_id)


def test_the_keys_carry_the_workers_rank_and_the_requests_salt() -> None:
    """Rank and salt are part of the stored key, so they reach the records."""
    fetch = plan_for(resolve_obj_keys(vllm_request(cache_salt="tenant-a")))

    for slot in fetch.plan.slots:
        object_key, _ = split_record_key(slot.record_key)
        fields = object_key.split("@")
        assert fields[0] == MODEL_NAME
        assert int(fields[1], 16) == ObjectKey.ComputeKVRank(2, 1, 2, 1)
        assert fields[-1] == "tenant-a"


def test_slots_tile_each_object_at_its_placed_destination() -> None:
    """Every byte of every placed object is covered once, inside its slot."""
    placer = PackingPlacer()
    fetch = plan_for(resolve_obj_keys(vllm_request()), placer)
    layout = fetch_model().layout

    for placement in fetch.request.placements:
        assert (
            placement.dest_offset
            == placer.offsets[(placement.chunk_id, placement.object_group_id)]
        )
        size = layout.object_group_bytes(placement.object_group_id)
        group_layers = {
            layer
            for layer in layout.layer_ids()
            if layout.object_group_of_layer(layer) == placement.object_group_id
        }
        ranges = sorted(
            (s.offset, s.length)
            for s in fetch.plan.slots
            if s.chunk_id == placement.chunk_id and s.layer_id in group_layers
        )
        cursor = placement.dest_offset
        for offset, length in ranges:
            assert offset == cursor
            cursor += length
        assert cursor == placement.dest_offset + size


def test_the_placer_is_asked_once_per_object_with_its_size() -> None:
    """The destination must fit the object, so the placer is told its size."""
    placer = PackingPlacer()
    fetch = plan_for(resolve_obj_keys(vllm_request()), placer)
    layout = fetch_model().layout

    assert sorted(placer.calls) == sorted(
        (p.chunk_id, p.object_group_id, layout.object_group_bytes(p.object_group_id))
        for p in fetch.request.placements
    )


def test_node_names_come_from_the_placer_in_first_seen_order() -> None:
    """Node indices resolve back to the names the placer chose."""
    fetch = plan_for(resolve_obj_keys(vllm_request()))

    assert fetch.plan.node_names == ("node-0", "node-1")
    for slot in fetch.plan.slots:
        assert fetch.plan.node_name_for(slot) == f"node-{slot.chunk_id % 2}"


def test_the_plan_flattens_into_the_native_fetch_call() -> None:
    """The planned request can be handed to the native session as is."""
    fetch = plan_for(resolve_obj_keys(vllm_request()))

    arguments = pipelined_fetch_arguments(fetch.plan)

    for slot, (node_index, record_key, offset, length, layer_id) in zip(
        fetch.plan.slots, arguments.slots, strict=True
    ):
        assert arguments.node_names[node_index] == f"node-{slot.chunk_id % 2}"
        assert (record_key, offset, length, layer_id) == (
            slot.record_key,
            slot.offset,
            slot.length,
            slot.layer_id,
        )


@pytest.mark.parametrize(
    "num_chunks, window, expected",
    [(5, -1, 0), (5, 2, 3), (5, 5, 0), (5, 9, 0), (0, 2, 0), (1, 1, 0)],
)
def test_the_window_rule_matches_the_retrieve_path(
    num_chunks: int, window: int, expected: int
) -> None:
    """A window wider than the request reads it all; -1 is the whole prefix."""
    assert first_in_window_chunk(num_chunks, window) == expected


def test_keys_for_the_wrong_number_of_groups_are_rejected() -> None:
    """Keys that do not line up with the model's groups name wrong objects."""
    keys = resolve_obj_keys(vllm_request())
    with pytest.raises(ValueError, match="object groups"):
        request_cache_keys(keys[:2], ATTN)


def test_groups_disagreeing_on_the_chunk_count_are_rejected() -> None:
    """Chunk ids index every group alike, so the counts must match."""
    keys = resolve_obj_keys(vllm_request())
    with pytest.raises(ValueError, match="number of chunks"):
        request_cache_keys([keys[0], keys[1][:-1], keys[2]], ATTN)


def test_a_request_with_no_full_chunk_plans_nothing() -> None:
    """A prompt shorter than a chunk has no stored objects to fetch."""
    with pytest.raises(ValueError, match="at least one chunk"):
        plan_for([[], [], []])


def test_the_registry_keeps_a_model_until_its_last_registration_goes() -> None:
    """Every worker registers the model; the entry lives until the last."""
    registry = FetchModelRegistry()
    model = fetch_model()
    registry.register(MODEL_NAME, 2, model)
    registry.register(MODEL_NAME, 2, model)

    registry.unregister(MODEL_NAME, 2)
    assert registry.find(MODEL_NAME, 2) is model
    registry.unregister(MODEL_NAME, 2)
    with pytest.raises(KeyError, match=MODEL_NAME):
        registry.find(MODEL_NAME, 2)
    registry.unregister(MODEL_NAME, 2)


def test_the_registry_separates_world_sizes() -> None:
    """A model at another world size is a different set of stored objects."""
    registry = FetchModelRegistry()
    registry.register(MODEL_NAME, 2, fetch_model())
    with pytest.raises(KeyError):
        registry.find(MODEL_NAME, 4)
