# SPDX-License-Identifier: Apache-2.0
"""The native adapter tells the storage backend how records must be cut.

The writer is what decides whether a layer can be fetched on its own: if a
record straddles two layers, neither is ready until both have landed. It can
only keep records inside a layer if it is told each object group's plane
runs, and the runs it is told must be the ones the reader numbers records
by -- otherwise a slot names a record holding someone else's bytes.
"""

# Standard
from collections.abc import Iterator

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
)
from lmcache.v1.layerwise import ModelLayout, PlaneRun, record_plane_runs
from lmcache.v1.platform import create_event_notifier


class _RecordLayoutClient:
    """Native client stub that records what layouts it was given."""

    def __init__(self, *, accepts_record_layouts: bool = True) -> None:
        """Create the stub.

        Args:
            accepts_record_layouts: Whether to expose ``set_record_layouts``,
                as a connector built before record layouts existed would not.
        """
        self._efd = create_event_notifier()
        self.record_layouts: list[list[list[tuple[int, int]]]] = []
        self.object_group_layouts: list[dict[int, dict[str, object]]] = []
        if accepts_record_layouts:
            self.set_record_layouts = self._set_record_layouts

    def event_fd(self) -> int:
        """Return the completion eventfd."""
        return self._efd.fileno()

    def set_object_group_layouts(self, layouts: dict[int, dict[str, object]]) -> None:
        """Record the pipelined planner layouts."""
        self.object_group_layouts.append(layouts)

    def close(self) -> None:
        """Release the eventfd."""
        self._efd.close()

    def _set_record_layouts(self, object_groups: list[list[tuple[int, int]]]) -> None:
        """Record the record layouts."""
        self.record_layouts.append(object_groups)


def _hybrid_descs() -> dict[int, MemoryLayoutDesc]:
    """A hybrid model: object group 0 mixes two plane sizes.

    Returns:
        Group 0 with a 4D attention kernel group (2 x 3 layers of 16 x 32
        fp16 = 1024-byte planes) and a 3D kernel group (2 layers of 8 x 64
        fp32 = 2048-byte planes); group 1 with one 4D kernel group.
    """
    return {
        0: MemoryLayoutDesc(
            shapes=[torch.Size([2, 3, 16, 32]), torch.Size([2, 8, 64])],
            dtypes=[torch.float16, torch.float32],
        ),
        1: MemoryLayoutDesc(
            shapes=[torch.Size([2, 1, 4, 64])], dtypes=[torch.bfloat16]
        ),
    }


@pytest.fixture
def client() -> Iterator[_RecordLayoutClient]:
    """A client that accepts record layouts, closed by its adapter."""
    yield _RecordLayoutClient()


def test_the_writer_is_told_each_kernel_groups_plane_runs(
    client: _RecordLayoutClient,
) -> None:
    """One (plane_bytes, planes) pair per kernel group, per object group."""
    adapter = NativeConnectorL2Adapter(native_client=client, type_name="test")
    try:
        adapter.set_object_group_layouts(_hybrid_descs())
    finally:
        adapter.close()

    assert client.record_layouts == [[[(1024, 6), (2048, 2)], [(512, 2)]]]


def test_the_pipelined_planner_still_gets_its_layouts(
    client: _RecordLayoutClient,
) -> None:
    """Record layouts are an addition, not a replacement."""
    adapter = NativeConnectorL2Adapter(native_client=client, type_name="test")
    try:
        adapter.set_object_group_layouts(
            _hybrid_descs(), {0: [[0, 2, 4], [1, 3]], 1: [[5]]}
        )
    finally:
        adapter.close()

    assert len(client.object_group_layouts) == 1
    assert client.object_group_layouts[0][0]["layer_indices"] == [[0, 2, 4], [1, 3]]


def test_a_group_without_layer_indices_lets_the_native_side_number_layers(
    client: _RecordLayoutClient,
) -> None:
    """No indices means no key: the native side rejects an empty list."""
    adapter = NativeConnectorL2Adapter(native_client=client, type_name="test")
    try:
        adapter.set_object_group_layouts(_hybrid_descs(), {0: [[0, 2, 4], [1, 3]]})
    finally:
        adapter.close()

    layouts = client.object_group_layouts[0]
    assert layouts[0]["layer_indices"] == [[0, 2, 4], [1, 3]]
    assert "layer_indices" not in layouts[1]


def test_a_client_without_record_layouts_is_left_alone() -> None:
    """An older connector keeps working; it just stays byte-count sharded."""
    client = _RecordLayoutClient(accepts_record_layouts=False)
    adapter = NativeConnectorL2Adapter(native_client=client, type_name="test")
    try:
        adapter.set_object_group_layouts(_hybrid_descs())
    finally:
        adapter.close()

    assert not hasattr(client, "set_record_layouts")
    assert len(client.object_group_layouts) == 1


def test_an_unsupported_shape_is_reported_not_forwarded(
    client: _RecordLayoutClient,
) -> None:
    """Guessing runs for a shape the planner cannot parse would misalign."""
    adapter = NativeConnectorL2Adapter(native_client=client, type_name="test")
    try:
        with pytest.raises(ValueError, match="3D or 4D"):
            adapter.set_object_group_layouts(
                {0: MemoryLayoutDesc(shapes=[torch.Size([8, 64])], dtypes=[torch.half])}
            )
    finally:
        adapter.close()

    assert client.record_layouts == []


def test_the_writers_runs_are_the_readers_runs() -> None:
    """The writer cuts by these runs and the reader numbers by them.

    ``record_plane_runs`` needs no layer indices and ``ModelLayout`` does, so
    they are separate code paths over the same shapes. If they disagreed, a
    slot would name a record index the writer assigned to different bytes.
    """
    descs = _hybrid_descs()
    layout = ModelLayout.from_registration(descs, {0: [[0, 2, 4], [1, 3]], 1: [[5]]})
    runs = record_plane_runs(descs)

    assert set(runs) == set(layout.object_group_ids())
    for object_group_id, group_runs in runs.items():
        assert group_runs == layout.plane_runs(object_group_id)
    assert runs[0] == (
        PlaneRun(plane_bytes=1024, planes=6),
        PlaneRun(plane_bytes=2048, planes=2),
    )
