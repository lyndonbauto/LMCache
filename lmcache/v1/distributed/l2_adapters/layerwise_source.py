# SPDX-License-Identifier: Apache-2.0
"""Track A's side of the layerwise contract: arrival reporting over RDMA.

:class:`AerospikeLayerArrivalSource` presents the native pipelined fetch in
``csrc/storage_backends/aerospike/`` as a
:class:`~lmcache.v1.layerwise.contract.LayerArrivalSource`. The native side
already does the accounting that matters -- per-slot arrival, stale-generation
rejection, command chunking, receive-queue sizing. This module translates its
answers into the contract's terms:

- two native answers (ready, and the unservable-layer list) become the
  three-valued :class:`~lmcache.v1.layerwise.contract.LayerArrivalStatus`,
- wrong generations and unknown layers raise, where natively they return
  ``False``,
- native ``RuntimeError`` / ``ValueError`` / ``IndexError`` become
  :class:`~lmcache.v1.layerwise.contract.LayerwiseContractError`.

:class:`NativePlanIssuer` issues a
:class:`~lmcache.v1.layerwise.contract.LayerFetchPlan` slot for slot: each
slot goes to the node the plan names, and its position in the plan is its
notification slot. Issuing sits behind :class:`PlanIssuer` so the arrival
accounting can be tested without a native client.
"""

# Standard
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol, runtime_checkable
import threading

# First Party
from lmcache.v1.layerwise.contract import (
    NO_GENERATION,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerNotInPlanError,
    LayerwiseContractError,
    PlanTooLargeError,
    StaleGenerationError,
)


@contextmanager
def _as_contract_errors(action: str) -> Iterator[None]:
    """Re-raise native failures as :class:`LayerwiseContractError`.

    pybind11 maps ``std::runtime_error`` to ``RuntimeError``,
    ``std::invalid_argument`` and ``std::length_error`` to ``ValueError``, and
    ``std::out_of_range`` to ``IndexError``. Callers of the contract catch
    ``LayerwiseContractError`` to decide whether to fall back, so a native
    exception leaking through would bypass the fallback.

    :class:`NotImplementedError` subclasses ``RuntimeError`` but is passed
    through unchanged: it means this adapter is incomplete, and disguising
    that as a recoverable backend failure would hide it behind the fallback.

    Args:
        action: What was being attempted, for the error message.

    Raises:
        LayerwiseContractError: Wrapping any native failure.
        NotImplementedError: Unchanged.
    """
    try:
        yield
    except NotImplementedError:
        raise
    except (RuntimeError, ValueError, IndexError) as exc:
        raise LayerwiseContractError(f"native {action} failed: {exc}") from exc


@runtime_checkable
class PipelinedFetchConnector(Protocol):
    """The pipelined-fetch surface of the native Aerospike client.

    Matches the methods ``lmcache_aerospike.LMCacheAerospikeClient`` exposes
    when built with ``LMCACHE_AEROSPIKE_RDMA``. Declared here so the adapter
    can be tested without the native extension. Issuing is deliberately
    absent: it is reached only through :class:`PlanIssuer`.
    """

    def pipelined_fetch_ready(self) -> bool:
        """Return whether pipelined fetch is initialized and usable."""
        ...

    def pipelined_fetch_init_error(self) -> str:
        """Return why pipelined fetch is unavailable, or ``""``."""
        ...

    def is_pipelined_layer_ready(self, layer_id: int, request_generation: int) -> bool:
        """Drain arrivals, then return whether every slot of a layer landed."""
        ...

    def pipelined_unservable_layers(self) -> list[int]:
        """Return layers of the active fetch that were declined or lost."""
        ...

    def finish_pipelined_fetch(self) -> None:
        """Drop the active fetch after completion."""
        ...

    def abandon_pipelined_fetch(self) -> None:
        """Drop the active fetch without waiting for outstanding slots."""
        ...


@runtime_checkable
class PlannedFetchConnector(Protocol):
    """The native entry point that issues a caller-planned fetch.

    Matches ``issue_pipelined_fetch_by_slots`` and
    ``pipelined_max_slots_per_request`` on
    ``lmcache_aerospike.LMCacheAerospikeClient`` built with
    ``LMCACHE_AEROSPIKE_RDMA``.
    """

    def issue_pipelined_fetch_by_slots(
        self,
        node_names: Sequence[str],
        slots: Sequence[tuple[int, str, int, int, int]],
    ) -> int:
        """Begin a fetch of exactly ``slots`` and return its generation.

        ``slots[i]`` is notification slot ``i`` and reads
        ``(node_index, record_key, dest_offset, length, layer_id)``.
        """
        ...

    def pipelined_max_slots_per_request(self) -> int:
        """Return the most slots one fetch may carry, or 0 when not ready."""
        ...


@runtime_checkable
class PlanIssuer(Protocol):
    """Starts the native fetch described by a contract plan.

    Kept as a seam so the arrival accounting can be tested without a native
    client; production uses :class:`NativePlanIssuer`.
    """

    def issue(self, plan: LayerFetchPlan) -> int:
        """Issue every slot in ``plan`` and return the native generation.

        Args:
            plan: The slots to fetch.

        Returns:
            The generation the native session allocated.

        Raises:
            PlanTooLargeError: If the plan is larger than the device's
                receive queue can accept.
            RuntimeError, ValueError, IndexError: Other native failures.
        """
        ...


class NativePlanIssuer:
    """Issues a contract plan through the native client, slot for slot.

    The plan is the only source of truth: each slot is sent to the node the
    plan names, at the offset the plan gives, as the notification slot its
    position gives. Nothing is re-planned natively, so the slot numbers on
    the wire are the plan's.
    """

    def __init__(self, connector: PlannedFetchConnector) -> None:
        """Build an issuer over one native client.

        Args:
            connector: The native client the fetch is issued through.
        """
        self._connector = connector

    def issue(self, plan: LayerFetchPlan) -> int:
        """Flatten ``plan`` and issue it.

        Args:
            plan: The slots to fetch. ``plan.slots[i]`` becomes notification
                slot ``i``; offsets are relative to the registered window.

        Returns:
            The native generation.

        Raises:
            PlanTooLargeError: If the plan has more slots than the device can
                post receives for. Checked before anything is sent, because
                on RC with ``rnr_retry = 7`` a shortfall is an infinite retry
                rather than an error. The native session's own refusal is
                bound as a subclass of the same error.
            RuntimeError, ValueError, IndexError: Native failures, e.g. a
                node with no kv-sink registration or a slot outside the
                window.
        """
        max_slots = self._connector.pipelined_max_slots_per_request()
        if max_slots and len(plan.slots) > max_slots:
            raise PlanTooLargeError(
                f"fetch plan has {len(plan.slots)} slots but the device accepts "
                f"at most {max_slots} per request; split the fetch"
            )
        slots = [
            (s.node_index, s.record_key, s.offset, s.length, s.layer_id)
            for s in plan.slots
        ]
        return self._connector.issue_pipelined_fetch_by_slots(plan.node_names, slots)


class AerospikeLayerArrivalSource:
    """Reports layer arrival for a pipelined Aerospike RDMA fetch.

    Implements :class:`~lmcache.v1.layerwise.contract.LayerArrivalSource`. See
    that protocol for the full contract; the notes here are the parts specific
    to this transport.

    A layer is resident once every slot carrying part of it has landed. The
    server writes each slot with ``RDMA_WRITE_WITH_IMM`` and the immediate
    encodes ``(generation << 16) | slot``, so arrivals are counted per slot
    and attributed to a generation. The generation returned by
    :meth:`begin_fetch` is the native one, so it matches what appears on the
    wire.

    Instances hold at most one active fetch. Every method takes one lock, so
    concurrent :meth:`poll_layer` calls are safe; the native side serializes
    them anyway.
    """

    def __init__(self, connector: PipelinedFetchConnector, issuer: PlanIssuer) -> None:
        """Build a source with no active fetch.

        Args:
            connector: The native client that reports arrivals.
            issuer: Starts the native fetch for a plan, normally a
                :class:`NativePlanIssuer` over the same ``connector``.
        """
        self._connector = connector
        self._issuer = issuer
        self._lock = threading.Lock()
        self._generation = NO_GENERATION
        self._layer_ids: frozenset[int] = frozenset()

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        """Issue every slot in ``plan`` to the nodes that hold it.

        The native side rejects a plan with more slots than the device can
        post receives for, rather than letting it deadlock: on RC with
        ``rnr_retry = 7`` a shortfall is infinite retry. It also splits each
        node's sinks into commands no larger than that node's advertised
        ``max_sinks``.

        Args:
            plan: The slots to fetch.

        Returns:
            A non-zero generation for this fetch.

        Raises:
            PlanTooLargeError: If the plan exceeds what the device can
                accept. Nothing was issued, and a smaller plan may succeed.
            LayerwiseContractError: If a fetch is already active, if the
                backend has no pipelined path, or if issuing fails.
        """
        with self._lock:
            if self._generation != NO_GENERATION:
                raise LayerwiseContractError(
                    f"fetch generation {self._generation} is still active"
                )
            with _as_contract_errors("readiness check"):
                ready = self._connector.pipelined_fetch_ready()
                reason = "" if ready else self._connector.pipelined_fetch_init_error()
            if not ready:
                raise LayerwiseContractError(
                    f"pipelined fetch is unavailable: {reason or 'no reason given'}"
                )
            with _as_contract_errors("fetch issue"):
                generation = self._issuer.issue(plan)
            if generation == NO_GENERATION:
                with _as_contract_errors("abandon"):
                    self._connector.abandon_pipelined_fetch()
                raise LayerwiseContractError(
                    "native fetch returned the reserved generation "
                    f"{NO_GENERATION}; refusing to track it"
                )
            self._generation = generation
            self._layer_ids = frozenset(plan.layer_ids())
            return generation

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        """Report whether every slot of ``layer_id`` has landed.

        Returns ``RESIDENT`` only when the layer is wholly present, and
        promptly once it is: each call drains pending arrivals before
        answering. A write belonging to an abandoned generation cannot move
        this fetch's accounting, because the native session discards
        immediates whose generation is not the active one.

        Args:
            layer_id: Global layer index in the model.
            generation: The generation returned by :meth:`begin_fetch`.

        Returns:
            The arrival status of ``layer_id``.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
            LayerNotInPlanError: If the active plan does not cover ``layer_id``.
            LayerwiseContractError: If the native query fails.
        """
        with self._lock:
            self._require_active(generation)
            if layer_id not in self._layer_ids:
                raise LayerNotInPlanError(f"layer {layer_id} is not in this fetch plan")
            with _as_contract_errors("readiness poll"):
                if self._connector.is_pipelined_layer_ready(layer_id, generation):
                    return LayerArrivalStatus.RESIDENT
                unservable = self._connector.pipelined_unservable_layers()
            if layer_id in unservable:
                return LayerArrivalStatus.UNSERVABLE
            return LayerArrivalStatus.PENDING

    def finish_fetch(self, generation: int) -> None:
        """Release a fetch whose layers have all been consumed.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
            LayerwiseContractError: If the native release fails. The fetch is
                no longer tracked here either way.
        """
        with self._lock:
            self._require_active(generation)
            try:
                with _as_contract_errors("finish"):
                    self._connector.finish_pipelined_fetch()
            finally:
                self._clear()

    def abandon_fetch(self, generation: int) -> None:
        """Release a fetch without waiting for its outstanding slots.

        Tolerates a generation that has already been finished or abandoned,
        so error paths can unwind without first working out how far the
        fetch got. Writes still in flight for the abandoned generation may
        land later; the native session ignores them.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.

        Raises:
            LayerwiseContractError: If the native release fails. The fetch is
                no longer tracked here either way.
        """
        with self._lock:
            if generation == NO_GENERATION or generation != self._generation:
                return
            try:
                with _as_contract_errors("abandon"):
                    self._connector.abandon_pipelined_fetch()
            finally:
                self._clear()

    def _require_active(self, generation: int) -> None:
        """Reject a call that does not name the active fetch.

        Args:
            generation: The generation the caller believes is active.

        Raises:
            StaleGenerationError: If it is not the active one.
        """
        if generation == NO_GENERATION or generation != self._generation:
            raise StaleGenerationError(
                f"generation {generation} is not active (active is {self._generation})"
            )

    def _clear(self) -> None:
        """Drop all state for the active fetch."""
        self._generation = NO_GENERATION
        self._layer_ids = frozenset()
