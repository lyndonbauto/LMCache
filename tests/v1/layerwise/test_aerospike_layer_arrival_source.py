# SPDX-License-Identifier: Apache-2.0
"""Tests for :class:`AerospikeLayerArrivalSource` over a fake native client.

The native session's own accounting -- per-slot arrival, stale immediates,
declined commands, receive-queue sizing -- is covered by the C++ logic harness
(``make -C tests/v1/distributed/rdma logic-test``). These tests cover what the
adapter adds on top: the three-valued status, generation and layer checks,
error translation, and the abandon rules, all through the public contract.

``begin_fetch`` goes through an injected issuer because issuing a
``LayerFetchPlan`` natively is still open; see "Open for the meeting" in
``docs/design/v1/layerwise/track-a-questions-for-track-c.md``.
"""

# Standard
from collections import deque
import threading

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.l2_adapters.layerwise_source import (
    AerospikeLayerArrivalSource,
    NativePlanIssuer,
)
from lmcache.v1.layerwise import (
    LayerArrivalPump,
    LayerArrivalStatus,
    LayerFetchPlan,
    LayerNotInPlanError,
    LayerUnservableError,
    LayerwiseContractError,
    RecordingLayerLoadSink,
    StaleGenerationError,
)
from lmcache.v1.layerwise.contract import NO_GENERATION

# Local
from .conftest import make_plan


class FakeNativeConnector:
    """Models the native client's pipelined surface without a fabric.

    Mirrors the one native property the adapter relies on: an arrival counts
    only when its generation is the active one, so a late write from an
    abandoned fetch never moves its successor's accounting.
    """

    def __init__(self) -> None:
        self.ready = True
        self.init_error = ""
        self.active_generation = NO_GENERATION
        self.landed: set[int] = set()
        self.unservable: set[int] = set()
        self.arrivals_per_poll: deque[int] = deque()
        self.finish_calls = 0
        self.abandon_calls = 0
        self.poll_error: Exception = RuntimeError("unset")
        self.raise_on_poll = False
        self.raise_on_finish = False

    def begin(self, generation: int) -> None:
        """Start a native request, as issue_pipelined_fetch would."""
        self.active_generation = generation
        self.landed = set()
        self.unservable = set()

    def land(self, layer_id: int, generation: int) -> None:
        """Deliver every slot of a layer tagged with ``generation``."""
        if generation == self.active_generation:
            self.landed.add(layer_id)

    def decline(self, layer_id: int) -> None:
        """Mark a layer's slot declined by a node."""
        self.unservable.add(layer_id)

    def pipelined_fetch_ready(self) -> bool:
        return self.ready

    def pipelined_fetch_init_error(self) -> str:
        return self.init_error

    def is_pipelined_layer_ready(self, layer_id: int, request_generation: int) -> bool:
        if self.raise_on_poll:
            raise self.poll_error
        if self.arrivals_per_poll:
            self.land(self.arrivals_per_poll.popleft(), self.active_generation)
        if request_generation != self.active_generation:
            return False
        return layer_id in self.landed and layer_id not in self.unservable

    def pipelined_unservable_layers(self) -> list[int]:
        return sorted(self.unservable)

    def finish_pipelined_fetch(self) -> None:
        self.finish_calls += 1
        self.active_generation = NO_GENERATION
        if self.raise_on_finish:
            raise RuntimeError("PipelinedFetchSession: no active request")

    def abandon_pipelined_fetch(self) -> None:
        self.abandon_calls += 1
        self.active_generation = NO_GENERATION


class FakeIssuer:
    """Issues a plan by starting a request on the fake connector."""

    def __init__(self, connector: FakeNativeConnector) -> None:
        self.connector = connector
        self.next_generation = 1
        self.error: Exception = RuntimeError("unset")
        self.raise_on_issue = False
        self.issued: list[LayerFetchPlan] = []

    def issue(self, plan: LayerFetchPlan) -> int:
        if self.raise_on_issue:
            raise self.error
        self.issued.append(plan)
        generation = self.next_generation
        self.next_generation += 1
        self.connector.begin(generation)
        return generation


def _make_source() -> tuple[
    AerospikeLayerArrivalSource, FakeNativeConnector, FakeIssuer
]:
    connector = FakeNativeConnector()
    issuer = FakeIssuer(connector)
    return AerospikeLayerArrivalSource(connector, issuer), connector, issuer


# ---------------------------------------------------------------- begin_fetch


def test_begin_fetch_returns_the_native_generation() -> None:
    """The generation on the wire is the one callers quote back."""
    source, _, issuer = _make_source()
    issuer.next_generation = 42
    assert source.begin_fetch(make_plan({0: 1})) == 42


def test_begin_fetch_rejects_a_second_active_fetch() -> None:
    """Only one fetch may be active, and the native side is not asked twice."""
    source, _, issuer = _make_source()
    plan = make_plan({0: 1})
    source.begin_fetch(plan)
    with pytest.raises(LayerwiseContractError, match="still active"):
        source.begin_fetch(plan)
    assert len(issuer.issued) == 1


def test_begin_fetch_reports_an_unavailable_backend_as_a_contract_error() -> None:
    """An unready backend raises, naming why, instead of looking merely slow."""
    source, connector, issuer = _make_source()
    connector.ready = False
    connector.init_error = "kv-sink-register failed on every node"
    with pytest.raises(LayerwiseContractError, match="kv-sink-register failed"):
        source.begin_fetch(make_plan({0: 1}))
    assert issuer.issued == []


@pytest.mark.parametrize(
    "native_error",
    [
        RuntimeError(
            "PipelinedFetchSession: plan requires 9000 notification slots but "
            "at most 4096 can be posted on this device's queue pair"
        ),
        ValueError("PipelinedFetchSession: chunk 3 has no node binding"),
        IndexError("layer 99 is not in the layout"),
    ],
)
def test_native_issue_failures_become_contract_errors(
    native_error: Exception,
) -> None:
    """Callers catch LayerwiseContractError to fall back; natives must not leak.

    The first case is A6: a plan too large for the device's receive queue is
    rejected at begin_fetch rather than deadlocking at runtime.
    """
    source, _, issuer = _make_source()
    issuer.raise_on_issue = True
    issuer.error = native_error
    with pytest.raises(LayerwiseContractError) as caught:
        source.begin_fetch(make_plan({0: 1}))
    assert caught.value.__cause__ is native_error


def test_a_failed_begin_fetch_leaves_the_source_idle() -> None:
    """After a rejected plan the caller can retry with a smaller one."""
    source, _, issuer = _make_source()
    issuer.raise_on_issue = True
    with pytest.raises(LayerwiseContractError):
        source.begin_fetch(make_plan({0: 1}))
    issuer.raise_on_issue = False
    assert source.begin_fetch(make_plan({0: 1})) != NO_GENERATION


def test_a_reserved_native_generation_is_refused_and_released() -> None:
    """Generation 0 would alias "no fetch"; it is abandoned, not tracked."""
    source, connector, issuer = _make_source()
    issuer.next_generation = NO_GENERATION
    with pytest.raises(LayerwiseContractError, match="reserved generation"):
        source.begin_fetch(make_plan({0: 1}))
    assert connector.abandon_calls == 1


def test_the_native_issuer_is_not_disguised_as_a_backend_failure() -> None:
    """The unfinished plan translation surfaces as NotImplementedError.

    Wrapping it as LayerwiseContractError would send every caller down the
    fallback path and hide that the adapter is incomplete.
    """
    connector = FakeNativeConnector()
    source = AerospikeLayerArrivalSource(connector, NativePlanIssuer(connector))
    with pytest.raises(NotImplementedError, match="per-record node binding"):
        source.begin_fetch(make_plan({0: 1}))


# ---------------------------------------------------------------- poll_layer


def test_a_layer_is_pending_until_it_lands_then_resident() -> None:
    """A2: nothing is resident early, and a landed layer is reported at once."""
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 2, 1: 1}))
    assert source.poll_layer(0, generation) is LayerArrivalStatus.PENDING
    connector.land(0, generation)
    assert source.poll_layer(0, generation) is LayerArrivalStatus.RESIDENT
    assert source.poll_layer(1, generation) is LayerArrivalStatus.PENDING


def test_a_declined_layer_is_unservable_and_others_are_unaffected() -> None:
    """A4: a decline is terminal for its layer and never reads as pending."""
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1, 1: 1}))
    connector.decline(0)
    assert source.poll_layer(0, generation) is LayerArrivalStatus.UNSERVABLE
    assert source.poll_layer(1, generation) is LayerArrivalStatus.PENDING


def test_poll_rejects_a_generation_that_is_not_active() -> None:
    """A caller quoting the wrong fetch is told so, not given its status."""
    source, _, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, generation + 1)
    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, NO_GENERATION)


def test_poll_rejects_a_layer_outside_the_plan() -> None:
    source, _, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    with pytest.raises(LayerNotInPlanError):
        source.poll_layer(7, generation)


def test_poll_with_no_active_fetch_is_stale() -> None:
    source, _, _ = _make_source()
    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, 1)


def test_a_native_poll_failure_becomes_a_contract_error() -> None:
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    connector.raise_on_poll = True
    connector.poll_error = RuntimeError("notification completion failed")
    with pytest.raises(LayerwiseContractError, match="notification completion"):
        source.poll_layer(0, generation)


def test_a_late_write_from_an_abandoned_fetch_is_not_credited() -> None:
    """A3: abandon mid-fetch, start again, then the old write lands."""
    source, connector, _ = _make_source()
    plan = make_plan({0: 1})
    old_generation = source.begin_fetch(plan)
    source.abandon_fetch(old_generation)
    new_generation = source.begin_fetch(plan)

    connector.land(0, old_generation)

    assert source.poll_layer(0, new_generation) is LayerArrivalStatus.PENDING
    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, old_generation)


def test_concurrent_polls_are_safe() -> None:
    """The pump may poll from a different thread than the one that began."""
    source, connector, _ = _make_source()
    layer_ids = list(range(8))
    generation = source.begin_fetch(make_plan(dict.fromkeys(layer_ids, 1)))
    for layer_id in layer_ids:
        connector.land(layer_id, generation)
    errors: list[BaseException] = []

    def poll_all() -> None:
        try:
            for _ in range(200):
                for layer_id in layer_ids:
                    status = source.poll_layer(layer_id, generation)
                    assert status is LayerArrivalStatus.RESIDENT
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=poll_all) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


# ---------------------------------------------------------- finish / abandon


def test_finish_releases_the_native_fetch_and_the_generation() -> None:
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    source.finish_fetch(generation)
    assert connector.finish_calls == 1
    with pytest.raises(StaleGenerationError):
        source.poll_layer(0, generation)


def test_finish_rejects_a_stale_generation_without_touching_native() -> None:
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    with pytest.raises(StaleGenerationError):
        source.finish_fetch(generation + 1)
    assert connector.finish_calls == 0


def test_a_failed_native_finish_still_frees_the_source() -> None:
    """Otherwise the next begin_fetch would be refused forever."""
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    connector.raise_on_finish = True
    with pytest.raises(LayerwiseContractError):
        source.finish_fetch(generation)
    connector.raise_on_finish = False
    assert source.begin_fetch(make_plan({0: 1})) != NO_GENERATION


def test_abandon_tolerates_repeated_and_unknown_generations() -> None:
    """Error paths unwind without first working out how far a fetch got.

    The native abandon throws when nothing is active, so only the first call
    may reach it.
    """
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    source.abandon_fetch(generation)
    source.abandon_fetch(generation)
    source.abandon_fetch(generation + 5)
    source.abandon_fetch(NO_GENERATION)
    assert connector.abandon_calls == 1


def test_abandon_after_finish_is_a_no_op() -> None:
    source, connector, _ = _make_source()
    generation = source.begin_fetch(make_plan({0: 1}))
    source.finish_fetch(generation)
    source.abandon_fetch(generation)
    assert connector.abandon_calls == 0


def test_abandoning_a_stale_generation_leaves_the_active_fetch_alone() -> None:
    source, connector, _ = _make_source()
    plan = make_plan({0: 1})
    old_generation = source.begin_fetch(plan)
    source.finish_fetch(old_generation)
    new_generation = source.begin_fetch(plan)
    source.abandon_fetch(old_generation)
    assert connector.abandon_calls == 0
    assert source.poll_layer(0, new_generation) is LayerArrivalStatus.PENDING


# ---------------------------------------------------------------- with pump


def _no_sleep(_: float) -> None:
    return None


def test_the_pump_loads_in_order_when_layers_land_backwards() -> None:
    """Driven by the real pump into the recording sink (A9: no GPU)."""
    source, connector, _ = _make_source()
    plan = make_plan({0: 1, 1: 2, 2: 1})
    connector.arrivals_per_poll.extend([2, 1, 0])
    sink = RecordingLayerLoadSink()

    generation = LayerArrivalPump(source, sink, sleep=_no_sleep).run(plan)

    assert sink.loaded_layers() == (0, 1, 2)
    assert sink.finished_generations() == (generation,)
    assert connector.finish_calls == 1


def test_the_pump_falls_back_when_a_layer_is_declined() -> None:
    """UNSERVABLE reaches the pump, which abandons both sides."""

    class DecliningIssuer(FakeIssuer):
        def issue(self, plan: LayerFetchPlan) -> int:
            generation = super().issue(plan)
            self.connector.decline(1)
            return generation

    connector = FakeNativeConnector()
    connector.arrivals_per_poll.extend([0])
    source = AerospikeLayerArrivalSource(connector, DecliningIssuer(connector))
    plan = make_plan({0: 1, 1: 1})
    sink = RecordingLayerLoadSink()

    with pytest.raises(LayerUnservableError):
        LayerArrivalPump(source, sink, sleep=_no_sleep).run(plan)

    assert sink.loaded_layers() == (0,)
    assert len(sink.abandoned_generations()) == 1
    assert connector.abandon_calls == 1
