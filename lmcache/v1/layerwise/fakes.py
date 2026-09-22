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


class ScriptedLayerArrivalSource:
    """A transport whose arrivals are driven by the test, not by hardware.

    Every layer starts ``PENDING``. A test calls :meth:`deliver_layer` or
    :meth:`decline_layer` to move it on. Because nothing arrives on its own,
    a test that forgets to deliver a layer hangs its pump rather than passing
    by accident, which is the behaviour that makes missing-arrival bugs
    visible.

    Safe to drive from a thread other than the one polling it, so a test can
    run a real pump on one thread and script arrivals from another.
    """

    def __init__(self) -> None:
        """Build a source with no active fetch."""
        self._lock = threading.Lock()
        self._generation = 0
        self._next_generation = 1
        self._status: dict[int, LayerArrivalStatus] = {}
        self._finished_generations: list[int] = []
        self._abandoned_generations: list[int] = []

    def begin_fetch(self, plan: LayerFetchPlan) -> int:
        """Start a scripted fetch with every layer pending.

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
            self._status = {
                layer_id: LayerArrivalStatus.PENDING for layer_id in plan.layer_ids()
            }
            return self._generation

    def poll_layer(self, layer_id: int, generation: int) -> LayerArrivalStatus:
        """Return the scripted status of ``layer_id``.

        Args:
            layer_id: Global layer index in the model.
            generation: The generation returned by :meth:`begin_fetch`.

        Returns:
            Whatever the test last scripted for this layer.

        Raises:
            StaleGenerationError: If ``generation`` is not the active fetch.
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
        """
        with self._lock:
            self._require_active(generation)
            return self._require_layer(layer_id)

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

    def deliver_layer(self, layer_id: int) -> None:
        """Mark ``layer_id`` resident, as if its last slot had landed.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
            LayerwiseContractError: If no fetch is active.
        """
        self._set_status(layer_id, LayerArrivalStatus.RESIDENT)

    def decline_layer(self, layer_id: int) -> None:
        """Mark ``layer_id`` unservable, as if a node had refused a slot.

        Args:
            layer_id: Global layer index in the model.

        Raises:
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
            LayerwiseContractError: If no fetch is active.
        """
        self._set_status(layer_id, LayerArrivalStatus.UNSERVABLE)

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

    def _set_status(self, layer_id: int, status: LayerArrivalStatus) -> None:
        """Script one layer's status.

        Args:
            layer_id: Global layer index in the model.
            status: The status future polls should report.

        Raises:
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
            LayerwiseContractError: If no fetch is active.
        """
        with self._lock:
            if self._generation == 0:
                raise LayerwiseContractError("no fetch is active")
            self._require_layer(layer_id)
            self._status[layer_id] = status

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

    def _require_layer(self, layer_id: int) -> LayerArrivalStatus:
        """Return a layer's status, rejecting layers outside the plan.

        Args:
            layer_id: Global layer index in the model.

        Returns:
            The layer's current status.

        Raises:
            LayerNotInPlanError: If the plan does not cover ``layer_id``.
        """
        if layer_id not in self._status:
            raise LayerNotInPlanError(f"layer {layer_id} is not in this fetch plan")
        return self._status[layer_id]

    def _clear(self) -> None:
        """Drop all state for the active fetch."""
        self._generation = 0
        self._status = {}


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
