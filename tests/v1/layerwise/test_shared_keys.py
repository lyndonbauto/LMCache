# SPDX-License-Identifier: Apache-2.0
"""Tests for :func:`resolve_shared_keys`: reuse, fetch, or give up on keys
another request is still fetching."""

# Standard
from collections.abc import Sequence

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ObjectKey, ResidentKeys
from lmcache.v1.layerwise.deferral import SharedKeyPolicy
from lmcache.v1.layerwise.pipelined_retrieve import (
    SHARED_KEY_POLL_SECONDS,
    SharedKeysBusyError,
    resolve_shared_keys,
)
from lmcache.v1.memory_management import MemoryObj


def _key(i: int) -> ObjectKey:
    return ObjectKey(chunk_hash=ObjectKey.IntHash2Bytes(i), model_name="m", kv_rank=0)


class _Memory:
    """Stands in for the memory of a locked key; never read."""


MEMORY: MemoryObj = _Memory()  # type: ignore[assignment]


class FakeL1:
    """Keys are readable, absent, or busy for a number of checks.

    A busy key becomes ``settles_to`` (readable or absent) after the given
    number of checks. Records every lock it holds for the caller.
    """

    def __init__(
        self,
        readable: Sequence[ObjectKey] = (),
        busy_for: dict[ObjectKey, int] | None = None,
        settles_to: str = "absent",
    ) -> None:
        self.readable = set(readable)
        self.busy_for = dict(busy_for or {})
        self.settles_to = settles_to
        self.locked: list[ObjectKey] = []
        self.released: list[ObjectKey] = []
        self.checks = 0

    def lock_resident_keys(self, keys: list[ObjectKey]) -> ResidentKeys:
        self.checks += 1
        locked: dict[ObjectKey, MemoryObj] = {}
        busy: list[ObjectKey] = []
        absent: list[ObjectKey] = []
        for key in keys:
            if self.busy_for.get(key, 0) > 0:
                self.busy_for[key] -= 1
                busy.append(key)
                if self.busy_for[key] == 0 and self.settles_to == "readable":
                    self.readable.add(key)
            elif key in self.readable:
                locked[key] = MEMORY
                self.locked.append(key)
            else:
                absent.append(key)
        return ResidentKeys(locked=locked, busy=tuple(busy), absent=tuple(absent))

    def finish_read_prefetched(
        self, keys: list[ObjectKey], read_locks: int = 1
    ) -> None:
        self.released.extend(keys)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _resolve(
    l1: FakeL1,
    keys: Sequence[ObjectKey],
    policy: SharedKeyPolicy = SharedKeyPolicy.WAIT,
    wait_seconds: float = 1.0,
    clock: FakeClock | None = None,
):
    clock = clock if clock is not None else FakeClock()
    return resolve_shared_keys(l1, keys, policy, wait_seconds, clock, clock.sleep)


@pytest.mark.parametrize("policy", list(SharedKeyPolicy))
def test_readable_keys_are_reused_and_absent_ones_fetched(
    policy: SharedKeyPolicy,
) -> None:
    l1 = FakeL1(readable=[_key(1)])

    resolution = _resolve(l1, [_key(0), _key(1), _key(2)], policy)

    assert resolution.reused == {_key(1): MEMORY}
    assert resolution.to_fetch == (_key(0), _key(2))
    assert l1.released == []


def test_recompute_gives_up_on_a_busy_key_and_releases_its_locks() -> None:
    l1 = FakeL1(readable=[_key(0)], busy_for={_key(1): 5})

    with pytest.raises(SharedKeysBusyError) as caught:
        _resolve(l1, [_key(0), _key(1)], SharedKeyPolicy.RECOMPUTE)

    assert caught.value.busy_keys == (_key(1),)
    assert l1.released == l1.locked == [_key(0)]
    assert l1.checks == 1


@pytest.mark.parametrize(
    ("settles_to", "reused", "to_fetch"),
    [("absent", {}, (_key(0),)), ("readable", {_key(0): MEMORY}, ())],
)
def test_wait_follows_a_busy_key_until_it_settles(
    settles_to: str, reused: dict[ObjectKey, MemoryObj], to_fetch: tuple
) -> None:
    """A key whose other fetch was freed is fetched; one it left is reused."""
    l1 = FakeL1(busy_for={_key(0): 3}, settles_to=settles_to)
    clock = FakeClock()

    resolution = _resolve(l1, [_key(0)], clock=clock)

    assert resolution.reused == reused
    assert resolution.to_fetch == to_fetch
    assert clock.sleeps == [SHARED_KEY_POLL_SECONDS] * 3


def test_wait_only_rechecks_the_busy_keys() -> None:
    """A key locked on the first check is not locked a second time."""
    l1 = FakeL1(readable=[_key(0)], busy_for={_key(1): 2})

    resolution = _resolve(l1, [_key(0), _key(1)])

    assert l1.locked == [_key(0)]
    assert resolution.reused == {_key(0): MEMORY}
    assert resolution.to_fetch == (_key(1),)


def test_wait_gives_up_at_its_budget_and_releases_its_locks() -> None:
    l1 = FakeL1(readable=[_key(0)], busy_for={_key(1): 10_000})
    clock = FakeClock()

    with pytest.raises(SharedKeysBusyError):
        _resolve(l1, [_key(0), _key(1)], wait_seconds=0.05, clock=clock)

    assert l1.released == [_key(0)]
    assert 0.05 <= clock.now < 0.05 + 2 * SHARED_KEY_POLL_SECONDS
