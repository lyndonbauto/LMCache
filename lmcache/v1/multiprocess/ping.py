# SPDX-License-Identifier: Apache-2.0
"""What a worker's PING to the MP server found.

A worker pings with its instance ID. The server answers ``False`` when it holds
no registration for that ID: it restarted (faster than the heartbeat interval,
so no ping ever failed) or reaped the worker. The worker must then re-register,
which a plain healthy/unhealthy flag cannot say, so heartbeats sort the answer
into a :class:`PingOutcome`.
"""

# Standard
import enum

# First Party
from lmcache.logging import init_logger
from lmcache.v1.multiprocess.transport.base import RequestClient

logger = init_logger(__name__)


class PingOutcome(enum.Enum):
    """What one PING found."""

    #: The server answered and holds this worker's registration, or the ping
    #: named no worker (an untracked prober such as the scheduler).
    REGISTERED = "registered"
    #: The server answered, but holds no registration for this worker.
    UNREGISTERED = "unregistered"
    #: No answer in time, or the request failed.
    UNREACHABLE = "unreachable"


def probe_server(
    req_client: RequestClient, timeout: float, instance_id: int | None
) -> PingOutcome:
    """Ping the server and sort its answer.

    Args:
        req_client: The request client.
        timeout: Seconds to wait for the answer.
        instance_id: The worker's instance ID, so the server refreshes its
            liveness and says whether it is registered; ``None`` for an
            untracked prober.

    Returns:
        :attr:`PingOutcome.UNREACHABLE` on a timeout or any error,
        :attr:`PingOutcome.UNREGISTERED` if the server answered ``False``,
        otherwise :attr:`PingOutcome.REGISTERED`.
    """
    try:
        answer = req_client.ping(instance_id).result(timeout=timeout)
    except TimeoutError:
        return PingOutcome.UNREACHABLE
    except Exception:
        logger.debug("Ping failed with exception", exc_info=True)
        return PingOutcome.UNREACHABLE
    return PingOutcome.REGISTERED if answer else PingOutcome.UNREGISTERED
