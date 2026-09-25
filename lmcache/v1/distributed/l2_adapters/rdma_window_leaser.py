# SPDX-License-Identifier: Apache-2.0
"""Hands out the RDMA receive windows to pipelined retrieves.

A pipelined retrieve allocates its L1 objects inside one leased window and the
data stays there after the fetch. So a window is not simply "free" or "in
use": after a clean fetch it holds cache entries that nothing may be reading.
:meth:`RdmaWindowLeaser.lease` reclaims such a window by deleting its objects,
which is safe because they were just read from L2 and can be read again.

A window released after an abandoned fetch is quarantined instead: the NIC can
still perform writes that were on the wire, and a new fetch must not have its
objects in bytes a late write can reach.

See "Window lifecycle" in
``docs/design/v1/layerwise/track-a-questions-for-track-c.md`` (W1 and W3).
"""

# Standard
from collections.abc import Callable
from dataclasses import dataclass
import enum
import threading
import time

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.error import L1Error
from lmcache.v1.distributed.internal_api import L1Pool
from lmcache.v1.distributed.l1_manager import L1Manager
from lmcache.v1.distributed.l2_adapters.rdma_registration import L1RdmaConfig
from lmcache.v1.layerwise.contract import LayerwiseContractError, PlanTooLargeError

logger = init_logger(__name__)


class FetchOutcome(enum.Enum):
    """How the fetch that held a window ended."""

    FINISHED = enum.auto()
    """Every layer became resident, so no write can still be on the wire."""

    ABANDONED = enum.auto()
    """Any other exit: unservable layer, timeout, error, or cancellation.

    Includes a fetch with a declined slot. A declined slot is never written,
    but treating every non-finished fetch alike keeps the rule simple.
    """


@dataclass(frozen=True)
class WindowLease:
    """One window held by one pipelined retrieve.

    Attributes:
        window_index: Which window is leased.
        base_offset: Byte offset of the window from the start of the L1 slab.
        size_bytes: Size of the window.
        lease_id: Distinguishes this lease from earlier leases of the same
            window, so a stale lease cannot release a newer one.
    """

    window_index: int
    base_offset: int
    size_bytes: int
    lease_id: int

    def pool(self) -> L1Pool:
        """Return the L1 pool to pass to ``reserve_write`` for this window.

        Returns:
            The RDMA window pool of :attr:`window_index`.
        """
        return L1Pool.rdma_window(self.window_index)


class RdmaWindowLeaser:
    """Leases RDMA windows, reclaiming idle ones and quarantining abandoned ones.

    At most one lease is outstanding at a time, because the native transport
    runs one pipelined fetch at a time (W3). A second retrieve is refused and
    falls back to a whole-object load.

    Choosing a window, among those not quarantined:

    1. an empty window, if any;
    2. otherwise the window released longest ago whose objects are all
       unlocked. Its objects are deleted through L1's normal delete path, so
       the usual eviction events reach listeners.

    "Released longest ago" approximates least recently used. Reads of a
    window's objects after the fetch do not refresh it.

    Thread-safe. Lock order is leaser, then L1; L1 never calls the leaser.
    """

    def __init__(
        self,
        l1_manager: L1Manager,
        rdma_config: L1RdmaConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a leaser over the windows ``l1_manager`` reserves.

        Args:
            l1_manager: The L1 whose windows are leased. Must reserve exactly
                ``rdma_config.window_plan.window_count`` windows.
            rdma_config: Supplies the window size and the quarantine length,
                which is ``fetch_timeout_seconds``: once that has passed since
                an abandon, no write of the abandoned fetch can still land.
            clock: Monotonic time source in seconds, injectable for tests.

        Raises:
            ValueError: If RDMA is not enabled in ``rdma_config``, or the L1
                reserves a different number of windows than its plan.
        """
        if not rdma_config.is_enabled():
            raise ValueError("RdmaWindowLeaser needs RDMA to be enabled")
        plan = rdma_config.window_plan
        reserved = l1_manager.get_rdma_window_count()
        if reserved != plan.window_count:
            raise ValueError(
                f"the RDMA plan has {plan.window_count} windows but L1 "
                f"reserves {reserved}"
            )
        self._l1_manager = l1_manager
        self._window_bytes = plan.window_bytes
        self._offsets = plan.window_offsets()
        self._quarantine_seconds = rdma_config.fetch_timeout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # Windows never released sort first, as least recently used.
        self._released_at = [float("-inf")] * plan.window_count
        self._quarantined_until = [float("-inf")] * plan.window_count
        self._outstanding: WindowLease | None = None
        self._next_lease_id = 1

    def lease(self, request_bytes: int) -> WindowLease:
        """Lease a window that can hold ``request_bytes``.

        May delete the objects of an idle window to free it; see the class
        docstring for which window is chosen.

        Args:
            request_bytes: Bytes the retrieve will allocate in the window.

        Returns:
            The lease. Allocate in it with ``reserve_write(...,
            pool=lease.pool())`` and end it with :meth:`release`.

        Raises:
            ValueError: If ``request_bytes`` is not positive.
            PlanTooLargeError: If ``request_bytes`` exceeds the window size.
                The caller can split the request.
            LayerwiseContractError: If a lease is already outstanding, or
                every window is quarantined or holds a locked object.
                Splitting does not help; the caller falls back.
        """
        if request_bytes <= 0:
            raise ValueError(f"request_bytes must be positive, got {request_bytes}")
        if request_bytes > self._window_bytes:
            raise PlanTooLargeError(
                f"request needs {request_bytes} bytes but an RDMA window holds "
                f"{self._window_bytes}"
            )
        with self._lock:
            if self._outstanding is not None:
                raise LayerwiseContractError(
                    "another pipelined fetch holds RDMA window "
                    f"{self._outstanding.window_index}; one fetch runs at a time"
                )
            now = self._clock()
            candidates = [
                i
                for i in range(len(self._offsets))
                if self._quarantined_until[i] <= now
            ]
            if not candidates:
                raise LayerwiseContractError("every RDMA window is quarantined")
            counts = {
                i: self._l1_manager.get_rdma_window_object_count(i) for i in candidates
            }
            candidates.sort(key=lambda i: (counts[i] > 0, self._released_at[i]))
            for window_index in candidates:
                err = self._l1_manager.reclaim_rdma_window(window_index)
                if err != L1Error.SUCCESS:
                    continue
                if counts[window_index] > 0:
                    logger.debug(
                        "Reclaimed RDMA window %d, evicting %d objects",
                        window_index,
                        counts[window_index],
                    )
                return self._grant(window_index)
            raise LayerwiseContractError(
                "every RDMA window is quarantined or holds an object in use"
            )

    def release(self, lease: WindowLease, outcome: FetchOutcome) -> None:
        """End a lease.

        The window's objects stay in L1 either way. After
        :attr:`FetchOutcome.ABANDONED` the caller is expected to have aborted
        the fetch's write reservations already; the window is not leased
        again until ``fetch_timeout_seconds`` has passed.

        Args:
            lease: The outstanding lease, as returned by :meth:`lease`.
            outcome: :attr:`FetchOutcome.FINISHED` only if every layer became
                resident; anything else is :attr:`FetchOutcome.ABANDONED`.

        Raises:
            ValueError: If ``lease`` is not the outstanding lease, e.g. it
                was already released.
        """
        with self._lock:
            if self._outstanding != lease:
                raise ValueError(f"{lease} is not the outstanding lease")
            self._outstanding = None
            now = self._clock()
            self._released_at[lease.window_index] = now
            if outcome is FetchOutcome.ABANDONED:
                self._quarantined_until[lease.window_index] = (
                    now + self._quarantine_seconds
                )

    def _grant(self, window_index: int) -> WindowLease:
        """Record and return a new lease of ``window_index``. Holds the lock."""
        lease = WindowLease(
            window_index=window_index,
            base_offset=self._offsets[window_index],
            size_bytes=self._window_bytes,
            lease_id=self._next_lease_id,
        )
        self._next_lease_id += 1
        self._outstanding = lease
        return lease
