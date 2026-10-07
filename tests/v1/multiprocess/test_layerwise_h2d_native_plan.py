# SPDX-License-Identifier: Apache-2.0
"""GPU tests for per-layer staging run as one native plan per layer.

A real :class:`GPUCacheContext` drives the real copy kernels. The per-layer
retrieve must write exactly what the per-call whole-object path writes, while
issuing one ``execute_object_group_transfer`` call per scheduled layer.
"""

# Standard
from collections.abc import Callable

# Third Party
import pytest
import torch

# First Party
from lmcache import torch_device_type
from lmcache.v1.multiprocess import object_group_transfer
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.multiprocess.retrieve_sequencer import RetrieveLaunchSequencer

pytest.importorskip("cupy", reason="GPU cache context tests require cupy")

# First Party
from lmcache.v1.platform.cuda.cache_context import GPUCacheContext  # noqa: E402


def _native_layer_launches() -> bool:
    """Whether the compiled plan executor takes per-launch layer ranges."""
    device_ops = object_group_transfer.device_ops
    if not hasattr(device_ops, "execute_object_group_transfer"):
        return False
    try:
        device_ops.LaunchVar(0, 0, 0, 1, 0, layer_offset=0, n_layers=1)
    except (TypeError, NotImplementedError):
        return False
    return True


pytestmark = [
    pytest.mark.cuda,
    pytest.mark.skipif(
        not (torch.cuda.is_available() and torch_device_type == "cuda"),
        reason="requires a CUDA or ROCm device",
    ),
    pytest.mark.skipif(
        not _native_layer_launches(),
        reason="native extension lacks layer-subrange launches",
    ),
]

_CHUNK_TOKENS = 32
_BLOCK_SIZE = 16
_NUM_BLOCKS = 24
#: Six chunks in staging batches of four: the second batch reuses the first
#: batch's slots, so each layer's batches must stay in order.
_NUM_CHUNKS = 6
#: Two kernel groups (different dtypes) in one object group.
_GROUPS = ((4, 8, 64, torch.bfloat16), (2, 4, 32, torch.float16))


class _FakeIPCWrapper:
    """Hands ``GPUCacheContext`` a local tensor in place of a CUDA IPC handle."""

    def __init__(self, tensor: torch.Tensor) -> None:
        self._tensor = tensor

    def to_tensor(self) -> torch.Tensor:
        return self._tensor

    def close(self) -> None:
        return None


class _PinnedObject:
    """A host memory object over one pinned tensor, laid out like staging."""

    def __init__(self, host: torch.Tensor) -> None:
        self.raw_tensor = host

    @property
    def data_ptr(self) -> int:
        return self.raw_tensor.data_ptr()

    def get_size(self) -> int:
        return self.raw_tensor.nbytes

    def parent(self) -> None:
        return None


class _NoEventBackend:
    """Event backend whose events do nothing; these tests sync the stream."""

    device_type = "cuda"

    def record_event(self, event: object, stream: object) -> None:
        return None


def _make_context() -> tuple[GPUCacheContext, list[torch.Tensor]]:
    kv_tensors = [
        torch.zeros(
            2,
            _NUM_BLOCKS,
            _BLOCK_SIZE,
            heads,
            head_size,
            dtype=dtype,
            device="cuda",
        )
        for layers, heads, head_size, dtype in _GROUPS
        for _ in range(layers)
    ]
    context = GPUCacheContext(
        [_FakeIPCWrapper(t) for t in kv_tensors],  # type: ignore[misc]
        lmcache_tokens_per_chunk=_CHUNK_TOKENS,
    )
    return context, kv_tensors


def _schedule(context: GPUCacheContext) -> LayerwiseSchedule:
    return LayerwiseSchedule(
        [group.layer_indices for group in context.kv_layer_groups_manager.kernel_groups]
    )


def _block_ids(context: GPUCacheContext) -> list[torch.Tensor]:
    """A scattered block per chunk position, per kernel group."""
    generator = torch.Generator().manual_seed(7)
    blocks_per_chunk = _CHUNK_TOKENS // _BLOCK_SIZE
    count = _NUM_CHUNKS * blocks_per_chunk
    return [
        torch.randperm(_NUM_BLOCKS, generator=generator)[:count].to("cuda")
        for _ in context.kv_layer_groups_manager.kernel_groups
    ]


def _host_objects(context: GPUCacheContext, fill: bool) -> list[_PinnedObject]:
    nbytes = context.get_temp_object_group_buffer(0, 0).nbytes
    generator = torch.Generator().manual_seed(11)
    objects = []
    for _ in range(_NUM_CHUNKS):
        host = torch.zeros(nbytes, dtype=torch.uint8).pin_memory()
        if fill:
            host.copy_(torch.randint(0, 256, (nbytes,), generator=generator).byte())
        objects.append(_PinnedObject(host))
    return objects


def _retrieve(
    context: GPUCacheContext,
    objects: list[_PinnedObject],
    block_ids: list[torch.Tensor],
    skip_first_n_tokens: int,
    staging: object_group_transfer.LayerStaging,
    before_launch: Callable[[int, int], None] = lambda _group, _position: None,
) -> None:
    """Run one retrieve to completion on the context's stream."""
    schedule = _schedule(context)
    count = schedule.launch_count()
    retrieve = object_group_transfer.LayerwiseH2DRetrieve(
        context,
        block_ids,
        object_group_transfer.FixedMemoryObjects([objects]),  # type: ignore[list-item]
        skip_first_n_tokens,
        schedule,
        RetrieveLaunchSequencer(
            context.stream,
            LayerProgressRecord(bytearray(LayerProgressRecord.RECORD_SIZE)),
            DaemonLayerLaunchEventPool(
                [object()] * count,
                _NoEventBackend(),  # type: ignore[arg-type]
                count,
            ),
            context.transfer_gate,
        ),
        1,
        staging=staging,
    )
    with torch.cuda.stream(context.stream):
        retrieve.begin()
        for launch in schedule.launches:
            before_launch(launch.kernel_group_index, launch.position_in_group)
            retrieve.launch_layer(launch.layer_id)
    context.stream.synchronize()


def _snapshot(kv_tensors: list[torch.Tensor]) -> list[torch.Tensor]:
    return [t.clone() for t in kv_tensors]


def _same_bytes(got: torch.Tensor, want: torch.Tensor) -> bool:
    """Compare bit patterns: random host bytes include NaNs, never equal."""
    return torch.equal(got.view(torch.uint8), want.view(torch.uint8))


def _reset(kv_tensors: list[torch.Tensor]) -> None:
    for tensor in kv_tensors:
        tensor.fill_(-3.0)


@pytest.fixture
def native_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count native plan calls; each records how many batch steps it ran."""
    calls: list[int] = []
    execute = object_group_transfer.device_ops.execute_object_group_transfer

    def counting(*args: object, **kwargs: object) -> None:
        calls.append(len(args[4]))  # type: ignore[arg-type]
        execute(*args, **kwargs)

    monkeypatch.setattr(
        object_group_transfer.device_ops, "execute_object_group_transfer", counting
    )
    return calls


@pytest.mark.parametrize("skip_first_n_tokens", [0, 48], ids=["no-skip", "skip"])
def test_per_layer_plan_writes_what_the_per_call_path_writes(
    native_calls: list[int], skip_first_n_tokens: int
) -> None:
    """One native call per layer lands the same KV as whole-object staging."""
    context, kv_tensors = _make_context()
    objects = _host_objects(context, fill=True)
    block_ids = _block_ids(context)

    _reset(kv_tensors)
    _retrieve(
        context,
        objects,
        block_ids,
        skip_first_n_tokens,
        object_group_transfer.LayerStaging.WHOLE_OBJECT,
    )
    expected = _snapshot(kv_tensors)
    assert native_calls == []

    _reset(kv_tensors)
    _retrieve(
        context,
        objects,
        block_ids,
        skip_first_n_tokens,
        object_group_transfer.LayerStaging.PER_LAYER,
    )

    assert native_calls == [2] * _schedule(context).launch_count()
    for got, want in zip(kv_tensors, expected, strict=True):
        assert _same_bytes(got, want)
    assert any(not torch.all(t == -3.0) for t in expected), "nothing was written"
    context.close()


def test_per_layer_plan_reads_each_layer_at_its_launch(
    native_calls: list[int],
) -> None:
    """Layers that land in host memory just before their launch still arrive.

    The host objects start empty and each layer's bytes are written only
    right before that layer launches, as an arrival-driven fetch would.
    """
    context, kv_tensors = _make_context()
    complete = _host_objects(context, fill=True)
    block_ids = _block_ids(context)
    _reset(kv_tensors)
    _retrieve(
        context, complete, block_ids, 0, object_group_transfer.LayerStaging.WHOLE_OBJECT
    )
    expected = _snapshot(kv_tensors)

    arriving = _host_objects(context, fill=False)
    region_starts = [
        context.get_temp_kernel_group_buffer(0, group).data_ptr()
        - context.get_temp_object_group_buffer(0, 0).data_ptr()
        for group in range(len(_GROUPS))
    ]

    def arrive(kernel_group: int, position: int) -> None:
        view = context.get_temp_kernel_group_buffer(0, kernel_group)
        plane_stride = view.stride(0) * view.element_size()
        layer_stride = view.stride(1) * view.element_size()
        for source, target in zip(complete, arriving, strict=True):
            for plane in range(view.shape[0]):
                start = (
                    region_starts[kernel_group]
                    + plane * plane_stride
                    + position * layer_stride
                )
                end = start + layer_stride
                target.raw_tensor[start:end] = source.raw_tensor[start:end]

    _reset(kv_tensors)
    _retrieve(
        context,
        arriving,
        block_ids,
        0,
        object_group_transfer.LayerStaging.PER_LAYER,
        before_launch=arrive,
    )

    assert len(native_calls) == _schedule(context).launch_count()
    for got, want in zip(kv_tensors, expected, strict=True):
        assert _same_bytes(got, want)
    context.close()
