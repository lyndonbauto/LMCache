# SPDX-License-Identifier: Apache-2.0
"""The retrieve side of the pipelined fetch: deferred keys to GPU layers.

A lookup that deferred L2 hits leaves the retrieve to fetch them (see
``docs/design/v1/layerwise/c9-wiring.md``). :func:`fetch_deferred_objects`
does that for one retrieve, over an :class:`ObjectTable` that already holds
the request's L1 objects:

- **Pipelined:** the objects are fetched layer by layer into an RDMA window,
  and a sink from the :class:`PipelinedSinkFactory` loads every layer of the
  request -- L1 and window objects alike -- as it lands.
- **Whole:** the fetch was refused before anything began, so the objects are
  loaded whole into L1 and put in the table; the caller then runs the
  ordinary layerwise transfer over it.

The sink is Track B's. It is built per retrieve from a
:class:`PipelinedLoadRequest`, and it must read the table when it loads a
layer, not when it is built, because the fallback swaps objects in.
"""

# Standard
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
import threading

# Third Party
import torch

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.layerwise.contract import (
    LayerArrivalSource,
    LayerLoadSink,
    LayerwiseContractError,
)
from lmcache.v1.layerwise.deferral import PipelinedModel
from lmcache.v1.layerwise.pipelined_retrieve import (
    PipelinedRetrieveRefused,
    RetrieveCompletion,
    run_pipelined_retrieve,
)
from lmcache.v1.layerwise.pump import DEFAULT_LAYER_TIMEOUT_SECONDS
from lmcache.v1.layerwise.request_fetch import (
    ObjectToPlace,
    WindowLease,
    objects_to_place,
)
from lmcache.v1.memory_management import MemoryObj
from lmcache.v1.multiprocess.layer_progress import (
    DaemonLayerLaunchEventPool,
    LayerProgressRecord,
)
from lmcache.v1.multiprocess.layerwise_schedule import LayerwiseSchedule
from lmcache.v1.platform.base.cache_context import BaseCacheContext

logger = init_logger(__name__)

#: How long loading deferred objects whole may take. The worker's wait for
#: its first layer is already running, so this stays within the pump's
#: per-layer timeout.
WHOLE_LOAD_TIMEOUT_SECONDS = DEFAULT_LAYER_TIMEOUT_SECONDS


class ObjectTable:
    """Each object a retrieve reads, by object group and chunk; thread-safe.

    Built from the L1 objects, with ``None`` where an object is deferred or
    outside the group's window. Filled in with window objects when the fetch
    is leased, and with whole objects if it falls back. A sink reads it each
    time it loads a layer.
    """

    def __init__(self, objects_by_group: Sequence[Sequence[MemoryObj | None]]) -> None:
        """Create a table.

        Args:
            objects_by_group: Element ``g`` is object group ``g``'s objects,
                one per chunk of the request, ``None`` where absent.
        """
        self._lock = threading.Lock()
        self._objects = [list(group) for group in objects_by_group]

    def get(self, object_group_id: int, chunk_id: int) -> MemoryObj | None:
        """Return one object, or ``None`` if the table has none for it.

        Args:
            object_group_id: The object group.
            chunk_id: The chunk's index in the request.

        Returns:
            The object's memory, or ``None``.
        """
        with self._lock:
            return self._objects[object_group_id][chunk_id]

    def put(self, objects: dict[tuple[int, int], MemoryObj]) -> None:
        """Set objects, replacing any already there.

        Args:
            objects: ``{(object_group_id, chunk_id): memory}``.

        Raises:
            IndexError: If a position is outside the request.
        """
        with self._lock:
            for (group_id, chunk_id), obj in objects.items():
                self._objects[group_id][chunk_id] = obj

    def by_group(self) -> list[list[MemoryObj | None]]:
        """Return a copy of the table, as the layerwise transfer takes it.

        Returns:
            Element ``g`` is object group ``g``'s objects by chunk.
        """
        with self._lock:
            return [list(group) for group in self._objects]


@dataclass(frozen=True)
class PipelinedLoadRequest:
    """Everything a pipelined sink needs for one retrieve.

    Attributes:
        cache_context: The worker's registered KV cache.
        block_ids_gpu: The destination blocks, staged as
            ``downsample_and_stage_block_ids`` returns them.
        objects: The objects to copy from; read at load time.
        skip_first_n_tokens: Tokens not to write at the start of the range.
        schedule: The registered layer launch schedule.
        progress: The worker's shared layer progress record.
        event_pool: The daemon's per-layer launch events.
        retrieve_generation: The worker's generation for this retrieve; the
            pump numbers its loads independently, so the sink maps one to
            the other.
        transfer_key: This retrieve's key for observability events.
    """

    cache_context: BaseCacheContext
    block_ids_gpu: list[torch.Tensor]
    objects: ObjectTable
    skip_first_n_tokens: int
    schedule: LayerwiseSchedule
    progress: LayerProgressRecord
    event_pool: DaemonLayerLaunchEventPool
    retrieve_generation: int
    transfer_key: str


class PipelinedSink(LayerLoadSink, Protocol):
    """A sink whose GPU copies the retrieve can wait for."""

    def wait_for_copies(self) -> None:
        """Block until every copy issued so far has finished reading host memory."""
        ...


class PipelinedSinkFactory(Protocol):
    """Builds the sink for one pipelined retrieve (Track B's loader)."""

    def build(self, request: PipelinedLoadRequest) -> PipelinedSink:
        """Build a sink over one retrieve.

        Args:
            request: The retrieve's destination, objects and progress.

        Returns:
            A sink with no load begun.
        """
        ...


class _NoPipelinedSinkFactory:
    """The factory of a daemon that has no pipelined sink."""

    def build(self, request: PipelinedLoadRequest) -> PipelinedSink:
        raise LayerwiseContractError("this daemon has no pipelined sink")


#: No pipelined sink: models are not registered for the pipelined fetch, so
#: lookups never defer.
NO_PIPELINED_SINK_FACTORY: PipelinedSinkFactory = _NoPipelinedSinkFactory()


class DeferredObjectStorage(Protocol):
    """The storage calls :func:`fetch_deferred_objects` needs.

    ``StorageManager`` implements it.
    """

    def layer_arrival_source(self) -> LayerArrivalSource:
        """Return a new source for one layerwise fetch."""
        ...

    def load_into_l1(
        self,
        keys: list[ObjectKey],
        group_layout_descs: dict[int, MemoryLayoutDesc],
        timeout_seconds: float,
    ) -> dict[ObjectKey, MemoryObj]:
        """Load whole objects into L1 and read-lock them."""
        ...

    def finish_read_prefetched(
        self, keys: list[ObjectKey], read_locks: int = 1
    ) -> None:
        """Release read locks."""
        ...


class DeferredLoad(Enum):
    """How :func:`fetch_deferred_objects` delivered the deferred objects."""

    #: The sink loaded every layer of the request; nothing is left to do.
    PIPELINED = "pipelined"
    #: The objects are whole in L1 and in the table; the caller runs the
    #: ordinary layerwise transfer over the table.
    WHOLE = "whole"


class PipelinedOutcome(Enum):
    """How one retrieve's deferred keys were served, for observability.

    Published as ``pipelined_outcome`` on ``MP_RETRIEVE_END``. Only
    ``PIPELINED`` means the layers were loaded as they arrived; every other
    served outcome means the request waited for whole objects.
    """

    #: The lookup deferred nothing for this retrieve.
    NOT_DEFERRED = "not_deferred"
    #: Every layer was loaded from the window as it landed.
    PIPELINED = "pipelined"
    #: The transport failed part way; the rest came from whole objects.
    FELL_BACK = "fell_back"
    #: No L2 adapter could fetch layer by layer; loaded whole.
    NO_SOURCE = "no_source"
    #: The lease or plan was refused (window busy, too many slots, ...);
    #: loaded whole.
    REFUSED = "refused"
    #: The retrieve was not layerwise, or the model has no pipelined setup;
    #: loaded whole before the transfer.
    LOADED_WHOLE = "loaded_whole"
    #: Another fetch had already left every deferred key in L1.
    REUSED = "reused"
    #: Another request was still fetching a deferred key, and
    #: ``--pipelined-shared-keys`` gave up; vLLM recomputes.
    SHARED_KEYS_BUSY = "shared_keys_busy"
    #: Serving the deferred keys raised; vLLM recomputes.
    FAILED = "failed"


@dataclass(frozen=True)
class DeferredFetchResult:
    """What :func:`fetch_deferred_objects` did.

    Attributes:
        outcome: ``PIPELINED``, ``FELL_BACK``, ``NO_SOURCE`` or ``REFUSED``.
        locked_keys: Keys this call read-locked in L1 (whole loads, in
            either path). The caller releases them after its copies.
    """

    outcome: PipelinedOutcome
    locked_keys: tuple[ObjectKey, ...]

    @property
    def load(self) -> DeferredLoad:
        """Whether the sink delivered every layer or the caller must copy."""
        if self.outcome in (PipelinedOutcome.PIPELINED, PipelinedOutcome.FELL_BACK):
            return DeferredLoad.PIPELINED
        return DeferredLoad.WHOLE


class _WindowLoader:
    """The :class:`~.pipelined_retrieve.PipelinedLoader` of one retrieve."""

    def __init__(
        self,
        storage: DeferredObjectStorage,
        sink_factory: PipelinedSinkFactory,
        request: PipelinedLoadRequest,
        group_layout_descs: dict[int, MemoryLayoutDesc],
    ) -> None:
        self._storage = storage
        self._sink_factory = sink_factory
        self._request = request
        self._group_layout_descs = group_layout_descs
        self._sink: PipelinedSink | None = None
        self._objects: tuple[ObjectToPlace, ...] = ()
        self.locked_keys: list[ObjectKey] = []

    def place(self, objects: Sequence[ObjectToPlace]) -> None:
        """Record the objects the window will hold, before leasing."""
        self._objects = tuple(objects)

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        self._request.objects.put(
            {
                (o.object_group_id, o.chunk_id): lease.memory_obj(
                    o.chunk_id, o.object_group_id
                )
                for o in self._objects
            }
        )
        self._sink = self._sink_factory.build(self._request)
        return self._sink

    def wait_for_copies(self) -> None:
        if self._sink is not None:
            self._sink.wait_for_copies()

    def wait_after_failure(self) -> None:
        """Wait for the sink's copies before its objects are released."""
        try:
            self.wait_for_copies()
        except Exception:
            logger.exception("Waiting for the pipelined sink's copies failed")

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        loaded = _load_whole(
            self._storage, objects, self._group_layout_descs, self.locked_keys
        )
        self._request.objects.put(loaded)


def _load_whole(
    storage: DeferredObjectStorage,
    objects: Sequence[ObjectToPlace],
    group_layout_descs: dict[int, MemoryLayoutDesc],
    locked_keys: list[ObjectKey],
) -> dict[tuple[int, int], MemoryObj]:
    """Load objects whole into L1, recording the keys it locked.

    Raises:
        LayerwiseContractError: If an object is in neither L1 nor L2. The
            keys that did load stay in ``locked_keys`` for the caller to
            release.
        LMCacheTimeoutError: If the load took too long.
    """
    loaded = storage.load_into_l1(
        [o.key for o in objects], group_layout_descs, WHOLE_LOAD_TIMEOUT_SECONDS
    )
    locked_keys.extend(loaded)
    missing = [o.key for o in objects if o.key not in loaded]
    if missing:
        raise LayerwiseContractError(
            f"{len(missing)} deferred object(s) could not be loaded whole"
        )
    return {(o.object_group_id, o.chunk_id): loaded[o.key] for o in objects}


def fetch_deferred_objects(
    storage: DeferredObjectStorage,
    model: PipelinedModel,
    obj_keys_per_obj_group: Sequence[Sequence[ObjectKey]],
    keys_to_fetch: Sequence[ObjectKey],
    sink_factory: PipelinedSinkFactory,
    request: PipelinedLoadRequest,
    group_layout_descs: dict[int, MemoryLayoutDesc],
) -> DeferredFetchResult:
    """Deliver a retrieve's deferred objects, layer by layer if possible.

    Blocks until the sink loaded every layer, or the objects are whole in
    L1. On any failure, the keys this call locked are released before the
    error is raised.

    Args:
        storage: Where the objects are fetched or loaded from.
        model: The model's registered pipelined setup.
        obj_keys_per_obj_group: The request's keys, one list per object
            group, as ``resolve_obj_keys`` returns them with a worker id.
        keys_to_fetch: The deferred keys not already in L1.
        sink_factory: Builds the sink for the pipelined path.
        request: The retrieve's destination, with ``request.objects``
            holding its L1 objects.
        group_layout_descs: The model's per-object-group layouts, for whole
            loads.

    Returns:
        How the objects were delivered, and the keys the caller now releases.

    Raises:
        LayerwiseContractError: If the objects could not be delivered either
            way; a load the sink began has been abandoned.
        Exception: Anything else the transport, sink or storage raises.
    """
    wanted = frozenset(keys_to_fetch)
    loader = _WindowLoader(storage, sink_factory, request, group_layout_descs)
    try:
        try:
            source = storage.layer_arrival_source()
        except LayerwiseContractError as exc:
            logger.warning("No pipelined fetch; loading whole objects: %s", exc)
            outcome = PipelinedOutcome.NO_SOURCE
        else:
            try:
                loader.place(_objects_of(model, obj_keys_per_obj_group, wanted))
                result = run_pipelined_retrieve(
                    model.fetch_model,
                    obj_keys_per_obj_group,
                    model.max_record_bytes,
                    model.placer,
                    source,
                    loader,
                    keys_to_fetch=wanted,
                )
                return DeferredFetchResult(
                    (
                        PipelinedOutcome.FELL_BACK
                        if result.completion is RetrieveCompletion.FELL_BACK
                        else PipelinedOutcome.PIPELINED
                    ),
                    tuple(loader.locked_keys),
                )
            except PipelinedRetrieveRefused as exc:
                logger.warning(
                    "Pipelined fetch refused; loading whole objects: %s", exc
                )
                outcome = PipelinedOutcome.REFUSED
        objects = _objects_of(model, obj_keys_per_obj_group, wanted)
        request.objects.put(
            _load_whole(storage, objects, group_layout_descs, loader.locked_keys)
        )
        return DeferredFetchResult(outcome, tuple(loader.locked_keys))
    except BaseException:
        if loader.locked_keys:
            loader.wait_after_failure()
            storage.finish_read_prefetched(list(loader.locked_keys))
        raise


def _objects_of(
    model: PipelinedModel,
    obj_keys_per_obj_group: Sequence[Sequence[ObjectKey]],
    wanted: frozenset[ObjectKey],
) -> tuple[ObjectToPlace, ...]:
    """The objects of ``wanted``, as the placer is asked to place them."""
    try:
        return objects_to_place(model.fetch_model, obj_keys_per_obj_group, wanted)
    except (ValueError, KeyError) as exc:
        raise PipelinedRetrieveRefused(
            f"cannot place the deferred objects: {exc}"
        ) from exc
