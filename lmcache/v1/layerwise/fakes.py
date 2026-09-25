# SPDX-License-Identifier: Apache-2.0
"""Substitutes for each side of the layerwise contract.

These ship as library code rather than test helpers because the point of the
contract is that each workstream can build against the other before the other
exists. The transport team needs a loader that runs without a GPU; the loader
team needs a transport that runs without an RDMA fabric. Keeping the fakes
next to the protocols they implement means a change to a protocol breaks its
fake immediately, rather than in some other package's test suite.

Both fakes enforce the contract's ordering and generation rules, so code that
passes against a fake and then fails against the real implementation has found
a bug in the real implementation, not in its own assumptions.
"""

# Standard
from collections.abc import Sequence
from typing import Protocol, runtime_checkable
import threading

# Local
from .contract import (
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerNotInPlanError,
    LayerUnservableError,
    LayerwiseContractError,
    StaleGenerationError,
)


@runtime_checkable
class ArrivalDriver(Protocol):
    """Makes slots of a :class:`LayerArrivalSource`'s fetch land or fail.

    The conformance suite in ``tests/v1/layerwise/`` drives every source
    implementation through this, so the same tests pin the same behaviour for
    the scripted source and a real transport. A real transport's driver feeds
    what the fabric would deliver -- an encoded immediate for a landed slot, a
    node's decline for a refused one -- into the real session, without a
    fabric.

    Slots are named by index, the position in :attr:`LayerFetchPlan.slots`,
    because that is what the wire carries.

    Like the wire, a driver cannot fail on a stale arrival: a slot quoting a
    generation that is not the active fetch must be dropped, not raised,
    since a real late write has no caller to raise to.
    """

    def land_slot(self, slot_index: int, generation: int) -> None:
        """Deliver one slot's data, as the fabric would.

        Args:
            slot_index: Position of the slot in the fetch's plan.
            generation: The generation the arrival is tagged with.
        """
        ...

    def decline_slot(self, slot_index: int, generation: int) -> None:
        """Report that a node refused or lost one slot.

        Args:
            slot_index: Position of the slot in the fetch's plan.
            generation: The generation the reply is tagged with.
        """
        ...


class ScriptedLayerArrivalSource:
    """A transport whose arrivals are driven by the test, not by hardware.

    Every layer starts ``PENDING``. A layer is ``RESIDENT`` once every one of
    its slots has landed, and ``UNSERVABLE`` as soon as any of them is
    declined. A test moves slots on with :meth:`land_slot` and
    :meth:`decline_slot`, or a whole layer at once with :meth:`deliver_layer`
    and :meth:`decline_layer`. Because nothing arrives on its own, a test
    that forgets to deliver a layer hangs its pump rather than passing by
    accident, which is the behaviour that makes missing-arrival bugs visible.

    Safe to drive from a thread other than the one polling it, so a test can
    run a real pump on one thread and script arrivals from another.
    """

    def __init__(self) -> None:
        """Build a source with no active fetch."""
        self._lock = threading.Lock()
        self._generation = 0
        self._next_generation = 1
        self._slot_layers: tuple[int, ...] = ()
        self._pending_slots: dict[int, set[int]] = {}
        self._declined_layers: set[int] = set()
        self._finished_generations: list[int] = []
        self._abandoned_generations: list[int] = []

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        """Start a scripted fetch with every slot outstanding.

        Args:
            plan: The slots to pretend to fetch.

        Returns:
            A fresh non-zero generation.

        Raises:
            LayerwiseContractError: If a fetch is already active.
        """
        with self._lock:
            if self._generation != 0:
                raise LayerwiseContractError(
                    f"fetch generation {self._generation} is still active"
                )
            self._generation = self._next_generation
            self._next_generation += 1
            self._slot_layers = tuple(slot.layer_id for slot in plan.slots)
            self._pending_slots = {layer_id: set() for layer_id in plan.layer_ids()}
            for slot_index, layer_id in enumerate(self._slot_layers):
                self._pending_slots[layer_id].add(slot_index)
            self._declined_layers = set()
            return self._generation

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        """Return the scripted status of ``layer_id``.

        Args:
            layer_id: Global layer index in the model.
            generation: The generation returned by :meth:`begin_fetch`.

        Returns:
            ``UNSERVABLE`` if any slot of the layer was declined, else
            ``RESIDENT`` if all of them landed, else ``PENDING``.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
        """
        with self._lock:
            self._require_active(generation)
            pending = self._require_layer(layer_id)
            if layer_id in self._declined_layers:
                return LayerArrivalStatus.UNSERVABLE
            if not pending:
                return LayerArrivalStatus.RESIDENT
            return LayerArrivalStatus.PENDING

    def finish_fetch(self, generation: int) -> None:
        """Record a clean completion and clear the active fetch.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
        """
        with self._lock:
            self._require_active(generation)
            self._finished_generations.append(generation)
            self._clear()

    def abandon_fetch(self, generation: int) -> None:
        """Record an abandon and clear the active fetch.

        Tolerates a generation that is already gone, as the contract requires.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.
        """
        with self._lock:
            self._abandoned_generations.append(generation)
            if generation == self._generation:
                self._clear()

    def land_slot(self, slot_index: int, generation: int) -> None:
        """Land one slot of the active fetch.

        An arrival quoting any other generation, or arriving with no fetch
        active, is dropped, as a late write on the wire would be.

        Args:
            slot_index: Position of the slot in the active plan.
            generation: The generation the arrival is tagged with.

        Raises:
            ValueError: If ``slot_index`` is outside the active plan.
        """
        with self._lock:
            if generation == 0 or generation != self._generation:
                return
            layer_id = self._layer_of_slot(slot_index)
            self._pending_slots[layer_id].discard(slot_index)

    def decline_slot(self, slot_index: int, generation: int) -> None:
        """Decline one slot of the active fetch, making its layer unservable.

        A reply quoting any other generation is dropped.

        Args:
            slot_index: Position of the slot in the active plan.
            generation: The generation the reply is tagged with.

        Raises:
            ValueError: If ``slot_index`` is outside the active plan.
        """
        with self._lock:
            if generation == 0 or generation != self._generation:
                return
            self._declined_layers.add(self._layer_of_slot(slot_index))

    def deliver_layer(self, layer_id: int) -> None:
        """Land every remaining slot of ``layer_id`` in the active fetch.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
            LayerwiseContractError: If no fetch is active.
        """
        with self._lock:
            self._require_any_active()
            self._require_layer(layer_id).clear()

    def decline_layer(self, layer_id: int) -> None:
        """Mark ``layer_id`` unservable, as if a node had refused a slot.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
            LayerwiseContractError: If no fetch is active.
        """
        with self._lock:
            self._require_any_active()
            self._require_layer(layer_id)
            self._declined_layers.add(layer_id)

    def finished_generations(self) -> tuple[int, ...]:
        """Return the generations that were cleanly finished, in order.

        Returns:
            One entry per :meth:`finish_fetch` call.
        """
        with self._lock:
            return tuple(self._finished_generations)

    def abandoned_generations(self) -> tuple[int, ...]:
        """Return the generations that were abandoned, in order.

        Returns:
            One entry per :meth:`abandon_fetch` call, including repeats.
        """
        with self._lock:
            return tuple(self._abandoned_generations)

    def _require_any_active(self) -> None:
        """Reject scripting a layer when there is no fetch to script.

        Raises:
            LayerwiseContractError: If no fetch is active.
        """
        if self._generation == 0:
            raise LayerwiseContractError("no fetch is active")

    def _layer_of_slot(self, slot_index: int) -> int:
        """Return the layer a slot of the active plan carries.

        Args:
            slot_index: Position of the slot in the active plan.

        Returns:
            The slot's global layer index.

        Raises:
            ValueError: If ``slot_index`` is outside the active plan.
        """
        if not 0 <= slot_index < len(self._slot_layers):
            raise ValueError(
                f"slot {slot_index} is outside the active plan's "
                f"{len(self._slot_layers)} slots"
            )
        return self._slot_layers[slot_index]

    def _require_active(self, generation: int) -> None:
        """Reject a call that does not name the active fetch.

        Args:
            generation: The generation the caller believes is active.

        Raises:
            StaleGenerationError: If it is not the active one.
        """
        if generation != self._generation or generation == 0:
            raise StaleGenerationError(
                f"generation {generation} is not active (active is {self._generation})"
            )

    def _require_layer(self, layer_id: int) -> set[int]:
        """Return a layer's outstanding slots, rejecting layers outside the plan.

        Args:
            layer_id: Global layer index in the model.

        Returns:
            The live set of the layer's slot indices that have not landed.

        Raises:
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
        """
        if layer_id not in self._pending_slots:
            raise LayerNotInPlanError(f"layer {layer_id} is not in this fetch plan")
        return self._pending_slots[layer_id]

    def _clear(self) -> None:
        """Drop all state for the active fetch."""
        self._generation = 0
        self._slot_layers = ()
        self._pending_slots = {}
        self._declined_layers = set()


class ScriptedArrivalDriver:
    """The :class:`ArrivalDriver` for a :class:`ScriptedLayerArrivalSource`."""

    def __init__(self, source: ScriptedLayerArrivalSource) -> None:
        """Build a driver for one scripted source.

        Args:
            source: The source whose slots this driver lands and declines.
        """
        self._source = source

    def land_slot(self, slot_index: int, generation: int) -> None:
        """Land one slot; see :meth:`ScriptedLayerArrivalSource.land_slot`.

        Args:
            slot_index: Position of the slot in the fetch's plan.
            generation: The generation the arrival is tagged with.
        """
        self._source.land_slot(slot_index, generation)

    def decline_slot(self, slot_index: int, generation: int) -> None:
        """Decline one slot; see :meth:`ScriptedLayerArrivalSource.decline_slot`.

        Args:
            slot_index: Position of the slot in the fetch's plan.
            generation: The generation the reply is tagged with.
        """
        self._source.decline_slot(slot_index, generation)


class RecordingLayerLoadSink:
    """A loader that records the copies it was asked to make.

    Stands in for the GPU-side loader on machines without one. It enforces the
    contract's ordering rule, so a caller that issues layers out of order
    fails here rather than producing a silently wrong result on real hardware,
    where out-of-order issue merely makes a layer look ready early.
    """

    def __init__(self) -> None:
        """Build a sink with no active load."""
        self._generation = 0
        self._expected: tuple[int, ...] = ()
        self._next_index = 0
        self._loaded: list[int] = []
        self._finished_generations: list[int] = []
        self._abandoned_generations: list[int] = []

    def begin_load(self, generation: int, layer_ids: Sequence[int]) -> None:
        """Record the start of a load.

        Args:
            generation: The fetch generation these layers belong to.
            layer_ids: Global layer indices in the order they will be loaded.

        Raises:
            LayerwiseContractError: If a load is already in progress.
        """
        if self._generation != 0:
            raise LayerwiseContractError(
                f"load for generation {self._generation} is still active"
            )
        self._generation = generation
        self._expected = tuple(layer_ids)
        self._next_index = 0
        self._loaded = []

    def load_layer(self, layer_id: int) -> None:
        """Record a copy of ``layer_id``.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerwiseContractError: If no load is active, or every expected
                layer has already been issued.
            LayerNotInPlanError: If ``layer_id`` is not the next expected
                layer, which covers both unknown layers and out-of-order ones.
        """
        if self._generation == 0:
            raise LayerwiseContractError("no load is active")
        if self._next_index >= len(self._expected):
            raise LayerwiseContractError(
                f"layer {layer_id} issued after all "
                f"{len(self._expected)} expected layers"
            )
        expected = self._expected[self._next_index]
        if layer_id != expected:
            raise LayerNotInPlanError(f"expected layer {expected} next, got {layer_id}")
        self._next_index += 1
        self._loaded.append(layer_id)

    def finish_load(self, generation: int) -> None:
        """Record a clean completion.

        Args:
            generation: The generation passed to :meth:`begin_load`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active load.
            LayerwiseContractError: If some expected layer was never issued.
        """
        if generation != self._generation or generation == 0:
            raise StaleGenerationError(
                f"generation {generation} is not the active load "
                f"(active is {self._generation})"
            )
        if self._next_index != len(self._expected):
            missing = self._expected[self._next_index :]
            raise LayerwiseContractError(
                f"load finished with layers {list(missing)} never issued"
            )
        self._finished_generations.append(generation)
        self._reset()

    def abandon_load(self, generation: int) -> None:
        """Record an abandon and clear the active load.

        Tolerates being called with no load active, as the contract requires.

        Args:
            generation: The generation passed to :meth:`begin_load`.
        """
        self._abandoned_generations.append(generation)
        if generation == self._generation:
            self._reset()

    def loaded_layers(self) -> tuple[int, ...]:
        """Return the layers issued during the active or last-finished load.

        Returns:
            Global layer indices in the order they were issued.
        """
        return tuple(self._loaded)

    def finished_generations(self) -> tuple[int, ...]:
        """Return the generations that were cleanly finished, in order.

        Returns:
            One entry per :meth:`finish_load` call.
        """
        return tuple(self._finished_generations)

    def abandoned_generations(self) -> tuple[int, ...]:
        """Return the generations that were abandoned, in order.

        Returns:
            One entry per :meth:`abandon_load` call, including repeats.
        """
        return tuple(self._abandoned_generations)

    def _reset(self) -> None:
        """Drop the active load, keeping the record of issued layers."""
        self._generation = 0
        self._expected = ()
        self._next_index = 0


class UnservableLayerArrivalSource:
    """A transport that declines every layer immediately.

    Used to check that callers really do have a fallback path. A caller tested
    only against :class:`ScriptedLayerArrivalSource` can pass while its
    fallback is unreachable, because nothing ever forced it to be taken.
    """

    def __init__(self) -> None:
        """Build a source that refuses everything."""
        self._generation = 0
        self._layer_ids: tuple[int, ...] = ()

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        """Accept the fetch so the caller reaches the decline on poll.

        Args:
            plan: The slots that will not be served.

        Returns:
            A fixed non-zero generation.
        """
        self._generation = 1
        self._layer_ids = plan.layer_ids()
        return self._generation

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        """Report ``layer_id`` unservable.

        Args:
            layer_id: Global layer index in the model.
            generation: The generation returned by :meth:`begin_fetch`.

        Returns:
            Always :attr:`LayerArrivalStatus.UNSERVABLE`.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
        """
        if generation != self._generation or generation == 0:
            raise StaleGenerationError(f"generation {generation} is not active")
        if layer_id not in self._layer_ids:
            raise LayerNotInPlanError(f"layer {layer_id} is not in this fetch plan")
        return LayerArrivalStatus.UNSERVABLE

    def finish_fetch(self, generation: int) -> None:
        """Reject finishing, since no layer of this fetch can complete.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.

        Raises:
            LayerUnservableError: Always.
        """
        raise LayerUnservableError(
            f"generation {generation} served no layers and cannot be finished"
        )

    def abandon_fetch(self, generation: int) -> None:
        """Clear the active fetch.

        Args:
            generation: The generation returned by :meth:`begin_fetch`.
        """
        if generation == self._generation:
            self._generation = 0
            self._layer_ids = ()
