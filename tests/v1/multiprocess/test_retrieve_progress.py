# SPDX-License-Identifier: Apache-2.0
"""Tests for ConcurrentRetrieveProgress: several retrieves, one progress record."""

# Standard
import threading

# Third Party
import pytest

# First Party
from lmcache.v1.multiprocess.retrieve_progress import (
    ConcurrentRetrieveProgress,
    RetrieveState,
    RetrieveStoppedError,
)


class FakeRecord:
    """Records what the tracker publishes, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []

    def begin_retrieve(self, generation: int) -> None:
        self.events.append(("begin", generation))

    def report_launch_recorded(self, watermark: int) -> None:
        self.events.append(("watermark", watermark))

    def fail_retrieve(self, generation: int) -> None:
        self.events.append(("fail", generation))


class FakeDrain:
    """Counts drains and notes what was published before each one."""

    def __init__(self, record: FakeRecord) -> None:
        self.record = record
        self.events_at_drain: list[int] = []

    def __call__(self) -> None:
        self.events_at_drain.append(len(self.record.events))


def make(
    launch_count: int = 4,
) -> tuple[ConcurrentRetrieveProgress, FakeRecord, FakeDrain]:
    record = FakeRecord()
    drain = FakeDrain(record)
    progress = ConcurrentRetrieveProgress(record, launch_count, drain)  # type: ignore[arg-type]
    return progress, record, drain


def test_rejects_non_positive_launch_count() -> None:
    record = FakeRecord()
    with pytest.raises(ValueError):
        ConcurrentRetrieveProgress(record, 0, FakeDrain(record))  # type: ignore[arg-type]


def test_register_publishes_newer_generations_only() -> None:
    progress, record, _ = make()
    progress.register(3)
    with pytest.raises(ValueError):
        progress.register(3)
    with pytest.raises(ValueError):
        progress.register(2)
    progress.register(5)
    assert record.events == [("begin", 3), ("begin", 5)]
    assert progress.loading_count() == 2


def test_single_retrieve_publishes_its_own_watermark() -> None:
    progress, record, drain = make(launch_count=3)
    retrieve = progress.register(1)
    for watermark in (1, 2, 3):
        retrieve.report_launch_recorded(watermark)
    assert record.events == [
        ("begin", 1),
        ("watermark", 1),
        ("watermark", 2),
        ("watermark", 3),
    ]
    assert retrieve.state is RetrieveState.DONE
    assert progress.loading_count() == 0
    retrieve.close()
    assert drain.events_at_drain == []


def test_published_watermark_is_the_minimum_over_loading_retrieves() -> None:
    progress, record, _ = make(launch_count=3)
    older = progress.register(1)
    newer = progress.register(2)
    record.events.clear()

    newer.report_launch_recorded(2)
    assert record.events == [], "the older retrieve is still at 0"
    older.report_launch_recorded(1)
    assert record.events == [("watermark", 1)]
    older.report_launch_recorded(3)
    assert record.events == [("watermark", 1), ("watermark", 2)]
    newer.report_launch_recorded(3)
    assert record.events[-1] == ("watermark", 3)
    assert older.state is RetrieveState.DONE
    assert newer.state is RetrieveState.DONE


def test_watermark_may_not_go_backwards() -> None:
    progress, _, _ = make()
    retrieve = progress.register(1)
    retrieve.report_launch_recorded(2)
    with pytest.raises(ValueError):
        retrieve.report_launch_recorded(1)


def test_read_returns_the_retrieve_own_progress() -> None:
    progress, _, _ = make(launch_count=3)
    older = progress.register(1)
    newer = progress.register(2)
    newer.report_launch_recorded(2)
    snapshot = newer.read()
    assert snapshot.generation == 2
    assert snapshot.watermark == 2
    assert not snapshot.retrieve_failed
    assert older.read().watermark == 0


def test_one_failure_stops_every_loading_retrieve_after_a_drain() -> None:
    progress, record, drain = make()
    older = progress.register(1)
    newer = progress.register(2)
    record.events.clear()

    older.fail_retrieve(1)

    assert older.state is RetrieveState.STOPPED
    assert newer.state is RetrieveState.STOPPED
    assert progress.loading_count() == 0
    assert record.events == [("fail", 2)], "the published generation is flagged"
    assert drain.events_at_drain == [0], "the stream drains before the failure"
    assert newer.read().retrieve_failed


def test_stopped_retrieve_may_not_launch() -> None:
    progress, _, _ = make()
    older = progress.register(1)
    newer = progress.register(2)
    newer.fail_retrieve(2)
    with pytest.raises(RetrieveStoppedError):
        with older.launching():
            pass
    with pytest.raises(RetrieveStoppedError):
        older.begin_retrieve(1)


def test_reports_after_stop_are_ignored() -> None:
    progress, record, _ = make()
    retrieve = progress.register(1)
    retrieve.fail_retrieve(1)
    record.events.clear()
    retrieve.report_launch_recorded(4)
    assert record.events == []


def test_failure_is_published_once() -> None:
    progress, record, drain = make()
    retrieve = progress.register(1)
    retrieve.fail_retrieve(1)
    retrieve.fail_retrieve(1)
    retrieve.close()
    assert record.events.count(("fail", 1)) == 1
    assert len(drain.events_at_drain) == 1


def test_generation_mismatch_is_refused() -> None:
    progress, _, _ = make()
    retrieve = progress.register(1)
    with pytest.raises(ValueError):
        retrieve.fail_retrieve(2)
    with pytest.raises(ValueError):
        retrieve.begin_retrieve(2)
    retrieve.begin_retrieve(1)


def test_close_fails_an_unfinished_retrieve() -> None:
    progress, record, _ = make()
    retrieve = progress.register(1)
    retrieve.report_launch_recorded(2)
    retrieve.close()
    assert retrieve.state is RetrieveState.STOPPED
    assert record.events[-1] == ("fail", 1)


def test_finished_retrieve_of_an_older_step_is_left_alone() -> None:
    progress, record, drain = make(launch_count=1)
    older = progress.register(1)
    older.report_launch_recorded(1)
    progress.register(2).report_launch_recorded(1)
    record.events.clear()
    older.fail_retrieve(1)
    assert record.events == []
    assert drain.events_at_drain == []


def test_finished_retrieve_fails_its_step_while_others_load() -> None:
    progress, record, _ = make(launch_count=2)
    older = progress.register(1)
    newer = progress.register(2)
    older.report_launch_recorded(2)
    record.events.clear()
    older.fail_retrieve(1)
    assert newer.state is RetrieveState.STOPPED
    assert record.events == [("fail", 2)]


def test_fail_generation_registers_an_unseen_generation() -> None:
    progress, record, _ = make()
    progress.fail_generation(4)
    assert record.events == [("begin", 4), ("fail", 4)]
    with pytest.raises(ValueError):
        progress.fail_generation(0)


def test_fail_generation_stops_a_loading_retrieve() -> None:
    progress, record, _ = make()
    retrieve = progress.register(1)
    progress.fail_generation(1)
    assert retrieve.state is RetrieveState.STOPPED
    assert record.events[-1] == ("fail", 1)


def test_stop_all_stops_loading_retrieves() -> None:
    progress, record, _ = make()
    assert progress.stop_all() == 0
    assert record.events == []
    first = progress.register(1)
    second = progress.register(2)
    assert progress.stop_all() == 2
    assert first.state is RetrieveState.STOPPED
    assert second.state is RetrieveState.STOPPED
    assert record.events[-1] == ("fail", 2)


def test_launches_and_staging_exclude_each_other() -> None:
    progress, _, _ = make()
    retrieve = progress.register(1)
    inside = threading.Event()
    release = threading.Event()
    entered: list[str] = []

    def launch() -> None:
        with retrieve.launching():
            inside.set()
            release.wait(5)
            entered.append("launch")

    thread = threading.Thread(target=launch)
    thread.start()
    assert inside.wait(5)

    def store() -> None:
        with progress.staging():
            entered.append("store")

    store_thread = threading.Thread(target=store)
    store_thread.start()
    store_thread.join(0.2)
    assert store_thread.is_alive(), "the store waits for the launch"
    release.set()
    thread.join(5)
    store_thread.join(5)
    assert entered == ["launch", "store"]


def test_concurrent_reports_publish_a_monotonic_minimum() -> None:
    launch_count = 50
    progress, record, _ = make(launch_count=launch_count)
    retrieves = [progress.register(g) for g in range(1, 5)]

    def run(index: int) -> None:
        for watermark in range(1, launch_count + 1):
            with retrieves[index].launching():
                retrieves[index].report_launch_recorded(watermark)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    published = [value for kind, value in record.events if kind == "watermark"]
    assert published == sorted(published)
    assert published[-1] == launch_count
    assert progress.loading_count() == 0
