# SPDX-License-Identifier: Apache-2.0
"""LMCache-driven KV cache transfer operations for the MPCacheServer."""

# Standard
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from typing import Any, Sequence
import threading
import time

# Third Party
import torch

# First Party
from lmcache import torch_dev
from lmcache.logging import init_logger
from lmcache.utils import (
    EngineType,
    _lmcache_nvtx_annotate,
)
from lmcache.v1.distributed.api import (
    MemoryLayoutDesc,
    ObjectKey,
)
from lmcache.v1.gpu_connector.utils import LayoutHints
from lmcache.v1.kv_layer_groups import ObjectGroupInfo
from lmcache.v1.layerwise import LayerwiseContractError
from lmcache.v1.layerwise.deferral import PipelinedModel
from lmcache.v1.layerwise.pipelined_retrieve import (
    SharedKeysBusyError,
    resolve_shared_keys,
)
from lmcache.v1.layerwise.planner import ModelLayout
from lmcache.v1.layerwise.request_fetch import (
    FetchModel,
    FetchModelRegistry,
    first_in_window_chunk,
)
from lmcache.v1.memory_management import MemoryObj
from lmcache.v1.mp_observability.event import Event, EventType, next_transfer_key
from lmcache.v1.multiprocess.custom_types import (
    IPCCacheServerKey,
    KVCache,
    RegisterKvCacheResponse,
)
from lmcache.v1.multiprocess.deferred_response import DeferredResponse
from lmcache.v1.multiprocess.engine_context import MPCacheServerContext
from lmcache.v1.multiprocess.engine_module import InstanceLivenessTarget
from lmcache.v1.multiprocess.group_view import EngineGroupInfo
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
    attach_layer_progress_shm,
)
from lmcache.v1.multiprocess.layerwise_schedule import (
    LayerwiseSchedule,
    assert_registration_schedules_agree,
)
from lmcache.v1.multiprocess.modules.lookup import resolve_prefetched_obj_keys
from lmcache.v1.multiprocess.native_completion import (
    DeviceHostFuncDispatcher,
    submit_callback_to_stream,
)
from lmcache.v1.multiprocess.object_group_transfer import (
    downsample_and_stage_block_ids,
    downsample_and_stage_owned_block_ids,
    per_layer_staging_ranges,
    transfer_kv_layerwise_h2d,
    transfer_kv_per_object_group,
)
from lmcache.v1.multiprocess.pipelined_loading import (
    NO_PIPELINED_SINK_FACTORY,
    DeferredLoad,
    ObjectTable,
    PipelinedLoadRequest,
    PipelinedOutcome,
    PipelinedSinkFactory,
    check_staging_matches_plan,
    fetch_deferred_objects,
)
from lmcache.v1.multiprocess.protocols.base import HandlerType, RequestType
from lmcache.v1.multiprocess.request_handler import request_handler
from lmcache.v1.multiprocess.retrieve_sequencer import RetrieveLaunchSequencer
from lmcache.v1.platform.base.cache_context import BaseCacheContext
from lmcache.v1.platform.base.event_ipc import (
    EventIPCBackend,
    get_event_ipc_backend,
)
from lmcache.v1.platform.cache_context import create_cache_context
import lmcache.lmcache_native as lmcache_native

logger = init_logger(__name__)

#: Threads that run layerwise retrieves after their handler returned. Each
#: blocks while its layers arrive; one per retrieve in flight across workers.
#: The pool must start retrieves in submission order: a running retrieve
#: waits for older ones of its worker, which must not be queued behind it.
_LAYERWISE_RETRIEVE_THREADS = 32


def get_layout_desc(
    cache_context: BaseCacheContext,
    num_tokens: int,
    object_group_id: int,
) -> MemoryLayoutDesc:
    """Get the memory layout description for a specific object group.

    The returned layout describes the single memory object that backs
    ``object_group_id``: one (shape, dtype) entry per kernel group in that
    object group, in the kernel groups' declared layout order. Kernel groups
    may have different shapes and dtypes.

    Args:
        cache_context: The cache context containing the KV cache information.
        num_tokens: The number of tokens to determine the layout for.
        object_group_id: Index of the object group whose layout to build.

    Returns:
        MemoryLayoutDesc: The memory layout description containing shapes and
        dtypes, one entry per kernel group in the object group.
    """
    object_group = cache_context.kv_layer_groups_manager.object_groups[object_group_id]
    shapes_and_dtypes = [
        cache_context.get_kernel_group_shape_dtype(num_tokens, kernel_group_idx)
        for kernel_group_idx in object_group.kernel_group_indices
    ]
    shapes, dtypes = zip(*shapes_and_dtypes, strict=False)
    return MemoryLayoutDesc(shapes=list(shapes), dtypes=list(dtypes))


def uniform_kv_plane_bytes(layout_descs: Iterable[MemoryLayoutDesc]) -> int:
    """Size in bytes of one K/V plane, when every kernel group agrees on it.

    A "plane" is one model layer's bytes within one of the K/V halves. In
    every layout this module emits, the two innermost dimensions are
    ``(num_slots, hidden_dim)`` -- the standard shape is
    ``(kv_size, num_layers, num_slots, hidden_dim)`` and the
    ``NL_X_NB_BS_HS`` variant drops the leading ``kv_size`` -- so the plane
    size is the product of the last two dimensions and the element size.

    Storage backends use this to align record boundaries to planes, which
    keeps a record within a single model layer. That is only expressible as
    one number while the kernel groups agree; compressed and recurrent groups
    generally do not, since their slot counts differ from the token count.

    Args:
        layout_descs: Layouts to inspect, typically one per object group, as
            built by ``get_layout_desc``.

    Returns:
        The shared plane size in bytes, or 0 if the layouts disagree, carry a
        shape with fewer than two dimensions, or are empty. Zero means "no
        single plane size describes this model", which callers should treat as
        "do not align".
    """
    plane_sizes = set()
    for desc in layout_descs:
        for shape, dtype in zip(desc.shapes, desc.dtypes, strict=True):
            if len(shape) < 2:
                return 0
            plane_sizes.add(shape[-2] * shape[-1] * dtype.itemsize)
    if len(plane_sizes) != 1:
        return 0
    return plane_sizes.pop()


def all_null_chunk_masks(
    block_ids: Sequence[Sequence[int]],
    object_groups: Sequence[ObjectGroupInfo],
    blocks_per_chunk: Sequence[int],
    num_chunks: int,
) -> list[list[bool]]:
    """Mark, per object group, the chunks whose engine block ids are all null.

    A chunk is null for an object group when every block id of every kernel
    group in that group is 0 (the vLLM null block). Align-mode Mamba/linear
    layers produce such chunks: only the block holding the last recurrent state
    is real, so every earlier chunk is null. These chunks must not be stored --
    the null block carries no valid KV, and object keys are content hashes, so
    committing them would serve garbage to a later prefix hit.

    Args:
        block_ids: Raw per-kernel-group engine block ids (before any downsample),
            indexed by kernel-group index.
        object_groups: The object groups, indexed by object-group id.
        blocks_per_chunk: Blocks in one chunk per kernel group, indexed by
            kernel-group index.
        num_chunks: Number of chunks in the request.

    Returns:
        ``mask[g][i]`` is True iff chunk ``i`` is all-null for object group ``g``.
    """
    masks: list[list[bool]] = []
    for group in object_groups:
        chunk_null: list[bool] = []
        for i in range(num_chunks):
            is_null = True
            for kg in group.kernel_group_indices:
                bpc = blocks_per_chunk[kg]
                if any(block_ids[kg][i * bpc : (i + 1) * bpc]):
                    is_null = False
                    break
            chunk_null.append(is_null)
        masks.append(chunk_null)
    return masks


def _publish_layerwise_retrieve_terminal(
    ctx: MPCacheServerContext,
    entry: "ContextEntry | None",
    instance_id: int,
    retrieve_generation: int,
) -> None:
    """Publish that layerwise retrieve ``retrieve_generation`` failed.

    A registered worker's failure goes through its
    :class:`RetrieveLaunchSequencer`, which first stops the worker's other
    in-flight retrieves. Without a registration nothing of this daemon is in
    flight for the worker, so the record is written directly with
    :meth:`LayerProgressRecord.fail_retrieve`, which never touches a newer
    retrieve's record.
    """
    if not ctx.use_layerwise or retrieve_generation <= 0:
        return
    sequencer = getattr(entry, "retrieve_sequencer", None)
    if sequencer is not None:
        sequencer.fail(retrieve_generation)
        return
    progress = entry.layer_progress if entry is not None else None
    shm_to_close: shared_memory.SharedMemory | None = None
    if progress is None:
        try:
            shm_to_close = attach_layer_progress_shm(instance_id)
            progress = LayerProgressRecord.from_shared_memory(shm_to_close)
        except FileNotFoundError:
            return
    try:
        progress.fail_retrieve(retrieve_generation)
    finally:
        if shm_to_close is not None:
            shm_to_close.close()


def _sequencer_of(entry: "ContextEntry") -> RetrieveLaunchSequencer:
    """Return a layerwise worker's launch sequencer.

    Raises:
        RuntimeError: If the worker registered without layerwise state.
    """
    if entry.retrieve_sequencer is None:
        raise RuntimeError("layerwise retrieve on a worker with no launch sequencer")
    return entry.retrieve_sequencer


def _event_backend_of(entry: "ContextEntry") -> EventIPCBackend:
    """Return a registered worker's event backend.

    Raises:
        RuntimeError: If the registration has none.
    """
    if entry.event_backend is None:
        raise RuntimeError("Registered cache context has no event backend")
    return entry.event_backend


@dataclass(frozen=True)
class _DeferredKeys:
    """What a retrieve does with the deferred keys its session handed it.

    ``to_fetch`` are fetched layer by layer. ``locked`` are the keys this
    retrieve read-locked itself (reused from another fetch, or loaded whole
    up front); they are read with the L1 part, and released by the retrieve
    even if it fails before reading them.
    """

    to_fetch: tuple[ObjectKey, ...] = ()
    locked: tuple[ObjectKey, ...] = ()
    #: The outcome when nothing is left to fetch layer by layer.
    outcome: PipelinedOutcome = PipelinedOutcome.NOT_DEFERRED


@dataclass
class ContextEntry:
    """Registered cache context metadata for a single worker instance.

    The concrete type is whatever :func:`create_cache_context` returned
    for the wrapper list at registration time -- a
    :class:`GPUCacheContext` for CUDA-IPC wrappers, a
    :class:`CPUCacheContext` for POSIX-SHM wrappers. Both expose
    the same ``kv_tensors`` / ``engine_kv_format`` / ``num_layers`` / ...
    duck-typed surface, so downstream consumers stay agnostic.

    Args:
        cache_context: Platform cache context (GPU or CPU) managing
            shape and pointers to the registered KV cache tensors.
        model_name: The name of the model associated with this KV cache.
        world_size: The world size associated with this KV cache.
        last_seen: ``time.monotonic()`` of the most recent activity from
            this instance (register, PING, store, or retrieve). Drives reaping.
        has_liveness_signal: True once the instance has sent at least one
            PING. Selects the reap window (timeout vs registration grace).
            Latched only by PING, never by traffic.
        event_backend: Cached event backend selected for this context's device.
        layerwise_schedule: Per-layer launch order when layerwise MP load is enabled.
        layer_progress: Shared progress record for layerwise retrieve waits.
        daemon_layer_event_pool: Daemon-owned IPC events recorded per ordinal.
        layer_progress_shm: Attached shared-memory segment backing ``layer_progress``.
        retrieve_sequencer: Orders the launches of this worker's in-flight
            layerwise retrieves and is the only writer of ``layer_progress``
            and ``daemon_layer_event_pool``. Set when layerwise is on.
    """

    cache_context: BaseCacheContext
    model_name: str
    world_size: int
    last_seen: float = 0.0
    has_liveness_signal: bool = False
    event_backend: EventIPCBackend | None = None
    layerwise_schedule: LayerwiseSchedule | None = None
    layer_progress: LayerProgressRecord | None = None
    daemon_layer_event_pool: DaemonLayerLaunchEventPool | None = None
    layer_progress_shm: shared_memory.SharedMemory | None = None
    retrieve_sequencer: RetrieveLaunchSequencer | None = None


@dataclass
class _RetrieveRun:
    """One retrieve's state, from its handler to wherever it finishes.

    A layerwise retrieve starts on the client's affinity thread and finishes
    on the layerwise retrieve pool; the fields carry what the finish needs.
    """

    key: IPCCacheServerKey
    instance_id: int
    entry: ContextEntry
    event: object
    transfer_key: str
    started: float
    num_chunks: int
    expected_retained: int
    skip_first_n_tokens: int
    retrieve_generation: int
    obj_keys_per_obj_group: list[list[ObjectKey]]
    block_ids_per_group_gpu: list[torch.Tensor]
    layerwise_active: bool
    memory_objs_by_group: list[list[MemoryObj | None]]
    prefetched_keys: list[ObjectKey] = field(default_factory=list)
    total_bytes: int = 0
    succeeded: bool = True
    deferred: _DeferredKeys = field(default_factory=_DeferredKeys)
    claimed: tuple[ObjectKey, ...] = ()
    outcome: PipelinedOutcome = PipelinedOutcome.NOT_DEFERRED
    fetched_in_window: tuple[ObjectKey, ...] = ()


class LMCacheDrivenTransferModule(InstanceLivenessTarget):
    """Handles LMCache-driven KV cache transfer operations.

    Owns GPU context registrations and provides handlers for
    register, unregister, store, and retrieve of GPU KV caches.

    Args:
        ctx: The shared engine context.
        pipelined_sink_factory: Builds the sink of a pipelined retrieve.
            Without one, no model is registered for the pipelined fetch, so
            lookups never defer, even with ``--pipelined-fetch`` on.
    """

    def __init__(
        self,
        ctx: MPCacheServerContext,
        pipelined_sink_factory: PipelinedSinkFactory = NO_PIPELINED_SINK_FACTORY,
    ) -> None:
        self._ctx = ctx
        self._pipelined_sink_factory = pipelined_sink_factory
        self._cache_contexts: dict[int, ContextEntry] = {}
        # Guards all reads/writes of _cache_contexts. The reaper mutates it
        # off the MQ main loop, so register/unregister/store/retrieve and
        # report_status all serialize through this lock. Held only for dict
        # ops -- never across context creation, layout-registry calls, or
        # empty_cache (leaf-lock invariant: no thread holds two locks).
        self._lock = threading.Lock()
        self._fetch_models = FetchModelRegistry()
        self._layerwise_retrieve_pool = ThreadPoolExecutor(
            max_workers=_LAYERWISE_RETRIEVE_THREADS,
            thread_name_prefix="lmcache-layerwise-retrieve",
        )

        # Route finish_write / finish_read_prefetched through a C++ host
        # callback so the driver thread doesn't acquire the GIL.
        self._device_host_func_dispatcher = DeviceHostFuncDispatcher()
        self._device_host_func_dispatcher.register(
            "finish_write",
            self._ctx.storage_manager.finish_write,
            payload_type=list[ObjectKey],
        )
        self._device_host_func_dispatcher.register(
            "finish_read_prefetched",
            self._ctx.storage_manager.finish_read_prefetched,
            payload_type=list[ObjectKey],
        )
        self._device_host_func_dispatcher.start()

    def register_host_func(self, kind: str, handler: Any, payload_type: Any) -> None:
        """Register *handler* for *kind* on the per-process device host-func
        dispatcher (stream-ordered callbacks without a driver-thread GIL
        acquire); pair with ``submit_callback_to_stream``."""
        self._device_host_func_dispatcher.register(kind, handler, payload_type)

    @property
    def context(self) -> MPCacheServerContext:
        """Return the shared engine context. Exposed for testing only."""
        return self._ctx

    def fetch_model(self, model_name: str, world_size: int) -> FetchModel:
        """Return what a layerwise fetch plan needs from a registered model.

        Built once at ``register_kv_cache`` from the layouts published to
        storage, and released with the model's last registration.

        Args:
            model_name: The model name.
            world_size: The world size.

        Returns:
            The model's fetch layout and attention windows.

        Raises:
            KeyError: If the model is not registered or its layout cannot be
                planned layer by layer.
        """
        return self._fetch_models.find(model_name, world_size)

    def get_and_touch_context_entry(self, instance_id: int) -> ContextEntry | None:
        """Return the entry for ``instance_id``, refreshing its last-seen time.

        The refresh keeps an actively transferring worker from being reaped
        even if its PINGs are briefly delayed. Does not latch the
        ping-proven flag -- only PINGs do that.

        Args:
            instance_id: The worker instance ID.

        Returns:
            The entry, or None if the instance is not (or no longer) tracked.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._cache_contexts.get(instance_id)
            if entry is not None:
                entry.last_seen = now
            return entry

    def _release_failed_retrieve_locks(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
    ) -> None:
        """Release only one failed instance's unconsumed lookup locks.

        The lookup session is the ownership record.  If it is absent or does
        not match the RETRIEVE range, no release is attempted: L1 locks are
        anonymous refcounts, so guessing could consume a concurrent reader's
        lock.  ``claim_failed_retrieve_release`` also makes duplicate failure
        responses idempotent.
        """
        session = self._ctx.session_manager.get(key.request_id)
        if session is None:
            logger.warning(
                "Cannot release RETRIEVE locks for unregistered instance %d: "
                "request %s has no lookup session",
                instance_id,
                key.request_id,
            )
            return

        lock_state = session.prepare_failed_retrieve_release(key)
        if lock_state is None:
            return
        hit_chunks, locked_gids, group_windows, lookup_generation = lock_state
        obj_keys = resolve_prefetched_obj_keys(
            self._ctx,
            key,
            hit_chunks,
            locked_gids,
            group_windows=group_windows,
        )
        deferred = session.deferred_keys()
        if deferred:
            obj_keys = [obj_key for obj_key in obj_keys if obj_key not in deferred]
        if not session.claim_failed_retrieve_release(
            instance_id, key, lookup_generation
        ):
            return
        if obj_keys:
            # One failed RETRIEVE owns one read lock per key.  In
            # particular, do not release the scheduler's whole MLA
            # reservation here: the remaining TP workers and concurrent
            # requests still own their independent read locks.
            self._ctx.storage_manager.finish_read_prefetched(obj_keys, read_locks=1)

    def context_entries_snapshot(self) -> dict[int, ContextEntry]:
        """Return a shallow copy of the registry for iteration or status.

        Returns:
            A new dict mapping instance ID to entry; does not refresh
            last-seen times.
        """
        with self._lock:
            return dict(self._cache_contexts)

    def touch_instance(self, instance_id: int) -> bool:
        """Refresh the worker's last-seen time and mark it ping-proven.

        A no-op if the instance is not tracked.

        Args:
            instance_id: The worker instance ID.

        Returns:
            Whether the instance has a registered KV cache here.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._cache_contexts.get(instance_id)
            if entry is None:
                return False
            entry.last_seen = now
            entry.has_liveness_signal = True
            return True

    def tracked_instance_count(self) -> int:
        """Return the number of currently registered instances."""
        with self._lock:
            return len(self._cache_contexts)

    def reap_stale_instances(
        self, reap_timeout_s: float, registration_grace_s: float
    ) -> list[int]:
        """Reap GPU registrations that have gone silent.

        A ping-proven instance is judged against ``reap_timeout_s``; one
        that has never pinged against the larger ``registration_grace_s``.

        Args:
            reap_timeout_s: Silence budget for ping-proven instances.
            registration_grace_s: Silence budget for never-pinged instances.

        Returns:
            The instance IDs reaped this scan.
        """
        now = time.monotonic()
        reaped: list[tuple[int, ContextEntry]] = []
        with self._lock:
            stale_ids = [
                iid
                for iid, entry in self._cache_contexts.items()
                if now - entry.last_seen
                > (
                    reap_timeout_s
                    if entry.has_liveness_signal
                    else registration_grace_s
                )
            ]
            for iid in stale_ids:
                reaped.append((iid, self._cache_contexts.pop(iid)))
        reaped_ids: list[int] = []
        entries: list[ContextEntry] = []
        for iid, e in reaped:
            logger.warning(
                "Reaped GPU instance %d: silent for %.1fs (pinged=%s)",
                iid,
                now - e.last_seen,
                e.has_liveness_signal,
            )
            reaped_ids.append(iid)
            entries.append(e)
        if reaped:
            del e  # a bound name would pin the final entry (see _release_entries)
            reaped.clear()
            self._release_entries(entries)
        return reaped_ids

    def _release_entries(self, entries: list[ContextEntry]) -> None:
        """Release a batch of entries and reclaim their device memory.

        Args:
            entries: The only remaining references to the released entries.
                The list is cleared before memory is reclaimed.
        """
        if not entries:
            return
        for entry in entries:
            if entry.layer_progress_shm is not None:
                entry.layer_progress_shm.close()
                entry.layer_progress_shm = None
            entry.cache_context.close()
            self._ctx.layout_desc_registry.unregister(
                entry.model_name, entry.world_size
            )
            self._fetch_models.unregister(entry.model_name, entry.world_size)
            self._ctx.pipelined_models.unregister(entry.model_name, entry.world_size)
        del entry
        entries.clear()
        # ipc_collect() only unmaps a CUDA-IPC-imported segment once its last
        # tensor reference is gone (LMCache#4014), hence the clear() above.
        torch_dev.empty_cache()
        ipc_collect = getattr(torch_dev, "ipc_collect", None)
        if ipc_collect is not None:
            # Backends without IPC collection omit this optional operation.
            ipc_collect()

    def report_status(self) -> dict:
        """Return GPU transfer module status information.

        Returns:
            A dict containing registered GPU instance IDs and
            per-instance KV cache layout metadata.
        """
        registered_gpu_ids: list[int] = []
        cache_context_meta: dict[str, dict] = {}

        for instance_id, entry in self.context_entries_snapshot().items():
            registered_gpu_ids.append(instance_id)
            ctx = entry.cache_context
            cache_context_meta[str(instance_id)] = {
                "model_name": entry.model_name,
                "world_size": entry.world_size,
                "kv_cache_layout": ctx.report_status(),
            }

        return {
            "registered_gpu_ids": registered_gpu_ids,
            "cache_context_meta": cache_context_meta,
        }

    def close(self) -> None:
        """Release GPU resources owned by this module."""
        # Retrieves still loading layers enqueue completions and touch the
        # cache contexts, so they finish before either goes away.
        self._layerwise_retrieve_pool.shutdown(wait=True)
        # Stop the drain thread before storage_manager.close() so any
        # in-flight completions reach a live storage manager.
        self._device_host_func_dispatcher.stop()

        with self._lock:
            entries = list(self._cache_contexts.values())
            self._cache_contexts.clear()
        self._release_entries(entries)

    @request_handler(RequestType.REGISTER_KV_CACHE)
    def register_kv_cache(
        self,
        instance_id: int,
        kv_caches: KVCache,
        model_name: str,
        world_size: int,
        engine_type: EngineType,
        layout_hints: LayoutHints,
        engine_group_infos: list[EngineGroupInfo],
    ) -> RegisterKvCacheResponse:
        """Register the KV cache tensors for a given GPU instance ID.

        Args:
            instance_id: The GPU instance ID (such as PID).
            kv_caches: The KV cache tensor wrappers from the
                serving engine.
            model_name: The name of the model associated with this KV cache.
            world_size: The world size associated with this KV cache.
            engine_type: Which serving engine produced the caches.
                Forwarded to GPUCacheContext for format detection.
            layout_hints: See LayoutHints.  Forwarded to
                GPUCacheContext for GPU KV format detection.
            engine_group_infos: Engine-neutral KV cache group metadata
                (already msgspec-decoded by the message queue).

        Returns:
            Registration metadata including server layerwise flag and optional
            exported layer event handles.
        """
        now = time.monotonic()
        # NOOP-register: an already-registered instance (e.g. a recovering
        # worker re-registering on its first ping) refreshes its last-seen
        # time so a stale entry is not reaped right after recovery. REGISTER
        # is SYNC-serialized on the MQ main loop, so it is the sole inserter.
        with self._lock:
            existing = self._cache_contexts.get(instance_id)
            if existing is not None:
                existing.last_seen = now
                logger.info(
                    "Instance %d already registered; refreshing liveness",
                    instance_id,
                )
                response_handles: list[bytes] = []
                if (
                    existing.daemon_layer_event_pool is not None
                    and existing.event_backend is not None
                ):
                    response_handles = existing.daemon_layer_event_pool.export_handles(
                        existing.cache_context.device
                    )
                return RegisterKvCacheResponse(
                    server_use_layerwise=self._ctx.use_layerwise,
                    layer_event_ipc_handles=response_handles,
                    layer_publish_budget_seconds=(
                        self._ctx.pipelined_fetch.layer_publish_budget_seconds
                    ),
                )

        # Build the context and layout descriptor outside the lock.
        cache_context = create_cache_context(
            kv_caches,
            self._ctx.chunk_size,
            layout_hints=layout_hints or None,
            engine_group_infos=engine_group_infos,
            engine_type=engine_type,
            separate_object_groups=self._ctx.separate_object_groups,
            full_sw_kv=self._ctx.full_sw_kv,
        )
        kv_groups_manager = cache_context.kv_layer_groups_manager
        num_object_groups = kv_groups_manager.num_object_groups
        event_backend = get_event_ipc_backend(cache_context.device)
        event_backend.check_event_support(cache_context.device)
        layout_desc = get_layout_desc(
            cache_context, self._ctx.chunk_size, object_group_id=0
        )
        # One layout per object group, also in the single-group case: no
        # None special-casing downstream (group 0 maps to the merged layout).
        group_layout_descs = {
            gid: get_layout_desc(
                cache_context, self._ctx.chunk_size, object_group_id=gid
            )
            for gid in range(num_object_groups)
        }
        attn_desc = kv_groups_manager.get_attn_desc()
        self._ctx.layout_desc_registry.register(
            model_name,
            world_size,
            layout_desc,
            attn_desc,
            group_layout_descs=group_layout_descs,
        )
        group_kernel_layer_indices = {
            gid: [
                list(kv_groups_manager.kernel_groups[kernel_index].layer_indices)
                for kernel_index in object_group.kernel_group_indices
            ]
            for gid, object_group in enumerate(kv_groups_manager.object_groups)
        }
        try:
            self._ctx.storage_manager.set_object_group_layouts(
                group_layout_descs, group_kernel_layer_indices
            )
        except ValueError:
            # Storage then shards this model's records by byte count, as it
            # did before layouts were published: stores and whole-object
            # loads are unaffected, only layer-at-a-time fetch is lost.
            logger.warning(
                "Storage could not use the KV layout of %s; records will not "
                "follow layer boundaries",
                model_name,
                exc_info=True,
            )
        try:
            fetch_layout = ModelLayout.from_registration(
                group_layout_descs, group_kernel_layer_indices
            )
        except ValueError:
            logger.warning(
                "Cannot plan layerwise fetches for %s; its retrieves load "
                "whole objects",
                model_name,
                exc_info=True,
            )
        else:
            fetch_model = FetchModel(fetch_layout, attn_desc)
            self._fetch_models.register(model_name, world_size, fetch_model)
            if self._ctx.pipelined_fetch.enabled:
                self._register_pipelined_model(
                    model_name,
                    world_size,
                    fetch_model,
                    group_layout_descs,
                    cache_context,
                )

        layerwise_schedule: LayerwiseSchedule | None = None
        layer_progress: LayerProgressRecord | None = None
        layer_progress_shm: shared_memory.SharedMemory | None = None
        daemon_layer_event_pool: DaemonLayerLaunchEventPool | None = None
        retrieve_sequencer: RetrieveLaunchSequencer | None = None
        response_handles = []
        if self._ctx.use_layerwise:
            engine_layers = [list(group.layer_indices) for group in engine_group_infos]
            if any(engine_layers):
                assert_registration_schedules_agree(
                    engine_layers,
                    kv_groups_manager.kernel_groups,
                )
            layerwise_schedule = LayerwiseSchedule.from_kernel_groups(
                kv_groups_manager.kernel_groups
            )
            launch_count = layerwise_schedule.launch_count()
            daemon_events = [
                event_backend.create_event(cache_context.device)
                for _ in range(launch_count)
            ]
            daemon_layer_event_pool = DaemonLayerLaunchEventPool(
                daemon_events,
                event_backend,
                launch_count,
            )
            response_handles = daemon_layer_event_pool.export_handles(
                cache_context.device
            )
            layer_progress_shm = attach_layer_progress_shm(instance_id)
            layer_progress = LayerProgressRecord.from_shared_memory(layer_progress_shm)
            retrieve_sequencer = RetrieveLaunchSequencer(
                cache_context.stream,
                layer_progress,
                daemon_layer_event_pool,
                cache_context.transfer_gate,
            )

        with self._lock:
            self._cache_contexts[instance_id] = ContextEntry(
                cache_context=cache_context,
                model_name=model_name,
                world_size=world_size,
                last_seen=now,
                has_liveness_signal=False,
                event_backend=event_backend,
                layerwise_schedule=layerwise_schedule,
                layer_progress=layer_progress,
                daemon_layer_event_pool=daemon_layer_event_pool,
                layer_progress_shm=layer_progress_shm,
                retrieve_sequencer=retrieve_sequencer,
            )

        logger.info(
            "Registered KV cache for GPU ID %d with %d layers",
            instance_id,
            cache_context.num_layers,
        )
        return RegisterKvCacheResponse(
            server_use_layerwise=self._ctx.use_layerwise,
            layer_event_ipc_handles=response_handles,
            layer_publish_budget_seconds=(
                self._ctx.pipelined_fetch.layer_publish_budget_seconds
            ),
        )

    @request_handler(RequestType.UNREGISTER_KV_CACHE)
    def unregister_kv_cache(self, instance_id: int) -> None:
        """Unregister the KV cache tensors for a given GPU instance ID.

        Args:
            instance_id: The GPU instance ID (such as PID).
        """
        with self._lock:
            popped = [
                e
                for e in (self._cache_contexts.pop(instance_id, None),)
                if e is not None
            ]
        if not popped:
            logger.warning(
                "No registered GPU context found for instance ID %d", instance_id
            )
            return

        # No scalar binding: `popped` must stay the only reference so
        # _release_entries' reclaim actually unmaps the IPC segments.
        self._release_entries(popped)
        logger.info("Unregistered KV cache for GPU ID %d", instance_id)

    @request_handler(
        RequestType.STORE,
        HandlerType.BLOCKING,
        requires_client_affinity=True,
    )
    @_lmcache_nvtx_annotate
    def store(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
        gpu_block_ids: list[list[int]],
        event_ipc_handle: bytes,
    ) -> tuple[bytes, bool]:
        """Store the GPU KV cache blocks to CPU.

        Args:
            key: The IPC key for the KV cache blocks.
                Must have worker_id != None (worker store operation).
            instance_id: The GPU instance ID (such as PID).
            gpu_block_ids: GPU block IDs to store, indexed by LMCache KV
                group index.
            event_ipc_handle: The IPC handle of the event to wait on.

        Returns:
            A tuple where the first element is the IPC handle of the event
            that signals the completion of the store operation, and the second
            element indicates whether the store operation completed without a
            fatal error (not whether every requested chunk was stored; see
            Notes). The event handle is empty when no device work was submitted.

        Raises:
            RuntimeError: If the backend does not support IPC event handles.

        Notes:
            All-or-nothing. If ``gpu_block_ids`` do not fully cover every chunk
            ``key`` resolves to for every LMCache group (e.g. a caller/protocol
            bug), or a copy fails, the whole store is skipped and nothing is
            committed (logged at WARNING); a subsequent retrieve simply misses
            and the engine recomputes. The boolean result reports whether the
            store completed without such a failure.
        """
        st = time.perf_counter()

        entry = self.get_and_touch_context_entry(instance_id)
        if entry is None:
            # The worker can reconnect to a replacement server before its next
            # registration probe. No device work was submitted in that window,
            # so return an empty completion-event handle and a terminal False
            # response instead of leaving the MQ future unanswered. Echoing the
            # producer handle would make the originating process import its own
            # IPC event, which is invalid on HIP.
            logger.warning(
                "Rejecting STORE for unregistered GPU instance ID %d",
                instance_id,
            )
            return b"", False
        cache_context = entry.cache_context
        model_name = entry.model_name
        event_backend = entry.event_backend
        if event_backend is None:
            raise RuntimeError("Registered cache context has no event backend")

        num_object_groups = cache_context.kv_layer_groups_manager.num_object_groups
        obj_keys_per_obj_group = self._ctx.resolve_obj_keys(
            key, list(range(num_object_groups))
        )
        num_chunks = len(obj_keys_per_obj_group[0])

        # NOTE: different engine groups may have different block sizes, so
        # ``blocks_per_chunk[i]`` is the number of blocks in one chunk for
        # group ``i``.
        blocks_per_chunk = [
            cache_context.calculate_num_blocks(self._ctx.chunk_size, group_idx)
            for group_idx in range(
                cache_context.kv_layer_groups_manager.num_kernel_groups
            )
        ]

        with (
            torch_dev.device(cache_context.device),
            torch_dev.stream(cache_context.stream),
        ):
            event = event_backend.create_event(cache_context.device)

            # Fail closed: every LMCache group must have block IDs covering all
            # chunks. A short list (e.g. a caller/protocol bug) would otherwise
            # drive the transfer kernel to read out-of-bounds GPU memory, so skip
            # the whole store and commit nothing rather than caching a partial or
            # garbage entry. A later request can store it once the block IDs are
            # complete. Checked on the raw block ids, before cutting drops the
            # per-chunk blocks that sliding-window groups do not need.
            if any(
                len(group_block_ids) < num_chunks * bpc
                for group_block_ids, bpc in zip(
                    gpu_block_ids, blocks_per_chunk, strict=True
                )
            ):
                logger.warning(
                    "STORE block ID underflow for request_id=%s: each group needs "
                    "num_chunks * blocks_per_chunk block IDs for %d chunks "
                    "(per-group blocks_per_chunk=%s); skipping the store.",
                    key.request_id,
                    num_chunks,
                    blocks_per_chunk,
                )
                event_backend.record_event(event, cache_context.stream)
                return event_backend.export_event(event, cache_context.device), False

            # Chunks whose block ids are all the null block (e.g. align-mode
            # Mamba chunks holding no real state) carry no valid KV and must not
            # be committed. Computed on the raw block ids before downsampling
            # mutates them.
            skipped_chunks = all_null_chunk_masks(
                gpu_block_ids,
                cache_context.kv_layer_groups_manager.object_groups,
                blocks_per_chunk,
                num_chunks,
            )

            block_ids_per_group_gpu = downsample_and_stage_block_ids(
                cache_context, gpu_block_ids
            )

            producer_event = event_backend.import_event(
                event_ipc_handle, cache_context.device
            )
            event_backend.wait_event(producer_event, cache_context.stream)

            # CPU-synchronous sentinel: a GPU store is about to be enqueued.
            # Must be published via publish() (not publish_on_stream) so the
            # drain thread sees it before MP_REQUEST_END can race MP_STORE_END.
            self._ctx.event_bus.publish(
                Event(
                    event_type=EventType.MP_STORE_SUBMITTED,
                    session_id=key.request_id,
                    metadata={"device": str(cache_context.device)},
                )
            )

            # Worker 0 only: bindings depend on token content alone, so one
            # report covers every rank's keys. Published before finish_write
            # is enqueued so the token bindings precede the write-finished
            # events on the bus.
            if key.worker_id == 0 and self._ctx.event_bus.has_subscribers(
                EventType.MP_TOKENS
            ):
                self._publish_token_bindings(key, obj_keys_per_obj_group[0])

            transfer_key = next_transfer_key(key.request_id)
            self._ctx.event_bus.publish_on_stream(
                cache_context.cupy_stream,
                Event(
                    event_type=EventType.MP_STORE_START,
                    session_id=key.request_id,
                    metadata={
                        "device": str(cache_context.device),
                        "engine_id": instance_id,
                        "model_name": model_name,
                        "transfer_key": transfer_key,
                    },
                ),
            )

            reserved_dict: dict[ObjectKey, MemoryObj] = {}
            all_dict: dict[ObjectKey, MemoryObj] = {}
            total_bytes: int = 0
            store_succeeded = False
            try:
                for obj_group_id in range(num_object_groups):
                    obj_keys = obj_keys_per_obj_group[obj_group_id]
                    skip_mask = skipped_chunks[obj_group_id]
                    keys_to_reserve = [
                        k for i, k in enumerate(obj_keys) if not skip_mask[i]
                    ]
                    layout_desc = get_layout_desc(
                        cache_context,
                        self._ctx.chunk_size,
                        object_group_id=obj_group_id,
                    )
                    reserved_dict = self._ctx.storage_manager.reserve_write(
                        keys_to_reserve, layout_desc, "new"
                    )
                    all_dict.update(reserved_dict)
                    if reserved_dict:
                        total_bytes += next(
                            iter(reserved_dict.values())
                        ).get_size() * len(reserved_dict)

                    # Keys not in reserved_dict (all-null chunks skipped above, or
                    # skipped by the storage manager) become None entries; the
                    # helper skips them for D2H.
                    memory_objs: list[MemoryObj | None] = [
                        reserved_dict.get(obj_key) for obj_key in obj_keys
                    ]

                    # NOTE: batch_size must stay 1 for store.
                    with cache_context.transfer_gate.hold():
                        transfer_kv_per_object_group(
                            cache_context,
                            block_ids_per_group_gpu,
                            memory_objs,
                            object_group_id=obj_group_id,
                            batch_size=1,
                            skip_first_n_tokens=0,
                            direction=lmcache_native.TransferDirection.D2H,
                            transfer_key=transfer_key,
                        )

                store_succeeded = True
            except Exception:
                logger.exception("Cannot store keys due to exception")
            finally:
                event_backend.record_event(event, cache_context.stream)
                # Fail closed: commit the reserved objects only when every chunk
                # copied successfully; otherwise the whole store is skipped.
                stored_count = len(all_dict) if store_succeeded else 0
                if stored_count:
                    submit_callback_to_stream(
                        cache_context.cupy_stream,
                        "finish_write",
                        list(all_dict.keys()),
                    )
                else:
                    total_bytes = 0
                num_tokens = num_chunks * self._ctx.chunk_size if stored_count else 0
                self._ctx.event_bus.publish_on_stream(
                    cache_context.cupy_stream,
                    Event(
                        event_type=EventType.MP_STORE_END,
                        session_id=key.request_id,
                        metadata={
                            "stored_count": stored_count,
                            "device": str(cache_context.device),
                            "engine_id": instance_id,
                            "model_name": model_name,
                            "total_bytes": total_bytes,
                            "num_tokens": num_tokens,
                            "transfer_key": transfer_key,
                        },
                    ),
                )

        ed = time.perf_counter()
        if stored_count:
            logger.info(
                "Stored %d tokens in %.3f seconds",
                num_chunks * self._ctx.chunk_size,
                ed - st,
            )
        return (
            event_backend.export_event(event, cache_context.device),
            store_succeeded,
        )

    @request_handler(
        RequestType.RETRIEVE,
        HandlerType.BLOCKING,
        requires_client_affinity=True,
    )
    @_lmcache_nvtx_annotate
    def start_retrieve(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
        gpu_block_ids: list[list[int]],
        event_ipc_handle: bytes,
        skip_first_n_tokens: int = 0,
        retrieve_generation: int = 0,
    ) -> DeferredResponse[tuple[bytes, bool]]:
        """Retrieve the CPU KV cache into GPU blocks; the RETRIEVE handler.

        Runs on the client's affinity thread, so everything that must follow
        the worker's request order happens here: staging the block ids,
        ordering after the worker's producer event, claiming deferred keys,
        reading the L1 objects and, for a layerwise retrieve, admitting it to
        the worker's launch sequencer. A layerwise retrieve then loads its
        layers on the layerwise retrieve pool, so the thread is free for the
        worker's next request while those layers arrive; the response is sent
        when it finishes. Every other retrieve finishes before this returns.

        Args:
            key: The IPC key for the KV cache blocks.
                Must have worker_id != None (worker retrieve operation).
            instance_id: The GPU instance ID (such as PID).
            gpu_block_ids: GPU block IDs to retrieve into, indexed by LMCache
                KV group index.
            event_ipc_handle: The IPC handle of the event to wait on.
            skip_first_n_tokens: Number of tokens to skip writing at
                the start of the retrieve range. This avoids overwriting
                APC-shared GPU blocks that may be read concurrently by other
                requests.
            retrieve_generation: Worker-assigned generation tag for layerwise
                progress; ignored when layerwise mode is disabled.

        Returns:
            Resolves to a tuple where the first element is the IPC handle of
            the event that signals the completion of the retrieve operation,
            and the second element indicates whether the key was successfully
            retrieved. The event handle is empty when no device work was
            submitted.

        Raises:
            RuntimeError: If the backend does not support IPC event handles.
        """
        st = time.perf_counter()

        entry = self.get_and_touch_context_entry(instance_id)
        if entry is None:
            # See store(): there is no completion event because no device work
            # was submitted. The False result lets the caller recover or
            # recompute without importing its own producer event.
            logger.warning(
                "Rejecting RETRIEVE for unregistered GPU instance ID %d",
                instance_id,
            )
            try:
                self._release_failed_retrieve_locks(key, instance_id)
            except Exception:
                # A cleanup failure must never suppress the terminal response:
                # the client otherwise waits forever because blocking-handler
                # exceptions are only logged by the MQ server.
                logger.exception(
                    "Failed to release RETRIEVE locks for unregistered "
                    "GPU instance ID %d",
                    instance_id,
                )
            _publish_layerwise_retrieve_terminal(
                self._ctx, None, instance_id, retrieve_generation
            )
            return DeferredResponse.resolved((b"", False))
        cache_context = entry.cache_context
        model_name = entry.model_name
        event_backend = entry.event_backend
        if event_backend is None:
            raise RuntimeError("Registered cache context has no event backend")

        num_object_groups = cache_context.kv_layer_groups_manager.num_object_groups
        obj_keys_per_obj_group = self._ctx.resolve_obj_keys(
            key, list(range(num_object_groups))
        )
        num_chunks = len(obj_keys_per_obj_group[0])

        # CPU-synchronous sentinel: a GPU retrieve is about to be enqueued.
        # Must be published via publish() (not publish_on_stream) so the
        # drain thread sees it before MP_REQUEST_END can race MP_RETRIEVE_END.
        self._ctx.event_bus.publish(
            Event(
                event_type=EventType.MP_RETRIEVE_SUBMITTED,
                session_id=key.request_id,
                metadata={"device": str(cache_context.device)},
            )
        )

        transfer_key = next_transfer_key(key.request_id)
        self._ctx.event_bus.publish_on_stream(
            cache_context.cupy_stream,
            Event(
                event_type=EventType.MP_RETRIEVE_START,
                session_id=key.request_id,
                metadata={
                    "device": str(cache_context.device),
                    "engine_id": instance_id,
                    "model_name": model_name,
                    "transfer_key": transfer_key,
                },
            ),
        )

        blocks_per_chunk = [
            cache_context.calculate_num_blocks(self._ctx.chunk_size, group_idx)
            for group_idx in range(
                cache_context.kv_layer_groups_manager.num_kernel_groups
            )
        ]

        with (
            torch_dev.device(cache_context.device),
            torch_dev.stream(cache_context.stream),
        ):
            event = event_backend.create_event(cache_context.device)

            # Fail closed: a short block-id list would drive the transfer
            # kernel to write out-of-bounds GPU memory. Checked on the raw
            # block ids, before cutting drops the per-chunk blocks that
            # sliding-window groups do not need.
            if any(
                len(group_block_ids) < num_chunks * bpc
                for group_block_ids, bpc in zip(
                    gpu_block_ids, blocks_per_chunk, strict=True
                )
            ):
                logger.error(
                    "RETRIEVE block ID underflow for request_id=%s: each group "
                    "needs num_chunks * blocks_per_chunk block IDs for %d "
                    "chunks (per-group blocks_per_chunk=%s); skipping the "
                    "retrieve.",
                    key.request_id,
                    num_chunks,
                    blocks_per_chunk,
                )
                _publish_layerwise_retrieve_terminal(
                    self._ctx, entry, instance_id, retrieve_generation
                )
                event_backend.record_event(event, cache_context.stream)
                return DeferredResponse.resolved(
                    (event_backend.export_event(event, cache_context.device), False)
                )

            layerwise_active = (
                self._ctx.use_layerwise
                and getattr(entry, "layerwise_schedule", None) is not None
                and getattr(entry, "retrieve_sequencer", None) is not None
            )
            # A layerwise retrieve keeps launching after this thread moved on
            # to the worker's next request, which restages the shared buffer.
            stage_block_ids = (
                downsample_and_stage_owned_block_ids
                if layerwise_active
                else downsample_and_stage_block_ids
            )
            block_ids_per_group_gpu = stage_block_ids(cache_context, gpu_block_ids)
            producer_event = event_backend.import_event(
                event_ipc_handle, cache_context.device
            )
            event_backend.wait_event(producer_event, cache_context.stream)

            # Per object group, the prefetch only locked the in-window suffix
            # (the last ``num_chunks_in_sw`` chunks; the whole prefix for full
            # attention, where the value is < 0). Read and transfer only those.
            # Aux (connector-private) groups are never served by the
            # std retrieve: the lookup does not lock their keys and their
            # block-id entry is a placeholder -- reading them would be an
            # unlocked read of a plane nobody consumes here.
            attn_desc = cache_context.kv_layer_groups_manager.get_attn_desc()
            skipped_groups = {
                g for g, kind in enumerate(attn_desc.group_kinds) if kind == "aux"
            }
            group_skips = [
                first_in_window_chunk(num_chunks, window)
                for window in attn_desc.num_chunks_in_sw
            ]
            run = _RetrieveRun(
                key=key,
                instance_id=instance_id,
                entry=entry,
                event=event,
                transfer_key=transfer_key,
                started=st,
                num_chunks=num_chunks,
                expected_retained=sum(
                    num_chunks - skip
                    for g, skip in enumerate(group_skips)
                    if g not in skipped_groups
                ),
                skip_first_n_tokens=skip_first_n_tokens,
                retrieve_generation=retrieve_generation,
                obj_keys_per_obj_group=obj_keys_per_obj_group,
                block_ids_per_group_gpu=block_ids_per_group_gpu,
                layerwise_active=layerwise_active,
                memory_objs_by_group=[[] for _ in range(num_object_groups)],
            )
            handed_off = False
            try:
                run.claimed = self._claim_deferred_keys(
                    key, obj_keys_per_obj_group, group_skips, skipped_groups
                )
                run.deferred = self._resolve_deferred_keys(
                    key, model_name, run.claimed, layerwise_active
                )
                run.outcome = run.deferred.outcome
                fetch_set = frozenset(run.deferred.to_fetch)
                for obj_group_id in range(num_object_groups):
                    if obj_group_id in skipped_groups:
                        continue
                    skip = group_skips[obj_group_id]
                    in_window_keys = obj_keys_per_obj_group[obj_group_id][skip:]
                    l1_keys = [k for k in in_window_keys if k not in fetch_set]
                    with self._ctx.storage_manager.read_prefetched_results(
                        l1_keys
                    ) as window_objs:
                        if (
                            window_objs is None
                            or not in_window_keys
                            or len(window_objs) != len(l1_keys)
                        ):
                            logger.error("Some keys not found during retrieve!")
                            run.succeeded = False
                            if layerwise_active:
                                _publish_layerwise_retrieve_terminal(
                                    self._ctx,
                                    entry,
                                    instance_id,
                                    retrieve_generation,
                                )
                            break

                        run.total_bytes += sum(mo.get_size() for mo in window_objs)

                        l1_objs = iter(window_objs)
                        memory_objs: list[MemoryObj | None] = [None] * skip
                        memory_objs.extend(
                            None if k in fetch_set else next(l1_objs)
                            for k in in_window_keys
                        )
                        run.memory_objs_by_group[obj_group_id] = memory_objs

                        if not layerwise_active:
                            with cache_context.transfer_gate.hold():
                                transfer_kv_per_object_group(
                                    cache_context,
                                    block_ids_per_group_gpu,
                                    memory_objs,
                                    object_group_id=obj_group_id,
                                    batch_size=cache_context.max_batch_size,
                                    skip_first_n_tokens=skip_first_n_tokens,
                                    direction=lmcache_native.TransferDirection.H2D,
                                    transfer_key=transfer_key,
                                )
                        run.prefetched_keys.extend(l1_keys)

                if layerwise_active and run.succeeded:
                    if retrieve_generation <= 0:
                        raise ValueError(
                            "layerwise retrieve requires a positive "
                            "retrieve_generation from the worker"
                        )
                    future = self._hand_off_layerwise_retrieve(run)
                    handed_off = True
                    return DeferredResponse(future)
            except Exception as exc:
                self._note_retrieve_failure(run, exc)
            finally:
                if not handed_off:
                    self._end_retrieve_on_stream(run)
        return DeferredResponse.resolved(self._retrieve_response(run))

    def retrieve(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
        gpu_block_ids: list[list[int]],
        event_ipc_handle: bytes,
        skip_first_n_tokens: int = 0,
        retrieve_generation: int = 0,
    ) -> tuple[bytes, bool]:
        """Retrieve the CPU KV cache into GPU blocks, blocking until done.

        Same as :meth:`start_retrieve`, waiting for its response.

        Args:
            key: See :meth:`start_retrieve`.
            instance_id: See :meth:`start_retrieve`.
            gpu_block_ids: See :meth:`start_retrieve`.
            event_ipc_handle: See :meth:`start_retrieve`.
            skip_first_n_tokens: See :meth:`start_retrieve`.
            retrieve_generation: See :meth:`start_retrieve`.

        Returns:
            The completion event's IPC handle and whether the key was
            retrieved; see :meth:`start_retrieve`.

        Raises:
            RuntimeError: If the backend does not support IPC event handles.
        """
        return self.start_retrieve(
            key,
            instance_id,
            gpu_block_ids,
            event_ipc_handle,
            skip_first_n_tokens,
            retrieve_generation,
        ).result()

    def _publish_token_bindings(
        self, key: IPCCacheServerKey, obj_keys: list[ObjectKey]
    ) -> None:
        """Publish one ``MP_TOKENS`` event for ``key``'s chunks.

        Pairs each complete chunk in ``[key.start, key.end)`` with its
        ObjectKey chunk hash and token position. Must be called at store
        submission, before the write-finished events reach the bus, so the
        cache-event subscriber can stamp them onto the STORE entries. A
        store that later fails leaves only unused cache entries.

        Args:
            key: The IPC key of the store being submitted.
            obj_keys: One ObjectKey per complete chunk, in chunk order.
        """
        # Complete chunks in [key.start, key.end) paired with the absolute
        # position of each chunk's first token. Prefix-chained chunk hashes
        # imply a position without revealing it, so it is reported here. A
        # trailing partial chunk has no stored KV to bind to.
        chunk_size = self._ctx.chunk_size
        token_ids = list(key.token_ids)
        effective_len = min(len(token_ids), key.end)
        num_complete = effective_len - effective_len % chunk_size
        token_offsets = list(range(key.start, num_complete, chunk_size))
        token_chunks = [
            token_ids[offset : offset + chunk_size] for offset in token_offsets
        ]
        if not token_chunks:
            return
        if len(obj_keys) != len(token_chunks):
            logger.warning(
                "Skipping token bindings for request %s: %d resolved keys "
                "vs %d complete chunks in [%d, %d)",
                key.request_id,
                len(obj_keys),
                len(token_chunks),
                key.start,
                key.end,
            )
            return
        self._ctx.event_bus.publish(
            Event(
                event_type=EventType.MP_TOKENS,
                session_id=key.request_id,
                metadata={
                    "chunk_hashes": [obj_key.chunk_hash for obj_key in obj_keys],
                    "token_chunks": token_chunks,
                    "token_offsets": token_offsets,
                },
            )
        )

    def _claim_deferred_keys(
        self,
        key: IPCCacheServerKey,
        obj_keys_per_obj_group: list[list[ObjectKey]],
        group_skips: list[int],
        skipped_groups: set[int],
    ) -> tuple[ObjectKey, ...]:
        """Take the session's deferred keys among this retrieve's reads.

        Takes no locks. Each key is handed out once per lookup, so a repeated
        retrieve cannot fetch twice.

        Args:
            key: The retrieve's key.
            obj_keys_per_obj_group: The retrieve's keys per object group.
            group_skips: Per group, the first chunk read.
            skipped_groups: Groups the retrieve does not read.

        Returns:
            The claimed keys in request order; empty without a pipelined
            sink, a session, or deferred keys.
        """
        if self._pipelined_sink_factory is NO_PIPELINED_SINK_FACTORY:
            return ()
        session = self._ctx.session_manager.get(key.request_id)
        if session is None:
            return ()
        in_window = [
            obj_key
            for group_id, keys in enumerate(obj_keys_per_obj_group)
            if group_id not in skipped_groups
            for obj_key in keys[group_skips[group_id] :]
        ]
        return tuple(session.claim_deferred_keys(in_window))

    def _resolve_deferred_keys(
        self,
        key: IPCCacheServerKey,
        model_name: str,
        claimed: tuple[ObjectKey, ...],
        layerwise_active: bool,
    ) -> _DeferredKeys:
        """Say which claimed deferred keys to fetch layer by layer.

        Deferred keys another fetch left readable are read-locked for reuse,
        and busy ones are handled by ``--pipelined-shared-keys``. If this
        retrieve cannot fetch layer by layer, the rest are loaded whole. Keys
        locked here are read, and released, like the lookup's; the retrieve
        releases them itself if it fails before reading them.

        Args:
            key: The retrieve's key.
            model_name: The registered model.
            claimed: The keys :meth:`_claim_deferred_keys` took.
            layerwise_active: Whether this retrieve loads layer by layer.

        Returns:
            The deferred keys still to fetch layer by layer, in request
            order (empty when every key is now in L1), the keys locked here,
            and the outcome when nothing is left to fetch.

        Raises:
            SharedKeysBusyError: If a busy key made the retrieve give up.
            LayerwiseContractError: If a whole load missed a key.
            LMCacheTimeoutError: If a whole load took too long.
        """
        if not claimed:
            return _DeferredKeys()
        storage_manager = self._ctx.storage_manager
        config = self._ctx.pipelined_fetch
        resolution = resolve_shared_keys(
            storage_manager,
            list(claimed),
            config.shared_keys,
            config.shared_wait_seconds,
        )
        to_fetch = resolution.to_fetch
        reused = tuple(resolution.reused)
        if not to_fetch:
            return _DeferredKeys(locked=reused, outcome=PipelinedOutcome.REUSED)
        try:
            self._ctx.pipelined_models.find(model_name, key.world_size)
        except KeyError:
            layerwise_active = False
        if layerwise_active:
            return _DeferredKeys(to_fetch=tuple(to_fetch), locked=reused)
        try:
            loaded = storage_manager.load_into_l1(
                list(to_fetch),
                self._group_layout_descs(model_name, key.world_size),
                config.whole_load_timeout_seconds,
            )
        except Exception:
            if resolution.reused:
                storage_manager.finish_read_prefetched(list(resolution.reused))
            raise
        if len(loaded) != len(to_fetch):
            storage_manager.finish_read_prefetched(
                list(resolution.reused) + list(loaded)
            )
            raise LayerwiseContractError(
                f"{len(to_fetch) - len(loaded)} deferred object(s) could not "
                "be loaded whole"
            )
        return _DeferredKeys(
            locked=reused + tuple(loaded), outcome=PipelinedOutcome.LOADED_WHOLE
        )

    def _group_layout_descs(
        self, model_name: str, world_size: int
    ) -> dict[int, MemoryLayoutDesc]:
        """Return a registered model's per-object-group layouts.

        Raises:
            LayerwiseContractError: If the model has none registered.
        """
        layouts = self._ctx.layout_desc_registry.find_group_layout_descs(
            model_name, world_size
        )
        if not layouts:
            raise LayerwiseContractError(
                f"no object group layouts registered for {model_name!r}"
            )
        return layouts

    def _register_pipelined_model(
        self,
        model_name: str,
        world_size: int,
        fetch_model: FetchModel,
        group_layout_descs: dict[int, MemoryLayoutDesc],
        cache_context: BaseCacheContext,
    ) -> None:
        """Make a model's lookups eligible for the pipelined retrieve.

        On any reason the model cannot be served -- no pipelined sink, a
        world size above one, per-layer staging reading layers from other
        bytes than the planner lands them at, no ready pipelined path, or a
        window too small for the chunk cap -- logs it and leaves the model
        loading whole objects at lookup.

        Args:
            model_name: The model being registered.
            world_size: Its world size.
            fetch_model: Its registered layout and attention windows.
            group_layout_descs: Its per-object-group layouts.
            cache_context: Its cache context, whose staging views per-layer
                staging copies through.
        """
        if self._pipelined_sink_factory is NO_PIPELINED_SINK_FACTORY:
            logger.warning(
                "No pipelined sink is installed; %s loads whole objects at lookup",
                model_name,
            )
            return
        if world_size != 1:
            logger.warning(
                "Pipelined fetch serves world size 1 only; %s (world size %d) "
                "loads whole objects at lookup",
                model_name,
                world_size,
            )
            return
        try:
            check_staging_matches_plan(
                fetch_model.layout,
                per_layer_staging_ranges(
                    cache_context,
                    LayerwiseSchedule.from_kernel_groups(
                        cache_context.kv_layer_groups_manager.kernel_groups
                    ),
                ),
            )
        except (LayerwiseContractError, ValueError):
            logger.error(
                "Per-layer staging would read %s's layers from other bytes than "
                "the pipelined fetch writes them to; it loads whole objects at "
                "lookup",
                model_name,
                exc_info=True,
            )
            return
        logger.info(
            "Per-layer staging matches the pipelined fetch plan for all %d "
            "layers of %s",
            len(fetch_model.layout.layer_ids()),
            model_name,
        )
        max_chunks = self._ctx.pipelined_fetch.max_chunks
        storage_manager = self._ctx.storage_manager
        try:
            model = PipelinedModel(
                fetch_model=fetch_model,
                placer=storage_manager.pipelined_window_placer(
                    group_layout_descs, fetch_model, max_chunks
                ),
                max_record_bytes=storage_manager.pipelined_max_record_bytes(),
                max_slots=storage_manager.pipelined_max_slots_per_request(),
                adapter_id=storage_manager.pipelined_adapter_id(),
                max_chunks=max_chunks,
            )
        except (LayerwiseContractError, ValueError):
            logger.warning(
                "Cannot fetch %s layer by layer; it loads whole objects at lookup",
                model_name,
                exc_info=True,
            )
            return
        self._ctx.pipelined_models.register(model_name, world_size, model)
        # The cap is the reader's; objects written under another cap make
        # the fetch decline slots and fall back.
        logger.info(
            "%s fetches layer by layer from L2 adapter %d, reading records of "
            "at most %d bytes",
            model_name,
            model.adapter_id,
            model.max_record_bytes,
        )

    def _hand_off_layerwise_retrieve(
        self, run: _RetrieveRun
    ) -> "Future[tuple[bytes, bool]]":
        """Admit a layerwise retrieve and queue its layer loading on the pool.

        Called on the client's affinity thread, so the worker's retrieves are
        admitted in the order it sent them.

        Args:
            run: The retrieve, with its L1 objects read.

        Returns:
            Resolves to the retrieve's response.

        Raises:
            ValueError: If the generation is not newer than every generation
                the worker's sequencer has seen.
            RuntimeError: If the pool no longer accepts work. The retrieve
                has been failed in the sequencer.
        """
        sequencer = _sequencer_of(run.entry)
        sequencer.admit(run.retrieve_generation)
        try:
            return self._layerwise_retrieve_pool.submit(
                self._finish_layerwise_retrieve, run
            )
        except BaseException:
            sequencer.release(run.retrieve_generation)
            raise

    def _finish_layerwise_retrieve(self, run: _RetrieveRun) -> tuple[bytes, bool]:
        """Load a handed-off layerwise retrieve's layers and finish it.

        Runs on the layerwise retrieve pool. Releases the retrieve from its
        sequencer after its completion event and key releases are queued.

        Args:
            run: The admitted retrieve.

        Returns:
            The retrieve's response; see :meth:`start_retrieve`.
        """
        cache_context = run.entry.cache_context
        try:
            with (
                torch_dev.device(cache_context.device),
                torch_dev.stream(cache_context.stream),
            ):
                try:
                    self._load_layers(run)
                except Exception as exc:
                    self._note_retrieve_failure(run, exc)
                finally:
                    self._end_retrieve_on_stream(run)
        finally:
            _sequencer_of(run.entry).release(run.retrieve_generation)
        return self._retrieve_response(run)

    def _load_layers(self, run: _RetrieveRun) -> None:
        """Copy a layerwise retrieve's objects to the GPU, layer by layer.

        Deferred keys are fetched with the pipelined fetch, which loads each
        layer as it lands, or whole; whole objects go through one layerwise
        transfer.

        Args:
            run: The admitted retrieve. Its keys, objects and outcome are
                updated with what the fetch locked and delivered.

        Raises:
            RetrieveAbortedError: If another retrieve's failure stopped it.
            Exception: Whatever the fetch or the transfer raises.
        """
        entry = run.entry
        cache_context = entry.cache_context
        schedule = entry.layerwise_schedule
        if schedule is None:
            raise RuntimeError("layerwise retrieve has no launch schedule")
        sequencer = _sequencer_of(entry)
        keys_to_fetch = run.deferred.to_fetch
        delivery = DeferredLoad.WHOLE
        if keys_to_fetch:
            table = ObjectTable(run.memory_objs_by_group)
            deferred_fetch = fetch_deferred_objects(
                self._ctx.storage_manager,
                self._ctx.pipelined_models.find(entry.model_name, run.key.world_size),
                run.obj_keys_per_obj_group,
                keys_to_fetch,
                self._pipelined_sink_factory,
                PipelinedLoadRequest(
                    cache_context=cache_context,
                    block_ids_gpu=run.block_ids_per_group_gpu,
                    objects=table,
                    skip_first_n_tokens=run.skip_first_n_tokens,
                    schedule=schedule,
                    sequencer=sequencer,
                    retrieve_generation=run.retrieve_generation,
                    transfer_key=run.transfer_key,
                ),
                self._group_layout_descs(entry.model_name, run.key.world_size),
                self._ctx.pipelined_fetch,
            )
            run.prefetched_keys.extend(deferred_fetch.locked_keys)
            run.memory_objs_by_group = table.by_group()
            delivery = deferred_fetch.load
            run.outcome = deferred_fetch.outcome
            if delivery is DeferredLoad.PIPELINED:
                run.fetched_in_window = keys_to_fetch
        if delivery is DeferredLoad.WHOLE:
            transfer_kv_layerwise_h2d(
                cache_context,
                run.block_ids_per_group_gpu,
                run.memory_objs_by_group,
                run.skip_first_n_tokens,
                schedule,
                sequencer,
                run.retrieve_generation,
                transfer_key=run.transfer_key,
            )

    def _note_retrieve_failure(self, run: _RetrieveRun, exc: Exception) -> None:
        """Record that a retrieve raised; call from the ``except`` clause.

        Args:
            run: The failed retrieve.
            exc: What it raised.
        """
        logger.exception("Cannot retrieve keys due to exception")
        run.succeeded = False
        if run.claimed:
            run.outcome = (
                PipelinedOutcome.SHARED_KEYS_BUSY
                if isinstance(exc, SharedKeysBusyError)
                else PipelinedOutcome.FAILED
            )
        if run.layerwise_active:
            _publish_layerwise_retrieve_terminal(
                self._ctx, run.entry, run.instance_id, run.retrieve_generation
            )

    def _end_retrieve_on_stream(self, run: _RetrieveRun) -> None:
        """Queue a retrieve's completion event, key releases and end event.

        Call with the cache context's stream current, after the retrieve
        queued its last copy (or failed).

        Args:
            run: The finished retrieve. Its keys to release are completed
                with the deferred keys it locked but never read.
        """
        entry = run.entry
        cache_context = entry.cache_context
        _event_backend_of(entry).record_event(run.event, cache_context.stream)
        retrieved_count = len(set(run.prefetched_keys).union(run.fetched_in_window))
        read_keys = set(run.prefetched_keys)
        run.prefetched_keys.extend(k for k in run.deferred.locked if k not in read_keys)
        if run.prefetched_keys:
            submit_callback_to_stream(
                cache_context.cupy_stream,
                "finish_read_prefetched",
                run.prefetched_keys,
            )
        num_tokens = (
            run.num_chunks * self._ctx.chunk_size
            if retrieved_count == run.expected_retained
            else 0
        )
        self._ctx.event_bus.publish_on_stream(
            cache_context.cupy_stream,
            Event(
                event_type=EventType.MP_RETRIEVE_END,
                session_id=run.key.request_id,
                metadata={
                    "retrieved_count": retrieved_count,
                    "device": str(cache_context.device),
                    "engine_id": run.instance_id,
                    "model_name": entry.model_name,
                    "cache_salt": run.key.cache_salt,
                    "total_bytes": run.total_bytes,
                    "num_tokens": num_tokens,
                    "transfer_key": run.transfer_key,
                    "pipelined_outcome": run.outcome.value,
                    "deferred_count": len(run.claimed),
                },
            ),
        )

    def _retrieve_response(self, run: _RetrieveRun) -> tuple[bytes, bool]:
        """Log a finished retrieve and build its response.

        Args:
            run: The retrieve, its completion event already queued.

        Returns:
            The completion event's IPC handle and whether it succeeded.
        """
        if run.succeeded:
            logger.info(
                "Retrieved %d tokens in %.3f seconds",
                run.num_chunks * self._ctx.chunk_size,
                time.perf_counter() - run.started,
            )
        cache_context = run.entry.cache_context
        return (
            _event_backend_of(run.entry).export_event(run.event, cache_context.device),
            run.succeeded,
        )
