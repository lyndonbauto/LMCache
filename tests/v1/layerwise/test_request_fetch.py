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

# First Party
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.layerwise import pipelined_fetch_arguments
from lmcache.v1.layerwise.request_fetch import (
    ChunkLocation,
    FetchModelRegistry,
    ObjectToPlace,
    RequestFetch,
    build_request_fetch,
    first_in_window_chunk,
    objects_to_place,
    request_cache_keys,
)

# Local
from .placers import PackingLease, PackingPlacer
from .vllm_requests import (
    ATTN,
    MAX_RECORD_BYTES,
    MODEL_NAME,
    NUM_CHUNKS,
    fetch_model,
    resolve_obj_keys,
    vllm_request,
)


def plan_for(
    keys: Sequence[Sequence[ObjectKey]], placer: PackingPlacer | None = None
) -> RequestFetch:
    """Lease a window the way retrieve does, then plan into it."""
    placer = placer or PackingPlacer()
    lease = placer.lease(objects_to_place(fetch_model(), keys))
    return build_request_fetch(fetch_model(), keys, MAX_RECORD_BYTES, lease)


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

    (lease,) = placer.leases
    for placement in fetch.request.placements:
        assert (
            placement.dest_offset
            == lease.locate(placement.chunk_id, placement.object_group_id).dest_offset
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


def test_the_placer_is_asked_once_per_request_with_every_objects_size() -> None:
    """One lease covers the request, and each destination must fit its object."""
    placer = PackingPlacer()
    fetch = plan_for(resolve_obj_keys(vllm_request()), placer)
    layout = fetch_model().layout

    (request,) = placer.requests
    assert request == tuple(
        ObjectToPlace(
            p.chunk_id,
            p.object_group_id,
            layout.object_group_bytes(p.object_group_id),
        )
        for p in fetch.request.placements
    )


def _lease_with(
    window_bytes: int, others_from: int = 1 << 20, **offsets: int
) -> tuple[PackingLease, list[list[ObjectKey]]]:
    """A lease placing the request's objects at chosen offsets.

    ``offsets`` maps ``c<chunk>g<group>`` to a destination offset; every other
    object is packed back to back from ``others_from``.
    """
    keys = resolve_obj_keys(vllm_request())
    objects = objects_to_place(fetch_model(), keys)
    locations: dict[tuple[int, int], ChunkLocation] = {}
    cursor = others_from
    for obj in objects:
        name = f"c{obj.chunk_id}g{obj.object_group_id}"
        if name in offsets:
            offset = offsets[name]
        else:
            offset = cursor
            cursor += obj.object_bytes
        locations[(obj.chunk_id, obj.object_group_id)] = ChunkLocation("n", offset)
    return PackingLease(window_bytes, locations), keys


def test_an_object_reaching_past_the_window_is_refused() -> None:
    """A write past the window lands in memory no lease covers."""
    size = fetch_model().layout.object_group_bytes(0)
    lease, keys = _lease_with(1 << 30, others_from=0, c0g0=(1 << 30) - size + 1)

    with pytest.raises(ValueError, match="outside the"):
        build_request_fetch(fetch_model(), keys, MAX_RECORD_BYTES, lease)


def test_an_object_that_exactly_fills_the_window_end_is_accepted() -> None:
    """The last byte of the window is usable."""
    size = fetch_model().layout.object_group_bytes(0)
    lease, keys = _lease_with(1 << 30, others_from=0, c0g0=(1 << 30) - size)

    fetch = build_request_fetch(fetch_model(), keys, MAX_RECORD_BYTES, lease)

    assert max(s.offset + s.length for s in fetch.plan.slots) == 1 << 30


def test_a_negative_offset_is_refused() -> None:
    """A negative offset writes before the window."""
    lease, keys = _lease_with(1 << 30, c0g0=-1)

    with pytest.raises(ValueError, match="outside the"):
        build_request_fetch(fetch_model(), keys, MAX_RECORD_BYTES, lease)


def test_two_objects_sharing_bytes_are_refused() -> None:
    """Overlapping destinations make one fetch overwrite another's data."""
    size = fetch_model().layout.object_group_bytes(0)
    lease, keys = _lease_with(1 << 30, c0g0=0, c1g0=size - 1)

    with pytest.raises(ValueError, match="overlap"):
        build_request_fetch(fetch_model(), keys, MAX_RECORD_BYTES, lease)


def test_adjacent_objects_are_not_an_overlap() -> None:
    """Objects that touch but do not share a byte are fine."""
    size = fetch_model().layout.object_group_bytes(0)
    lease, keys = _lease_with(1 << 30, c0g0=0, c1g0=size)

    build_request_fetch(fetch_model(), keys, MAX_RECORD_BYTES, lease)


def test_request_bytes_counts_exactly_the_objects_a_request_reads() -> None:
    """Window sizing must agree with what the placer is asked to place."""
    model = fetch_model()
    objects = objects_to_place(model, resolve_obj_keys(vllm_request()))

    assert model.request_bytes(NUM_CHUNKS) == sum(o.object_bytes for o in objects)


def test_request_bytes_honours_windows_aux_groups_and_alignment() -> None:
    """Full attention scales with chunks, the window caps at 2, aux is free."""
    model = fetch_model()
    full = model.layout.object_group_bytes(0)
    windowed = model.layout.object_group_bytes(1)

    assert model.request_bytes(0) == 0
    assert model.request_bytes(1) == full + windowed
    assert model.request_bytes(10) == 10 * full + 2 * windowed
    align = 1 << 20
    assert model.request_bytes(3, align_bytes=align) == 3 * align + 2 * align


@pytest.mark.parametrize("num_chunks, align", [(-1, 1), (1, 0), (1, -4)])
def test_request_bytes_rejects_meaningless_arguments(
    num_chunks: int, align: int
) -> None:
    """Negative chunk counts and non-positive alignments size nothing."""
    with pytest.raises(ValueError):
        fetch_model().request_bytes(num_chunks, align_bytes=align)


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
