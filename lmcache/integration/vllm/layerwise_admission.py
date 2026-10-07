# SPDX-License-Identifier: Apache-2.0
"""Admission of layerwise loads into scheduler steps under a byte budget.

In layerwise mode every cache-hit request is scheduled into the next step,
and that step's forward pass waits at each layer for the slowest of its
loads. When the fetch path is bandwidth-bound, all of a step's loads finish
together, so every request in it gets about the whole batch's transfer time
as its TTFT. Bounding the bytes a step loads splits a burst into groups, so
the first groups get their first token early.

A step's loads all end inside that step: its forward pass cannot finish
before every layer has arrived, failed or been abandoned, and the worker
runs steps one after another. So the bytes being fetched at any time are
the bytes admitted into the current step, and the budget is accounted per
step. Nothing carries over from one step to the next, so no exit path of a
request (success, failure, whole-object fallback, abort, preemption) can
leak budget.
"""

# First Party
from lmcache.utils import init_logger

logger = init_logger(__name__)

#: Default ``lmcache.mp.layerwise_inflight_budget_bytes``.
DEFAULT_LAYERWISE_INFLIGHT_BUDGET_BYTES = 4 << 30


class LayerwiseAdmissionGate:
    """Decides, per scheduler step, which layerwise loads start in it.

    vLLM asks about waiting requests in its scheduling order. The gate
    admits them in that order while the step's admitted bytes fit the
    budget. The first request of a step is always admitted, so a request
    larger than the budget cannot starve. Once one request is held, every
    later request of the step is held too, so a small request cannot
    overtake a large one.

    Not thread-safe: vLLM's scheduler calls it from one thread.
    """

    def __init__(self, budget_bytes: int) -> None:
        """Create a gate.

        Args:
            budget_bytes: Bytes of layerwise loads one step may start. 0
                disables the gate: every request is admitted.

        Raises:
            ValueError: If ``budget_bytes`` is negative.
        """
        if budget_bytes < 0:
            raise ValueError(f"budget_bytes must be non-negative, got {budget_bytes}")
        self._budget_bytes = budget_bytes
        self._admitted: dict[str, int] = {}
        self._admitted_bytes = 0
        self._holding = False

    @property
    def enabled(self) -> bool:
        """Whether the gate can hold a request back."""
        return self._budget_bytes > 0

    @property
    def admitted_bytes(self) -> int:
        """Bytes of the loads admitted in the current step."""
        return self._admitted_bytes

    def admit(self, request_id: str, request_bytes: int) -> bool:
        """Decide whether a request's layerwise load starts in this step.

        Asking again about a request admitted in this step admits it again
        without counting its bytes twice.

        Args:
            request_id: The vLLM request ID.
            request_bytes: Bytes the request's load fetches.

        Returns:
            True if the request is scheduled in this step; False if it
            waits for a later step.

        Raises:
            ValueError: If ``request_bytes`` is negative.
        """
        if request_bytes < 0:
            raise ValueError(f"request_bytes must be non-negative, got {request_bytes}")
        if not self.enabled or request_id in self._admitted:
            return True
        if self._holding:
            return False
        fits = self._admitted_bytes + request_bytes <= self._budget_bytes
        if self._admitted and not fits:
            self._holding = True
            logger.debug(
                "Holding layerwise load of request %s (%d B) for a later "
                "step: %d B already admitted, budget %d B",
                request_id,
                request_bytes,
                self._admitted_bytes,
                self._budget_bytes,
            )
            return False
        self._admitted[request_id] = request_bytes
        self._admitted_bytes += request_bytes
        return True

    def end_step(self) -> None:
        """Close the current step; the next step starts with the full budget."""
        self._admitted.clear()
        self._admitted_bytes = 0
        self._holding = False
