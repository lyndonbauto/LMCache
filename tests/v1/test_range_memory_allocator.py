# SPDX-License-Identifier: Apache-2.0
"""Tests for RangeMemoryAllocator, per its docstring contract.

Runs on a plain CPU tensor, so no device or pinned memory is needed.
"""

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.memory_allocators.range_memory_allocator import RangeMemoryAllocator
from lmcache.v1.memory_management import MemoryFormat

ALIGN = 4096
SLAB_BYTES = 16 * ALIGN
RANGE_START = 4 * ALIGN
RANGE_BYTES = 4 * ALIGN
ONE_PAGE = torch.Size([ALIGN])


@pytest.fixture
def slab() -> torch.Tensor:
    return torch.zeros(SLAB_BYTES, dtype=torch.uint8)


@pytest.fixture
def allocator(slab: torch.Tensor) -> RangeMemoryAllocator:
    return RangeMemoryAllocator(slab, RANGE_START, RANGE_BYTES, ALIGN)


def test_addresses_are_slab_offsets_inside_the_range(
    slab: torch.Tensor, allocator: RangeMemoryAllocator
) -> None:
    objs = allocator.batched_allocate(ONE_PAGE, torch.uint8, 4)

    assert objs is not None
    addresses = sorted(obj.meta.address for obj in objs)
    assert addresses == [RANGE_START + i * ALIGN for i in range(4)]
    for obj in objs:
        assert obj.data_ptr == slab.data_ptr() + obj.meta.address


def test_writes_land_in_the_slab_range(
    slab: torch.Tensor, allocator: RangeMemoryAllocator
) -> None:
    obj = allocator.allocate(ONE_PAGE, torch.uint8)
    assert obj is not None

    obj.tensor.fill_(7)

    start = obj.meta.address
    assert torch.all(slab[start : start + ALIGN] == 7)
    assert torch.all(slab[:RANGE_START] == 0)
    assert torch.all(slab[RANGE_START + RANGE_BYTES :] == 0)


def test_a_full_range_returns_none_and_allocates_nothing(
    allocator: RangeMemoryAllocator,
) -> None:
    assert allocator.batched_allocate(ONE_PAGE, torch.uint8, 5) is None
    assert allocator.get_used_bytes() == 0

    held = allocator.batched_allocate(ONE_PAGE, torch.uint8, 4)
    assert held is not None
    assert allocator.allocate(ONE_PAGE, torch.uint8) is None


def test_freed_memory_is_reused(allocator: RangeMemoryAllocator) -> None:
    objs = allocator.batched_allocate(ONE_PAGE, torch.uint8, 4)
    assert objs is not None
    assert allocator.get_used_bytes() == RANGE_BYTES

    allocator.batched_free(objs)

    assert allocator.get_used_bytes() == 0
    assert allocator.batched_allocate(ONE_PAGE, torch.uint8, 4) is not None
    assert allocator.memcheck()


def test_freeing_twice_is_harmless(allocator: RangeMemoryAllocator) -> None:
    obj = allocator.allocate(ONE_PAGE, torch.uint8)
    assert obj is not None

    allocator.free(obj)
    allocator.free(obj)

    assert allocator.get_used_bytes() == 0
    assert allocator.memcheck()


def test_contains_is_decided_by_data_pointer(
    slab: torch.Tensor, allocator: RangeMemoryAllocator
) -> None:
    inside = allocator.allocate(ONE_PAGE, torch.uint8)
    neighbour = RangeMemoryAllocator(slab, 0, RANGE_START, ALIGN)
    outside = neighbour.allocate(ONE_PAGE, torch.uint8)
    assert inside is not None and outside is not None

    assert allocator.contains(inside)
    assert not allocator.contains(outside)


def test_freeing_a_foreign_object_raises(
    slab: torch.Tensor, allocator: RangeMemoryAllocator
) -> None:
    neighbour = RangeMemoryAllocator(slab, 0, RANGE_START, ALIGN)
    foreign = neighbour.allocate(ONE_PAGE, torch.uint8)
    assert foreign is not None

    with pytest.raises(ValueError):
        allocator.free(foreign)


def test_binary_buffer_is_refused(allocator: RangeMemoryAllocator) -> None:
    with pytest.raises(ValueError):
        allocator.allocate(ONE_PAGE, torch.uint8, fmt=MemoryFormat.BINARY_BUFFER)


@pytest.mark.parametrize(
    ("start", "size"),
    [
        (-ALIGN, ALIGN),
        (0, 0),
        (ALIGN // 2, ALIGN),
        (0, ALIGN + 1),
        (SLAB_BYTES - ALIGN, 2 * ALIGN),
    ],
)
def test_bad_ranges_are_refused(slab: torch.Tensor, start: int, size: int) -> None:
    with pytest.raises(ValueError):
        RangeMemoryAllocator(slab, start, size, ALIGN)
