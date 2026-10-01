# SPDX-License-Identifier: Apache-2.0
"""``LMCacheDrivenTransferModule.retrieve`` with ``--use-layerwise`` on.

A layerwise retrieve reads every object from L1 and hands them all to one
``transfer_kv_layerwise_h2d`` call, which publishes progress layer by layer
under the worker's retrieve generation. When the retrieve fails before any
layer copy is queued, the module itself must publish the failure for that
generation, or the worker's per-layer waits never return.

No pipelined sink factory is installed, so no key is deferred: this is the
plain layerwise path. Kernels, streams and the progress record are mocked.
"""

# Standard
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

# Third Party
import pytest

# First Party
from lmcache.v1.multiprocess.modules import lmcache_driven_transfer as mod

NUM_GROUPS = 2
NUM_CHUNKS = 3
GENERATION = 7
KEYS = [[f"g{g}c{c}" for c in range(NUM_CHUNKS)] for g in range(NUM_GROUPS)]
ALL_KEYS = [k for group in KEYS for k in group]


@dataclass
class _LayerwiseCall:
    """The arguments of one ``transfer_kv_layerwise_h2d`` call."""

    objects_by_group: list[list[str]]
    schedule: object
    progress: object
    event_pool: object
    generation: int


@dataclass
class _Harness:
    module: mod.LMCacheDrivenTransferModule
    entry: SimpleNamespace
    layerwise_calls: list[_LayerwiseCall] = field(default_factory=list)
    group_transfers: list[int] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def retrieve(self, generation: int = GENERATION) -> bool:
        _handle, ok = self.module.retrieve(
            key=SimpleNamespace(
                request_id="req", cache_salt="salt", world_size=1, worker_id=0
            ),
            instance_id=1,
            gpu_block_ids=[[1, 2, 3] for _ in range(NUM_GROUPS)],
            event_ipc_handle=b"x",
            skip_first_n_tokens=0,
            retrieve_generation=generation,
        )
        return ok

    @property
    def progress(self) -> MagicMock:
        return self.entry.layer_progress


def _obj(name: str) -> MagicMock:
    obj = MagicMock(get_size=MagicMock(return_value=10))
    obj.name = name
    return obj


@pytest.fixture
def make_harness(monkeypatch: pytest.MonkeyPatch):
    """Build a layerwise module over mocks; ``missing`` keys are not in L1."""

    def build(missing: frozenset[str] = frozenset()) -> _Harness:
        monkeypatch.setattr(mod, "DeviceHostFuncDispatcher", MagicMock())
        monkeypatch.setattr(mod, "downsample_and_stage_block_ids", lambda cc, b: b)
        monkeypatch.setattr(mod, "torch_dev", MagicMock())

        ctx = MagicMock()
        ctx.chunk_size = 256
        ctx.use_layerwise = True
        ctx.resolve_obj_keys.return_value = KEYS
        module = mod.LMCacheDrivenTransferModule(ctx)

        kvlgm = SimpleNamespace(
            num_object_groups=NUM_GROUPS,
            num_kernel_groups=NUM_GROUPS,
            get_attn_desc=lambda: SimpleNamespace(
                num_chunks_in_sw=[-1] * NUM_GROUPS, group_kinds=()
            ),
        )
        cache_context = MagicMock(kv_layer_groups_manager=kvlgm, max_batch_size=8)
        cache_context.calculate_num_blocks.return_value = 1
        entry = SimpleNamespace(
            cache_context=cache_context,
            model_name="m",
            event_backend=MagicMock(),
            layerwise_schedule=MagicMock(),
            layer_progress=MagicMock(),
            daemon_layer_event_pool=MagicMock(),
        )
        monkeypatch.setattr(module, "get_and_touch_context_entry", lambda _id: entry)
        h = _Harness(module=module, entry=entry)

        @contextmanager
        def read(keys: list[str]) -> Iterator[list[MagicMock]]:
            yield [_obj(k) for k in keys if k not in missing]

        ctx.storage_manager.read_prefetched_results.side_effect = read

        def layerwise_transfer(
            cc, block_ids, objs_by_group, skip, schedule, progress, pool, gen, **kw
        ):
            h.layerwise_calls.append(
                _LayerwiseCall(
                    objects_by_group=[[o.name for o in g] for g in objs_by_group],
                    schedule=schedule,
                    progress=progress,
                    event_pool=pool,
                    generation=gen,
                )
            )

        monkeypatch.setattr(mod, "transfer_kv_layerwise_h2d", layerwise_transfer)

        def group_transfer(cc, block_ids, objs, object_group_id, **kwargs):
            h.group_transfers.append(object_group_id)

        monkeypatch.setattr(mod, "transfer_kv_per_object_group", group_transfer)

        def submit(stream, kind, payload):
            if kind == "finish_read_prefetched":
                h.released.extend(payload)

        monkeypatch.setattr(mod, "submit_callback_to_stream", submit)
        return h

    return build


def test_one_layerwise_transfer_carries_every_object_and_the_generation(
    make_harness,
) -> None:
    h = make_harness()

    assert h.retrieve() is True

    [call] = h.layerwise_calls
    assert call.objects_by_group == KEYS
    assert call.schedule is h.entry.layerwise_schedule
    assert call.progress is h.entry.layer_progress
    assert call.event_pool is h.entry.daemon_layer_event_pool
    assert call.generation == GENERATION
    assert h.group_transfers == []
    h.progress.fail_retrieve.assert_not_called()
    assert sorted(h.released) == sorted(ALL_KEYS)


def test_a_key_missing_from_l1_publishes_the_failure_for_this_generation(
    make_harness,
) -> None:
    h = make_harness(missing=frozenset({KEYS[1][0]}))

    assert h.retrieve() is False

    assert h.layerwise_calls == []
    assert h.group_transfers == []
    h.progress.fail_retrieve.assert_called_once_with(GENERATION)


def test_a_transfer_that_raises_publishes_the_failure_for_this_generation(
    make_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness()

    def broken_transfer(*args, **kwargs):
        raise RuntimeError("kernel launch failed")

    monkeypatch.setattr(mod, "transfer_kv_layerwise_h2d", broken_transfer)

    assert h.retrieve() is False

    h.progress.fail_retrieve.assert_called_once_with(GENERATION)
    assert sorted(h.released) == sorted(ALL_KEYS)


def test_a_layerwise_retrieve_without_a_generation_is_refused(make_harness) -> None:
    """Generation 0 means "not layerwise"; no copy runs under it."""
    h = make_harness()

    assert h.retrieve(generation=0) is False

    assert h.layerwise_calls == []
    assert h.group_transfers == []
