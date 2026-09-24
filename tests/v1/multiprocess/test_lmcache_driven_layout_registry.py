# SPDX-License-Identifier: Apache-2.0
"""Regression tests for GPU transfer layout registration lifetime."""

# Standard
from typing import Any, cast
from unittest.mock import MagicMock, patch
import sys
import types

# Third Party
import pytest
import torch


class _FakeKVLayerGroupsManager:
    """Minimal manager stub: one full-attention object group of two layers."""

    num_object_groups: int = 1
    kernel_groups: list[types.SimpleNamespace] = [
        types.SimpleNamespace(layer_indices=[0, 1])
    ]
    object_groups: list[types.SimpleNamespace] = [
        types.SimpleNamespace(kernel_group_indices=[0])
    ]

    def get_attn_desc(self) -> Any:
        """One full-attention object group."""
        # First Party
        from lmcache.v1.distributed.api import AttnWindowDesc

        return AttnWindowDesc(num_chunks_in_sw=[-1])


class _FakeGPUContext:
    """Small stand-in for GPUCacheContext used by registration tests."""

    device: torch.device = torch.device("cpu")
    num_layers: int = 2
    kv_layer_groups_manager: _FakeKVLayerGroupsManager = _FakeKVLayerGroupsManager()

    def close(self) -> None:
        """No-op teardown (real GPUCacheContext.close deregisters its GDS buffer)."""


class _FakeDeviceHostFuncDispatcher:
    """No-op dispatcher to avoid starting native completion threads."""

    def register(self, kind: str, handler: object, payload_type: object) -> None:
        """Record no native callback registration."""

    def start(self) -> None:
        """Start no background thread."""

    def stop(self) -> None:
        """Stop no background thread."""


@pytest.fixture
def stub_lmcache_native() -> Any:
    """Stub native modules so MP server imports work in source-only test runs.

    The stub covers only what registration touches, so it is used only when
    the real extension is missing; otherwise an import it does not cover
    would fail depending on which tests ran first.
    """
    try:
        # First Party
        import lmcache.lmcache_native  # noqa: F401
    except ImportError:
        pass
    else:
        with patch.dict(sys.modules, {"cupy": MagicMock()}):
            yield
        return
    module = types.ModuleType("lmcache.lmcache_native")
    module_any = cast(Any, module)
    module_any.PageBufferShapeDesc = type("PageBufferShapeDesc", (), {})
    module_any.KernelGroupSpec = type(
        "KernelGroupSpec",
        (),
        {"__init__": lambda self, *args, **kwargs: None},
    )
    module_any.TTLLock = type("TTLLock", (), {})
    module_any.Bitmap = type("Bitmap", (), {})
    module_any.PeriodicEventNotifier = type("PeriodicEventNotifier", (), {})
    with patch.dict(
        sys.modules,
        {
            "lmcache.lmcache_native": module,
            "cupy": MagicMock(),
        },
    ):
        yield


def _registration_module(
    monkeypatch: pytest.MonkeyPatch, ctx: Any, layout_desc: Any
) -> Any:
    """Build the transfer module with CUDA-touching collaborators stubbed out.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        ctx: Engine context the module is built with.
        layout_desc: Layout descriptor every object group reports.

    Returns:
        A ``LMCacheDrivenTransferModule`` ready for ``register_kv_cache``.
    """
    # First Party
    from lmcache.v1.multiprocess.modules import (
        lmcache_driven_transfer as lmcache_driven_transfer_mod,
    )

    monkeypatch.setattr(
        lmcache_driven_transfer_mod,
        "DeviceHostFuncDispatcher",
        _FakeDeviceHostFuncDispatcher,
    )
    monkeypatch.setattr(
        lmcache_driven_transfer_mod,
        "create_cache_context",
        lambda *args, **kwargs: _FakeGPUContext(),
    )
    monkeypatch.setattr(
        lmcache_driven_transfer_mod,
        "get_layout_desc",
        lambda *args, **kwargs: layout_desc,
    )
    monkeypatch.setattr(
        lmcache_driven_transfer_mod.torch_dev,
        "empty_cache",
        lambda: None,
        raising=False,
    )
    return lmcache_driven_transfer_mod.LMCacheDrivenTransferModule(ctx)


def test_registration_hands_storage_the_layout_and_layer_indices(
    monkeypatch: pytest.MonkeyPatch,
    stub_lmcache_native: Any,
) -> None:
    """Storage learns each object group's layout when a worker registers.

    Without this nothing ever tells the storage backend how a payload is laid
    out, so every model -- uniform or hybrid -- is sharded by byte count and
    no record follows a layer boundary.
    """
    # First Party
    from lmcache.utils import EngineType
    from lmcache.v1.distributed.api import MemoryLayoutDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    layout_desc = MemoryLayoutDesc(
        shapes=[torch.Size([2, 2, 16, 32])], dtypes=[torch.float16]
    )
    ctx = MagicMock()
    ctx.chunk_size = 16
    ctx.use_layerwise = False
    ctx.layout_desc_registry = LayoutDescRegistry()

    module = _registration_module(monkeypatch, ctx, layout_desc)
    module.register_kv_cache(1, [], "model", 1, EngineType.VLLM, {}, [], [])

    ctx.storage_manager.set_object_group_layouts.assert_called_once_with(
        {0: layout_desc}, {0: [[0, 1]]}
    )


def test_a_layout_storage_rejects_does_not_fail_registration(
    monkeypatch: pytest.MonkeyPatch,
    stub_lmcache_native: Any,
) -> None:
    """Storage falls back to byte-count records; the worker still registers."""
    # First Party
    from lmcache.utils import EngineType
    from lmcache.v1.distributed.api import MemoryLayoutDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    layout_desc = MemoryLayoutDesc(
        shapes=[torch.Size([2, 2, 16, 32])], dtypes=[torch.float16]
    )
    ctx = MagicMock()
    ctx.chunk_size = 16
    ctx.use_layerwise = False
    ctx.layout_desc_registry = LayoutDescRegistry()
    ctx.storage_manager.set_object_group_layouts.side_effect = ValueError("bad")

    module = _registration_module(monkeypatch, ctx, layout_desc)
    module.register_kv_cache(1, [], "model", 1, EngineType.VLLM, {}, [], [])

    assert ctx.layout_desc_registry.find("model", 1) is layout_desc


def test_registration_builds_the_fetch_layout_until_the_last_worker_leaves(
    monkeypatch: pytest.MonkeyPatch,
    stub_lmcache_native: Any,
) -> None:
    """The fetch layout is built from the published layout, once per model.

    It must describe the same bytes storage was told about, and outlive one
    worker's unregister while another still serves the model.
    """
    # First Party
    from lmcache.utils import EngineType
    from lmcache.v1.distributed.api import MemoryLayoutDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    layout_desc = MemoryLayoutDesc(
        shapes=[torch.Size([2, 2, 16, 32])], dtypes=[torch.float16]
    )
    ctx = MagicMock()
    ctx.chunk_size = 16
    ctx.use_layerwise = False
    ctx.layout_desc_registry = LayoutDescRegistry()

    module = _registration_module(monkeypatch, ctx, layout_desc)
    module.register_kv_cache(1, [], "model", 1, EngineType.VLLM, {}, [], [])
    module.register_kv_cache(2, [], "model", 1, EngineType.VLLM, {}, [], [])

    fetch_model = module.fetch_model("model", 1)
    assert fetch_model.layout.layer_ids() == (0, 1)
    assert fetch_model.layout.object_group_bytes(0) == 2 * 2 * 16 * 32 * 2
    assert fetch_model.attn_desc.num_chunks_in_sw == [-1]

    module.unregister_kv_cache(1)
    assert module.fetch_model("model", 1) is fetch_model
    module.unregister_kv_cache(2)
    with pytest.raises(KeyError, match="no layerwise fetch layout"):
        module.fetch_model("model", 1)


def test_a_layout_that_cannot_be_planned_does_not_fail_registration(
    monkeypatch: pytest.MonkeyPatch,
    stub_lmcache_native: Any,
) -> None:
    """An unplannable layout loses layerwise fetch, not the registration."""
    # First Party
    from lmcache.utils import EngineType
    from lmcache.v1.distributed.api import MemoryLayoutDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    layout_desc = MemoryLayoutDesc(shapes=[torch.Size([64])], dtypes=[torch.float16])
    ctx = MagicMock()
    ctx.chunk_size = 16
    ctx.use_layerwise = False
    ctx.layout_desc_registry = LayoutDescRegistry()
    ctx.storage_manager.set_object_group_layouts.side_effect = ValueError("bad")

    module = _registration_module(monkeypatch, ctx, layout_desc)
    module.register_kv_cache(1, [], "model", 1, EngineType.VLLM, {}, [], [])

    assert ctx.layout_desc_registry.find("model", 1) is layout_desc
    with pytest.raises(KeyError):
        module.fetch_model("model", 1)
    module.unregister_kv_cache(1)


def test_unregister_one_shared_gpu_layout_keeps_registry_until_last_instance(
    monkeypatch: pytest.MonkeyPatch,
    stub_lmcache_native: Any,
) -> None:
    """Unregistering one shared GPU instance must not remove the shared layout."""
    # First Party
    from lmcache.utils import EngineType
    from lmcache.v1.distributed.api import MemoryLayoutDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry
    from lmcache.v1.multiprocess.modules import (
        lmcache_driven_transfer as lmcache_driven_transfer_mod,
    )

    layout_desc = MemoryLayoutDesc(
        shapes=[torch.Size([2, 16, 32])],
        dtypes=[torch.float32],
    )
    ctx = MagicMock()
    ctx.chunk_size = 16
    ctx.use_layerwise = False
    ctx.layout_desc_registry = LayoutDescRegistry()

    def fake_create_cache_context(
        kv_caches: object,
        lmcache_tokens_per_chunk: int,
        layout_hints: object = None,
        engine_group_infos: object = (),
        engine_type: object = None,
        separate_object_groups: bool = False,
        full_sw_kv: bool = False,
    ) -> _FakeGPUContext:
        """Return a fake cache context without touching CUDA or wrappers."""
        return _FakeGPUContext()

    def fake_layout_desc(
        gpu_context: _FakeGPUContext,
        num_tokens: int,
        object_group_id: int = 0,
    ) -> MemoryLayoutDesc:
        """Return the shared layout descriptor used by both registrations."""
        return layout_desc

    monkeypatch.setattr(
        lmcache_driven_transfer_mod,
        "DeviceHostFuncDispatcher",
        _FakeDeviceHostFuncDispatcher,
    )
    monkeypatch.setattr(
        lmcache_driven_transfer_mod,
        "create_cache_context",
        fake_create_cache_context,
    )
    monkeypatch.setattr(
        lmcache_driven_transfer_mod,
        "get_layout_desc",
        fake_layout_desc,
    )
    monkeypatch.setattr(
        lmcache_driven_transfer_mod.torch_dev,
        "empty_cache",
        lambda: None,
        raising=False,
    )

    module = lmcache_driven_transfer_mod.LMCacheDrivenTransferModule(ctx)
    module.register_kv_cache(1, [], "shared-model", 1, EngineType.VLLM, {}, [], [])
    module.register_kv_cache(2, [], "shared-model", 1, EngineType.VLLM, {}, [], [])
    assert ctx.layout_desc_registry.find("shared-model", 1) is layout_desc

    module.unregister_kv_cache(1)

    assert ctx.layout_desc_registry.find("shared-model", 1) is layout_desc

    module.unregister_kv_cache(2)
    assert ctx.layout_desc_registry.find("shared-model", 1) is None


def _layout() -> Any:
    """A minimal layout descriptor for registry tests."""
    # First Party
    from lmcache.v1.distributed.api import MemoryLayoutDesc

    return MemoryLayoutDesc(shapes=[torch.Size([2, 4])], dtypes=[torch.float16])


def test_registry_attn_desc_roundtrip() -> None:
    """register stores the attention-window descriptor; find_attn_desc reads it."""
    # First Party
    from lmcache.v1.distributed.api import AttnWindowDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    registry = LayoutDescRegistry()
    registry.register(
        "m", 2, _layout(), attn_desc=AttnWindowDesc(num_chunks_in_sw=[-1, 2])
    )

    desc = registry.find_attn_desc("m", 2)
    assert desc.num_chunks_in_sw == [-1, 2]
    assert desc.world_size == 2


def test_registry_derives_group_layout_descs_when_not_given() -> None:
    """A registration without group_layout_descs (engine-driven, blend,
    qstore) still yields one shared-layout entry per object group, so
    lookups never hit the missing-group-layouts error path."""
    # First Party
    from lmcache.v1.distributed.api import AttnWindowDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    registry = LayoutDescRegistry()
    layout = _layout()
    registry.register("m", 1, layout)
    assert registry.find_group_layout_descs("m", 1) == {0: layout}

    registry.register(
        "m2", 1, layout, attn_desc=AttnWindowDesc(num_chunks_in_sw=[-1, 2])
    )
    assert registry.find_group_layout_descs("m2", 1) == {0: layout, 1: layout}


def test_registry_attn_desc_raises_when_unregistered() -> None:
    """find_attn_desc raises for an unknown (model, world_size) pair."""
    # First Party
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    registry = LayoutDescRegistry()

    with pytest.raises(ValueError, match="No attention-window descriptor"):
        registry.find_attn_desc("missing", 1)


def test_registry_windows_default_single_group_when_omitted() -> None:
    """A registration without windows resolves to a single full-attention group."""
    # First Party
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    registry = LayoutDescRegistry()
    registry.register("m", 1, _layout())

    assert registry.find_attn_desc("m", 1).num_chunks_in_sw == [-1]


def test_registry_windows_updated_on_reregister() -> None:
    """Re-registering the same pair refreshes the stored windows."""
    # First Party
    from lmcache.v1.distributed.api import AttnWindowDesc
    from lmcache.v1.multiprocess.engine_context import LayoutDescRegistry

    registry = LayoutDescRegistry()
    registry.register(
        "m", 1, _layout(), attn_desc=AttnWindowDesc(num_chunks_in_sw=[-1])
    )
    registry.register(
        "m", 1, _layout(), attn_desc=AttnWindowDesc(num_chunks_in_sw=[-1, 4])
    )

    assert registry.find_attn_desc("m", 1).num_chunks_in_sw == [-1, 4]
