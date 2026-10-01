# SPDX-License-Identifier: Apache-2.0
"""A worker re-registers when the MP server no longer holds its registration.

An LMCache server that restarts faster than the heartbeat interval fails no
ping, so before this the worker never noticed: it stayed healthy, never
re-registered, and every lookup missed. The server now answers a worker's
PING with ``False`` when it holds no registration for it (see
``ManagementModule.ping``), and the heartbeat re-registers on that answer.

Drives one real ``HeartbeatThread`` cycle at a time (``_execute``), with the
request client scripted.
"""

# Standard
from unittest.mock import MagicMock
import threading

# Third Party
import pytest

# First Party
from lmcache.integration.vllm import vllm_multi_process_adapter as adapter_mod
from lmcache.integration.vllm.vllm_multi_process_adapter import HeartbeatThread
from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.ping import PingOutcome, probe_server


def _answering(*answers: object) -> MagicMock:
    """A request client whose PINGs resolve to ``answers`` in turn.

    An exception among them fails that ping, as an unreachable server does.
    """
    queue = list(answers)

    def ping(_instance_id: int | None) -> MessagingFuture[object]:
        future: MessagingFuture[object] = MessagingFuture()
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            future.set_exception(answer)
        else:
            future.set_result(answer)
        return future

    client = MagicMock()
    client.ping.side_effect = ping
    return client


def _heartbeat(
    client: MagicMock, healthy: bool = True
) -> tuple[HeartbeatThread, threading.Event]:
    event = threading.Event()
    if healthy:
        event.set()
    return HeartbeatThread(client, event, interval=1.0, instance_id=7), event


def test_an_unregistered_answer_reregisters_while_degraded() -> None:
    """The callback runs with requests gated, then health is restored."""
    heartbeat, event = _heartbeat(_answering(False))
    health_during_recovery: list[bool] = []

    def recover() -> bool:
        health_during_recovery.append(event.is_set())
        return True

    heartbeat.register_recover_callback(recover)

    heartbeat._execute()

    assert health_during_recovery == [False]
    assert event.is_set()


def test_a_failed_reregistration_retries_on_the_next_answered_ping() -> None:
    heartbeat, event = _heartbeat(_answering(False, False))
    recover = MagicMock(side_effect=[False, True])
    heartbeat.register_recover_callback(recover)

    heartbeat._execute()
    assert not event.is_set()

    heartbeat._execute()
    assert event.is_set()
    assert recover.call_count == 2


def test_a_registered_answer_does_not_reregister_a_healthy_worker() -> None:
    heartbeat, event = _heartbeat(_answering(True, True))
    recover = MagicMock(return_value=True)
    heartbeat.register_recover_callback(recover)

    heartbeat._execute()
    heartbeat._execute()

    recover.assert_not_called()
    assert event.is_set()


def test_an_unreachable_server_degrades_without_reregistering() -> None:
    heartbeat, event = _heartbeat(_answering(ConnectionError("down")))
    recover = MagicMock(return_value=True)
    heartbeat.register_recover_callback(recover)

    heartbeat._execute()

    recover.assert_not_called()
    assert not event.is_set()


def test_a_restart_seen_only_as_an_outage_still_reregisters_once() -> None:
    """A restart longer than the interval fails a ping first; the next
    answered ping, registered or not, runs the callback exactly once."""
    heartbeat, event = _heartbeat(_answering(ConnectionError("down"), False, True))
    recover = MagicMock(return_value=True)
    heartbeat.register_recover_callback(recover)

    heartbeat._execute()
    heartbeat._execute()
    heartbeat._execute()

    assert recover.call_count == 1
    assert event.is_set()


def test_a_worker_that_cannot_reregister_stays_up_and_warns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no recover callback there is nothing to retry; flapping between
    degraded and healthy every cycle would only add noise. The module logger
    does not propagate, so the test spies on it instead of ``caplog``."""
    warnings: list[str] = []
    monkeypatch.setattr(
        adapter_mod.logger,
        "warning",
        lambda msg, *args, **kwargs: warnings.append(str(msg)),
    )
    heartbeat, event = _heartbeat(_answering(False, False, False))

    for _ in range(3):
        heartbeat._execute()

    assert event.is_set()
    assert len([w for w in warnings if "cannot re-register" in w]) == 1


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (True, PingOutcome.REGISTERED),
        (False, PingOutcome.UNREGISTERED),
        (ConnectionError("down"), PingOutcome.UNREACHABLE),
        (TimeoutError("late"), PingOutcome.UNREACHABLE),
    ],
)
def test_probe_server_sorts_the_answer(answer: object, outcome: PingOutcome) -> None:
    assert probe_server(_answering(answer), 1.0, instance_id=7) is outcome
