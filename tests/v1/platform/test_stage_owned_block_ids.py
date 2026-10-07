# SPDX-License-Identifier: Apache-2.0
"""``stage_owned_block_ids`` stages block IDs no later staging overwrites.

A layerwise retrieve keeps launching copies after its request thread moved on
to the worker's next request, which stages that request's block IDs. Those
copies must keep reading the IDs their retrieve staged.
"""

# Third Party
import pytest
import torch

# First Party
from lmcache import torch_dev, torch_device_type
from lmcache.v1.platform.base.cache_context import BaseCacheContext

pytest.importorskip("lmcache.lmcache_native", reason="needs the native extension")

# First Party
from lmcache.v1.platform.cache_context import create_cache_context  # noqa: E402

CHUNK_TOKENS = 256
NUM_BLOCKS = 4
NUM_LAYERS = 2

_DEVICES = [
    pytest.param(torch.device("cpu"), id="cpu"),
    pytest.param(
        torch.device(torch_device_type),
        id="gpu",
        marks=[
            pytest.mark.cuda,
            pytest.mark.skipif(
                not torch_dev.is_available(), reason="needs a GPU runtime"
            ),
        ],
    ),
]


class _Wrapper:
    """Hands registration a local tensor, as an IPC wrapper would."""

    def __init__(self, tensor: torch.Tensor) -> None:
        self._tensor = tensor

    def to_tensor(self) -> torch.Tensor:
        return self._tensor


def _context(device: torch.device) -> BaseCacheContext:
    layers = [
        torch.zeros(2, NUM_BLOCKS, 16, 8, 64, dtype=torch.bfloat16, device=device)
        for _ in range(NUM_LAYERS)
    ]
    return create_cache_context(
        [_Wrapper(t) for t in layers],  # type: ignore[misc]
        lmcache_tokens_per_chunk=CHUNK_TOKENS,
    )


def _ids(views: list[torch.Tensor]) -> list[list[int]]:
    return [view.cpu().tolist() for view in views]


@pytest.mark.parametrize("device", _DEVICES)
def test_owned_block_ids_survive_later_staging(device: torch.device) -> None:
    context = _context(device)
    try:
        owned = context.stage_owned_block_ids([[3, 1], [], [2]])

        context.stage_block_ids([[9, 9, 9], [9]])
        context.stage_owned_block_ids([[8, 8]])

        assert _ids(owned) == [[3, 1], [], [2]]
        assert all(view.device.type == device.type for view in owned)
        assert all(view.dtype == torch.long for view in owned)
    finally:
        context.close()


@pytest.mark.parametrize("device", _DEVICES)
def test_owned_block_ids_of_no_blocks_are_empty_views(device: torch.device) -> None:
    context = _context(device)
    try:
        assert _ids(context.stage_owned_block_ids([[], []])) == [[], []]
    finally:
        context.close()
