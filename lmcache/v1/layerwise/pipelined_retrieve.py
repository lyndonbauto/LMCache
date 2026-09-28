# SPDX-License-Identifier: Apache-2.0
"""Run one retrieve's pipelined fetch: lease, plan, pump, fall back, release.

The retrieve path needs one call that either delivers every layer of a
request to the loader, or tells it that nothing was started and it should
load whole objects instead::

    try:
        run_pipelined_retrieve(model, keys, cap, placer, source, loader)
    except PipelinedRetrieveRefused:
        ...  # nothing began: whole-object load, then today's path
    except Exception:
        ...  # the load began and was abandoned: vLLM recomputes

A transport that fails mid-fetch is neither: the load is continued from
whole objects (see :class:`PipelinedLoader`), because abandoning it would
fail the worker's waiters and a new load would make them raise as stale.

The window is released exactly once, and only after the loader's copies out
of it are done: see :class:`~.request_fetch.LeaseOutcome`.

Also here: :func:`resolve_shared_keys`, which decides before leasing what to
do with keys another request is still fetching (F3 in
``docs/design/v1/layerwise/c9-wiring.md``).

Kept out of the package ``__init__`` for the same import-cycle reason as
:mod:`~lmcache.v1.layerwise.request_fetch`.
"""

# Standard
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
import time

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.api import ObjectKey, ResidentKeys
from lmcache.v1.layerwise.contract import (
    LayerArrivalSource,
    LayerLoadSink,
    LayerwiseContractError,
)
from lmcache.v1.layerwise.deferral import SharedKeyPolicy
from lmcache.v1.layerwise.pump import (
    DEFAULT_LAYER_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    LayerArrivalPump,
    LoadLeftOpenError,
)
from lmcache.v1.layerwise.request_fetch import (
    ChunkPlacer,
    FetchModel,
    LeaseOutcome,
    ObjectToPlace,
    RequestFetch,
    WindowLease,
    build_request_fetch,
    objects_to_place,
)
from lmcache.v1.memory_management import MemoryObj

logger = init_logger(__name__)

#: Gap between checks while :attr:`SharedKeyPolicy.WAIT` waits for keys.
SHARED_KEY_POLL_SECONDS = 0.01


class PipelinedRetrieveRefused(LayerwiseContractError):
    """The pipelined fetch was refused before any load began.

    Raised when the placer, the planner or the transport's ``begin_fetch``
    refused the request. No sink load was begun and the window, if one was
    leased, has been released, so the caller can load every object whole and
    run the ordinary retrieve. The refusal is the ``__cause__``; it is a
    :class:`~.contract.PlanTooLargeError` when the request would fit only if
    split.
    """


class SharedKeysBusyError(LayerwiseContractError):
    """Another request is still fetching some of this retrieve's keys.

    Raised by :func:`resolve_shared_keys` under
    :attr:`SharedKeyPolicy.RECOMPUTE`, or under :attr:`SharedKeyPolicy.WAIT`
    once its budget is spent. Nothing is left locked; the retrieve should
    fail so vLLM recomputes.

    Attributes:
        busy_keys: The keys still being written.
    """

    def __init__(self, busy_keys: tuple[ObjectKey, ...]) -> None:
        """Record which keys were busy.

        Args:
            busy_keys: The keys still being written.
        """
        super().__init__(
            f"{len(busy_keys)} key(s) are still being fetched by another request"
        )
        self.busy_keys = busy_keys


class RetrieveCompletion(Enum):
    """How a pipelined retrieve delivered its layers."""

    #: Every layer came from the window as it landed.
    PIPELINED = "pipelined"
    #: The transport failed mid-fetch; the remaining layers came from
    #: whole objects loaded into L1.
    FELL_BACK = "fell_back"


@dataclass(frozen=True)
class PipelinedRetrieveResult:
    """What :func:`run_pipelined_retrieve` did.

    Attributes:
        fetch: The placements and plan that were fetched.
        completion: Whether the fetch finished or the fallback did.
    """

    fetch: RequestFetch
    completion: RetrieveCompletion


class PipelinedLoader(Protocol):
    """The retrieve path's side of one pipelined retrieve.

    Owns the sink and every memory object it copies from, other than the
    window's. :func:`run_pipelined_retrieve` calls :meth:`sink_for` once, and
    the other two only after it.
    """

    def sink_for(self, lease: WindowLease) -> LayerLoadSink:
        """Build the sink this retrieve loads through.

        The sink reads each object's memory when it loads a layer, not when
        it is built: the window's objects through ``lease.memory_obj``, until
        :meth:`reload_whole` replaces them.

        Args:
            lease: The request's window, with every deferred object placed.

        Returns:
            A sink with no load begun.
        """
        ...

    def wait_for_copies(self) -> None:
        """Block until every copy the sink has issued so far is done.

        After this returns the window's objects may be released.
        """
        ...

    def reload_whole(self, objects: Sequence[ObjectToPlace]) -> None:
        """Load ``objects`` whole into general L1, for the sink to read.

        Called after the window was released, so the objects' keys are free
        to reserve. From then on the sink reads these objects instead of the
        window's.

        Args:
            objects: Every object the window held.

        Raises:
            Exception: If any object could not be loaded; the load is then
                abandoned and vLLM recomputes.
        """
        ...


class ResidentKeyLocker(Protocol):
    """The storage calls :func:`resolve_shared_keys` needs.

    ``StorageManager`` implements it.
    """

    def lock_resident_keys(self, keys: list[ObjectKey]) -> ResidentKeys:
        """Read-lock the readable ``keys`` and report the rest.

        Args:
            keys: The keys to check.

        Returns:
            The keys split into locked, busy and absent.
        """
        ...

    def finish_read_prefetched(
        self, keys: list[ObjectKey], read_locks: int = 1
    ) -> None:
        """Release read locks taken by :meth:`lock_resident_keys`.

        Args:
            keys: The keys to release.
            read_locks: Locks to release per key.
        """
        ...


@dataclass(frozen=True)
class SharedKeyResolution:
    """Which deferred keys a retrieve reuses and which it fetches.

    Attributes:
        reused: Keys another fetch already left readable in L1, read-locked
            for this retrieve, with their memory. The caller releases them
            with ``finish_read_prefetched``.
        to_fetch: Keys not in L1, in the order asked; the retrieve fetches
            these.
    """

    reused: dict[ObjectKey, MemoryObj]
    to_fetch: tuple[ObjectKey, ...]


def _release_after_failure(lease: WindowLease, outcome: LeaseOutcome) -> None:
    """Release a lease on an error path without masking the original error."""
    try:
        lease.release(outcome)
    except Exception:
        logger.exception("Releasing a window lease as %s failed", outcome.name)


def _abandon_after_failure(sink: LayerLoadSink, generation: int) -> None:
    """Abandon a load on an error path without masking the original error."""
    try:
        sink.abandon_load(generation)
    except Exception:
        logger.exception("Abandoning load generation %d failed", generation)


def _wait_after_failure(loader: PipelinedLoader) -> None:
    """Wait for the loader's copies on an error path, logging any failure.

    The window is released right after, so a failure here is logged and not
    raised: the error that got us here is the one the caller needs.
    """
    try:
        loader.wait_for_copies()
    except Exception:
        logger.exception("Waiting for the loader's copies failed")


class _BeginTrackingSink:
    """Forwards to a sink and records whether a load was begun.

    Tells a refusal at ``begin_fetch``, which the pump raises before it
    begins the load, apart from a failure after it.
    """

    def __init__(self, sink: LayerLoadSink) -> None:
        self._sink = sink
        self.began = False

    def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None:
        self._sink.begin_load(generation, layer_ids)
        self.began = True

    def load_layer(self, layer_id: int) -> None:
        self._sink.load_layer(layer_id)

    def finish_load(self, generation: int) -> None:
        self._sink.finish_load(generation)

    def abandon_load(self, generation: int) -> None:
        self._sink.abandon_load(generation)


def resolve_shared_keys(
    locker: ResidentKeyLocker,
    keys: Sequence[ObjectKey],
    policy: SharedKeyPolicy,
    wait_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SharedKeyResolution:
    """Decide, before leasing, what to do with deferred keys already in L1.

    Two requests with the same prefix can both defer the same keys; the
    second one's lease would be refused while the first one's fetch holds
    them. So each deferred key is checked first: a readable one is reused, an
    absent one is fetched, and a busy one is handled by ``policy``. Under
    ``WAIT``, busy keys are rechecked every :data:`SHARED_KEY_POLL_SECONDS`
    until they are readable or absent, or ``wait_seconds`` pass.

    Args:
        locker: Storage, to lock readable keys and report the rest.
        keys: The retrieve's deferred keys.
        policy: What to do with busy keys.
        wait_seconds: How long ``WAIT`` waits in total.
        clock: Monotonic time source, in seconds; injectable for tests.
        sleep: Blocking sleep, in seconds; injectable for tests.

    Returns:
        The keys to reuse, locked, and the keys to fetch.

    Raises:
        SharedKeysBusyError: If any key is still busy when ``policy`` stops
            waiting. Every lock this call took has been released.
    """
    deadline = clock() + wait_seconds
    reused: dict[ObjectKey, MemoryObj] = {}
    absent: set[ObjectKey] = set()
    pending = list(keys)
    while True:
        resident = locker.lock_resident_keys(pending)
        reused.update(resident.locked)
        absent.update(resident.absent)
        if not resident.busy:
            break
        if policy is SharedKeyPolicy.RECOMPUTE or clock() >= deadline:
            if reused:
                locker.finish_read_prefetched(list(reused))
            raise SharedKeysBusyError(resident.busy)
        pending = list(resident.busy)
        sleep(SHARED_KEY_POLL_SECONDS)
    return SharedKeyResolution(
        reused=reused, to_fetch=tuple(key for key in keys if key in absent)
    )


def run_pipelined_retrieve(
    model: FetchModel,
    obj_keys_per_obj_group: Sequence[Sequence[ObjectKey]],
    max_record_bytes: int,
    placer: ChunkPlacer,
    source: LayerArrivalSource,
    loader: PipelinedLoader,
    keys_to_fetch: AbstractSet[ObjectKey] | None = None,
    layer_timeout_seconds: float = DEFAULT_LAYER_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> PipelinedRetrieveResult:
    """Fetch every deferred object a retrieve reads, loading layers as they land.

    Leases a window for the objects, plans the fetch into it, builds the
    loader's sink over the lease, and pumps the plan from ``source`` to the
    sink. Blocks until every layer is loaded or the load is abandoned, so
    call it from a worker thread, not the request handler.

    If the transport fails mid-fetch, the load is left open and continued:
    wait for the sink's copies, release the window ``ABANDONED``, reload the
    objects whole with :meth:`PipelinedLoader.reload_whole`, then load the
    remaining layers and finish. The window is released first because it
    still holds the objects' keys, which the reload has to reserve.

    The lease is released exactly once, after the sink's copies out of it:
    ``NEVER_FETCHED`` if nothing was issued, ``ABANDONED`` if the fetch
    failed, since writes may still be on the wire, and ``FINISHED``
    otherwise. A release that fails on an error path is logged and the
    original error raised.

    Args:
        model: The registered model's layout and windows.
        obj_keys_per_obj_group: The keys to fetch, one list per object
            group, as ``MPCacheServerContext.resolve_obj_keys`` returns them
            with a worker id.
        max_record_bytes: The record cap the objects were written under.
        placer: Leases the request's window and places its objects.
        source: The transport that fetches into the window.
        loader: Builds the sink, and reloads objects for the fallback.
        keys_to_fetch: Fetch only these of the request's objects, e.g. the
            deferred ones; ``None`` fetches every object the retrieve reads.
        layer_timeout_seconds: Longest the pump waits for any one layer.
        poll_interval_seconds: Gap between the pump's arrival polls.

    Returns:
        The fetch, and whether it finished or fell back.

    Raises:
        PipelinedRetrieveRefused: If the placer, the planner or
            ``begin_fetch`` refused the request. No load was begun.
        LayerwiseContractError: If the load was begun and then abandoned: a
            contract violation, or the fallback failing to load an object.
        BaseException: Anything else the loader or transport raises,
            unchanged. If a load was begun it has been abandoned.
    """
    try:
        objects = objects_to_place(model, obj_keys_per_obj_group, keys_to_fetch)
    except (ValueError, KeyError) as exc:
        raise PipelinedRetrieveRefused(
            f"cannot plan a layerwise fetch for this request: {exc}"
        ) from exc

    try:
        lease = placer.lease(objects)
    except LayerwiseContractError as exc:
        raise PipelinedRetrieveRefused(f"no window for this request: {exc}") from exc

    try:
        fetch = build_request_fetch(
            model, obj_keys_per_obj_group, max_record_bytes, lease, keys_to_fetch
        )
    except (ValueError, KeyError) as exc:
        _release_after_failure(lease, LeaseOutcome.NEVER_FETCHED)
        raise PipelinedRetrieveRefused(
            f"cannot plan a layerwise fetch into the leased window: {exc}"
        ) from exc
    except BaseException:
        _release_after_failure(lease, LeaseOutcome.NEVER_FETCHED)
        raise
    try:
        sink = _BeginTrackingSink(loader.sink_for(lease))
    except BaseException:
        _release_after_failure(lease, LeaseOutcome.NEVER_FETCHED)
        raise

    pump = LayerArrivalPump(
        source,
        sink,
        poll_interval_seconds=poll_interval_seconds,
        layer_timeout_seconds=layer_timeout_seconds,
    )
    try:
        pump.run_resumable(fetch.plan)
    except LoadLeftOpenError as exc:
        _finish_from_whole_objects(loader, lease, sink, objects, exc)
        return PipelinedRetrieveResult(fetch, RetrieveCompletion.FELL_BACK)
    except LayerwiseContractError as exc:
        _wait_after_failure(loader)
        _release_after_failure(lease, LeaseOutcome.ABANDONED)
        if not sink.began:
            raise PipelinedRetrieveRefused(
                f"the transport refused this request: {exc}"
            ) from exc
        raise
    except BaseException:
        _wait_after_failure(loader)
        _release_after_failure(lease, LeaseOutcome.ABANDONED)
        raise

    try:
        loader.wait_for_copies()
    except BaseException:
        _release_after_failure(lease, LeaseOutcome.ABANDONED)
        raise
    lease.release(LeaseOutcome.FINISHED)
    return PipelinedRetrieveResult(fetch, RetrieveCompletion.PIPELINED)


def _finish_from_whole_objects(
    loader: PipelinedLoader,
    lease: WindowLease,
    sink: LayerLoadSink,
    objects: Sequence[ObjectToPlace],
    left_open: LoadLeftOpenError,
) -> None:
    """Continue a load the transport failed, from whole objects.

    Args:
        loader: Waits for copies and reloads the objects.
        lease: The window, not yet released.
        sink: The sink whose load is still open.
        objects: Every object the window held.
        left_open: The pump's report of the open load.

    Raises:
        BaseException: Whatever failed while continuing; the load has been
            abandoned and the window released ``ABANDONED``.
    """
    generation = left_open.generation
    logger.warning(
        "Pipelined fetch failed on layer %d; loading %d object(s) whole: %s",
        left_open.remaining_layers[0],
        len(objects),
        left_open.transport_error,
    )
    try:
        loader.wait_for_copies()
    except BaseException:
        _release_after_failure(lease, LeaseOutcome.ABANDONED)
        _abandon_after_failure(sink, generation)
        raise
    try:
        lease.release(LeaseOutcome.ABANDONED)
        loader.reload_whole(objects)
        for layer_id in left_open.remaining_layers:
            sink.load_layer(layer_id)
        sink.finish_load(generation)
    except BaseException:
        _abandon_after_failure(sink, generation)
        raise
