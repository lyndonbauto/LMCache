# SPDX-License-Identifier: Apache-2.0
"""Contract of ``downsample_and_stage_block_ids``.

The block id lists are cut to the blocks each chunk needs, then staged. A
list that is not a whole number of chunks is rejected with ``ValueError``;
this was an ``assert``, which ``python -O`` strips, letting a short list drive
the transfer kernels out of bounds.
"""

# Standard
from dataclasses import dataclass

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.multiprocess.object_group_transfer import (
    downsample_and_stage_block_ids,
)

TOKENS_PER_BLOCK = 32
CHUNK_TOKENS = 128
FULL_ATTENTION = CHUNK_TOKENS


@dataclass(frozen=True)
class _FakeGroups:
    """Kernel groups that differ only in their sub-chunk sliding window."""

    window_tokens: tuple[int, ...]

    @property
    def num_kernel_groups(self) -> int:
        return len(self.window_tokens)

    def get_subchunk_sw_size_tokens(self, kernel_group_idx: int) -> int:
        return self.window_tokens[kernel_group_idx]


class _FakeCacheContext:
    """The slice of a cache context the function reads, with CPU staging."""

    lmcache_tokens_per_chunk = CHUNK_TOKENS

    def __init__(self, *window_tokens: int) -> None:
        self.kv_layer_groups_manager = _FakeGroups(window_tokens)

    def calculate_num_blocks(self, num_tokens: int, kernel_group_idx: int) -> int:
        return num_tokens // TOKENS_PER_BLOCK

    def stage_block_ids(
        self, block_ids_per_group: list[list[int]]
    ) -> list[torch.Tensor]:
        return [torch.tensor(ids, dtype=torch.long) for ids in block_ids_per_group]


def test_a_sub_chunk_window_keeps_only_the_last_blocks_of_each_chunk() -> None:
    """The docstring example: full attention keeps all, a 64-token window two."""
    context = _FakeCacheContext(FULL_ATTENTION, 64)

    staged = downsample_and_stage_block_ids(
        context,  # type: ignore[arg-type]
        [[1, 2, 3, 4, 5, 6, 7, 8], [11, 12, 13, 14, 15, 16, 17, 18]],
    )

    assert [t.tolist() for t in staged] == [
        [1, 2, 3, 4, 5, 6, 7, 8],
        [13, 14, 17, 18],
    ]


def test_a_partial_chunk_of_block_ids_is_rejected() -> None:
    context = _FakeCacheContext(FULL_ATTENTION, 64)

    with pytest.raises(ValueError, match=r"block_ids\[1\]"):
        downsample_and_stage_block_ids(
            context,  # type: ignore[arg-type]
            [[1, 2, 3, 4], [11, 12, 13, 14, 15]],
        )
