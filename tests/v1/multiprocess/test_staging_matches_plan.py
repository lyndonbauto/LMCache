# SPDX-License-Identifier: Apache-2.0
"""The pipelined fetch lands each layer exactly where per-layer staging reads it.

Two pieces compute a layer's bytes independently: the planner, from the
shapes registration publishes (Track C), and per-layer staging, from the
strides of the cache context's staging views (Track B). If they ever drift,
the GPU reads bytes the transport never wrote, and nothing fails.
Registration therefore refuses the pipelined fetch for a model on which they
disagree (``check_staging_matches_plan``).

These tests build real cache contexts the way registration does, through
``create_cache_context``, for the layouts LMCache serves, and check that the
two agree; and that the check catches a layout that would corrupt KV. The
CPU cases run anywhere; the GPU cases build the real GPU context.
"""

# Standard
from collections.abc import Callable
from dataclasses import dataclass

# Third Party
import pytest
import torch

# First Party
from lmcache import torch_dev, torch_device_type
from lmcache.v1.distributed.api import MemoryLayoutDesc
from lmcache.v1.layerwise import LayerwiseContractError
from lmcache.v1.layerwise.planner import ModelLayout
from lmcache.v1.multiprocess.group_view import EngineGroupInfo
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.platform.base.cache_context import BaseCacheContext

pytest.importorskip("lmcache.lmcache_native", reason="needs the native extension")

# First Party
from lmcache.v1.multiprocess.modules.lmcache_driven_transfer import (  # noqa: E402
    get_layout_desc,
)
from lmcache.v1.multiprocess.object_group_transfer import (  # noqa: E402
    per_layer_staging_ranges,
)
from lmcache.v1.multiprocess.pipelined_loading import (  # noqa: E402
    check_staging_matches_plan,
)
from lmcache.v1.platform.cache_context import create_cache_context  # noqa: E402

CHUNK_TOKENS = 256
NUM_BLOCKS = 4


class _Wrapper:
    """Hands registration a local tensor, as an IPC wrapper would."""

    def __init__(self, tensor: torch.Tensor) -> None:
        self._tensor = tensor

    def to_tensor(self) -> torch.Tensor:
        return self._tensor


@dataclass(frozen=True)
class _Layers:
    """Layers of one shape: ``[2, NB, BS, NH, HS]`` per layer."""

    layer_ids: tuple[int, ...]
    num_heads: int = 8
    head_size: int = 64
    block_size: int = 16
    dtype: torch.dtype = torch.bfloat16


@dataclass(frozen=True)
class _Model:
    """A model as registration sees it."""

    layers: tuple[_Layers, ...]
    engine_group_infos: tuple[EngineGroupInfo, ...] = ()
    separate_object_groups: bool = False


MODELS = {
    # Llama-style: one shape, one kernel group, one object group.
    "dense": _Model((_Layers(tuple(range(4))),)),
    # Two shapes and dtypes in one object group: two kernel groups whose
    # planes differ in size, concatenated in one payload.
    "two_kernel_groups": _Model(
        (
            _Layers((0, 1, 2, 3)),
            _Layers((4, 5), num_heads=16, dtype=torch.float16),
        )
    ),
    # gpt-oss-style: full and sliding-window attention on alternating
    # layers, one object group per window size. A kernel group's layers are
    # not consecutive global layers.
    "hybrid_sliding_window": _Model(
        (_Layers((0, 1, 2, 3)),),
        engine_group_infos=(
            EngineGroupInfo(0, (0, 2), tokens_per_block=16),
            EngineGroupInfo(1, (1, 3), tokens_per_block=16, sw_size_tokens=128),
        ),
        separate_object_groups=True,
    ),
    # A group whose engine block covers more tokens than it has slots.
    "compressed": _Model(
        (_Layers((0, 1), block_size=8),),
        engine_group_infos=(EngineGroupInfo(0, (0, 1), tokens_per_block=16),),
    ),
}


def _kv_tensors(model: _Model, device: torch.device) -> list[torch.Tensor]:
    """One ``[2, NB, BS, NH, HS]`` tensor per layer, in global layer order."""
    by_layer: dict[int, torch.Tensor] = {}
    for layers in model.layers:
        for layer_id in layers.layer_ids:
            by_layer[layer_id] = torch.zeros(
                2,
                NUM_BLOCKS,
                layers.block_size,
                layers.num_heads,
                layers.head_size,
                dtype=layers.dtype,
                device=device,
            )
    return [by_layer[layer_id] for layer_id in sorted(by_layer)]


def _context(model: _Model, device: torch.device) -> BaseCacheContext:
    return create_cache_context(
        [_Wrapper(t) for t in _kv_tensors(model, device)],  # type: ignore[misc]
        lmcache_tokens_per_chunk=CHUNK_TOKENS,
        engine_group_infos=model.engine_group_infos,
        separate_object_groups=model.separate_object_groups,
    )


#: One kernel group as registration publishes it: shape, dtype, layer ids.
_KernelGroup = tuple[torch.Size, torch.dtype, list[int]]


def _registered_layout(
    context: BaseCacheContext,
    reorder: Callable[[list[_KernelGroup]], list[_KernelGroup]] = lambda kgs: kgs,
) -> ModelLayout:
    """The fetch layout, built from the context exactly as registration does.

    Args:
        context: The registered cache context.
        reorder: Applied to each object group's kernel groups, to simulate a
            planner that drifted from the context.
    """
    manager = context.kv_layer_groups_manager
    group_layout_descs: dict[int, MemoryLayoutDesc] = {}
    group_kernel_layer_indices: dict[int, list[list[int]]] = {}
    for gid, object_group in enumerate(manager.object_groups):
        desc = get_layout_desc(context, CHUNK_TOKENS, object_group_id=gid)
        kernel_groups = reorder(
            [
                (shape, dtype, list(manager.kernel_groups[kernel_index].layer_indices))
                for shape, dtype, kernel_index in zip(
                    desc.shapes,
                    desc.dtypes,
                    object_group.kernel_group_indices,
                    strict=True,
                )
            ]
        )
        group_layout_descs[gid] = MemoryLayoutDesc(
            [shape for shape, _, _ in kernel_groups],
            [dtype for _, dtype, _ in kernel_groups],
        )
        group_kernel_layer_indices[gid] = [layers for _, _, layers in kernel_groups]
    return ModelLayout.from_registration(group_layout_descs, group_kernel_layer_indices)


def _staging(context: BaseCacheContext) -> dict[int, tuple[tuple[int, int], ...]]:
    schedule = LayerwiseSchedule.from_kernel_groups(
        context.kv_layer_groups_manager.kernel_groups
    )
    return per_layer_staging_ranges(context, schedule)


def _one_group_layout(num_layers: int = 2) -> dict[int, MemoryLayoutDesc]:
    """One object group of one kernel group, ``num_layers`` layers deep."""
    return {
        0: MemoryLayoutDesc([torch.Size((2, num_layers, 16, 128))], [torch.float16])
    }


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


@pytest.mark.parametrize("device", _DEVICES)
@pytest.mark.parametrize("model_name", sorted(MODELS))
def test_every_layer_lands_where_staging_reads_it(
    device: torch.device, model_name: str
) -> None:
    context = _context(MODELS[model_name], device)
    try:
        layout = _registered_layout(context)
        staging = _staging(context)

        check_staging_matches_plan(layout, staging)

        assert set(staging) == set(layout.layer_ids())
    finally:
        context.close()


@pytest.mark.parametrize("device", _DEVICES)
def test_kernel_groups_planned_in_the_wrong_order_are_caught(
    device: torch.device,
) -> None:
    """The planner concatenating one object group's kernel groups in another
    order than staging lays them out would land both groups' layers at the
    other's offsets."""
    context = _context(MODELS["two_kernel_groups"], device)
    try:
        layout = _registered_layout(context, reorder=lambda kgs: kgs[::-1])

        with pytest.raises(LayerwiseContractError, match="would land"):
            check_staging_matches_plan(layout, _staging(context))
    finally:
        context.close()


@pytest.mark.parametrize("device", _DEVICES)
def test_layers_planned_at_each_others_positions_are_caught(
    device: torch.device,
) -> None:
    """Swapping two layers' positions along the layer dimension swaps their
    bytes: each layer's attention would read the other's KV."""

    def swap_first_two(kgs: list[_KernelGroup]) -> list[_KernelGroup]:
        (shape, dtype, layers), *rest = kgs
        return [(shape, dtype, [layers[1], layers[0], *layers[2:]]), *rest]

    context = _context(MODELS["dense"], device)
    try:
        layout = _registered_layout(context, reorder=swap_first_two)

        with pytest.raises(LayerwiseContractError, match="layer 0"):
            check_staging_matches_plan(layout, _staging(context))
    finally:
        context.close()


def test_a_layer_only_one_side_covers_is_caught() -> None:
    layout = ModelLayout.from_registration(_one_group_layout(), {0: [[0, 1]]})
    staging = {0: ((0, 4096), (8192, 4096))}

    with pytest.raises(LayerwiseContractError, match="different layers"):
        check_staging_matches_plan(layout, staging)


def test_the_error_names_only_the_first_few_mismatches() -> None:
    num_layers = 10
    layout = ModelLayout.from_registration(
        _one_group_layout(num_layers), {0: [list(range(num_layers))]}
    )
    staging = {layer_id: ((1, 1),) for layer_id in range(num_layers)}

    with pytest.raises(LayerwiseContractError) as caught:
        check_staging_matches_plan(layout, staging)

    message = str(caught.value)
    assert message.startswith("10 layer(s)")
    assert message.count("planned") == 4
