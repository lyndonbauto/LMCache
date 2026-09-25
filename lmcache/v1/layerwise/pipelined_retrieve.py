# SPDX-License-Identifier: Apache-2.0
"""Run one retrieve's pipelined fetch: lease, plan, pump, release.

The retrieve path needs one call that either delivers every layer of a
request to the loader or raises something it can fall back on. Refusals come
from three places -- the placer (no window, or the request too large), the
planner (a request the layout cannot name layer by layer), and the pump (the
transport or loader failing mid-fetch) -- and all three surface here as
:class:`~lmcache.v1.layerwise.contract.LayerwiseContractError`, so the caller
needs a single handler::

    try:
        run_pipelined_retrieve(model, keys, cap, placer, pump)
    except LayerwiseContractError:
        ...  # whole-object load into fresh general-L1 objects

The window is released exactly once, with the outcome that decides whether it
can be reused at once: see :class:`~.request_fetch.LeaseOutcome`.

Kept out of the package ``__init__`` for the same import-cycle reason as
:mod:`~lmcache.v1.layerwise.request_fetch`.
"""

# Standard
from collections.abc import Sequence

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.layerwise.contract import LayerwiseContractError
from lmcache.v1.layerwise.pump import LayerArrivalPump
from lmcache.v1.layerwise.request_fetch import (
    ChunkPlacer,
    FetchModel,
    LeaseOutcome,
    RequestFetch,
    WindowLease,
    build_request_fetch,
    objects_to_place,
)

logger = init_logger(__name__)


def _release_after_failure(lease: WindowLease, outcome: LeaseOutcome) -> None:
    """Release a lease on an error path without masking the original error."""
    try:
        lease.release(outcome)
    except Exception:
        logger.exception("Releasing a window lease as %s failed", outcome.name)


def run_pipelined_retrieve(
    model: FetchModel,
    obj_keys_per_obj_group: Sequence[Sequence[ObjectKey]],
    max_record_bytes: int,
    placer: ChunkPlacer,
    pump: LayerArrivalPump,
) -> RequestFetch:
    """Fetch every object a retrieve reads, handing layers to the loader.

    Leases a window for the request's objects, plans the fetch into it, and
    runs ``pump`` over the plan. Blocks until every layer has been loaded or
    the fetch has failed, so call it from a worker thread, not the request
    handler.

    The lease is released exactly once: as ``NEVER_FETCHED`` if planning
    failed, ``ABANDONED`` if the pump raised, since writes may still be on the
    wire, and ``FINISHED`` otherwise. If releasing fails after a failure, the
    release error is logged and the original error raised.

    Args:
        model: The registered model's layout and windows.
        obj_keys_per_obj_group: The request's object keys, one list per
            object group, as ``MPCacheServerContext.resolve_obj_keys``
            returns them with a worker id.
        max_record_bytes: The record cap the objects were written under.
        placer: Leases the request's window and places its objects.
        pump: Joins the transport and the loader for this request.

    Returns:
        The placements and plan that were fetched, for diagnostics.

    Raises:
        LayerwiseContractError: If the request cannot be served layer by
            layer -- the placer refused it (``PlanTooLargeError`` when it
            would fit only if split), the layout cannot plan it, or the
            transport or loader failed. The caller should load whole objects
            instead; both sides of the pump have already been abandoned.
        BaseException: Anything else the pump or loader raises propagates
            unchanged, after the lease is released as abandoned.
    """
    try:
        objects = objects_to_place(model, obj_keys_per_obj_group)
    except (ValueError, KeyError) as exc:
        raise LayerwiseContractError(
            f"cannot plan a layerwise fetch for this request: {exc}"
        ) from exc

    lease = placer.lease(objects)

    try:
        fetch = build_request_fetch(
            model, obj_keys_per_obj_group, max_record_bytes, lease
        )
    except (ValueError, KeyError) as exc:
        _release_after_failure(lease, LeaseOutcome.NEVER_FETCHED)
        raise LayerwiseContractError(
            f"cannot plan a layerwise fetch into the leased window: {exc}"
        ) from exc
    except BaseException:
        _release_after_failure(lease, LeaseOutcome.NEVER_FETCHED)
        raise

    try:
        pump.run(fetch.plan)
    except BaseException:
        _release_after_failure(lease, LeaseOutcome.ABANDONED)
        raise

    lease.release(LeaseOutcome.FINISHED)
    return fetch
