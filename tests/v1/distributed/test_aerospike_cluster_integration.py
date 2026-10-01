# SPDX-License-Identifier: Apache-2.0
"""The Aerospike adapter against a real 3-node cluster (functional plan E4).

Storage-level halves of the cluster tests, run with no GPU and no vLLM:

- T-STO-08: replication factor 2 with commit level all -- a store returns
  only once the replica holds the records, and every key reads back
  byte-exact with one node down.
- T-FLT-02 / T-FLT-03 / T-FLT-04: a node killed while a reader is loading,
  at replication factor 1 (affected keys miss cleanly, the adapter stays
  usable) and 2 (reads fail over to the replica), then the node restarted.
- T-FLT-10: two writer processes store the same key at the same moment,
  1,000 times; every later read must equal one of the two writes.
- T-EVT-06: two LMCache "hosts" (storage managers in two processes) share
  the cluster with client-side L2 eviction off; neither deletes the other's
  entries.

The cluster is ``functional/harness/cluster.sh`` (nodes on 3300/3310/3320).
These tests kill and restart its nodes through
``functional/harness/cluster_ctl_daemon.sh``, which must run on the host and
watch ``AEROSPIKE_CLUSTER_CTL``::

    RUN_AEROSPIKE_CLUSTER_INTEGRATION=1 \\
    AEROSPIKE_CLUSTER_HOSTS=127.0.0.1:3300,127.0.0.1:3310,127.0.0.1:3320 \\
    AEROSPIKE_CLUSTER_CTL=/work/aero-cluster/ctl \\
    pytest tests/v1/distributed/test_aerospike_cluster_integration.py

Requires the BUILD_AEROSPIKE=1 extension and the ``aerospike`` Python
client; skipped otherwise.
"""

# Standard
from collections.abc import Iterator
from pathlib import Path
import os
import select
import subprocess
import sys
import threading
import time
import uuid

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.l2_adapters.base import L2AdapterInterface
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd

RUN_CLUSTER_IT = os.environ.get("RUN_AEROSPIKE_CLUSTER_INTEGRATION") == "1"
CLUSTER_HOSTS = os.environ.get(
    "AEROSPIKE_CLUSTER_HOSTS", "127.0.0.1:3300,127.0.0.1:3310,127.0.0.1:3320"
)
CTL_DIR = Path(os.environ.get("AEROSPIKE_CLUSTER_CTL", "/work/aero-cluster/ctl"))
NAMESPACE = os.environ.get("AEROSPIKE_CLUSTER_NAMESPACE", "lmcache")
#: Rounds for T-FLT-10 (the plan asks for 1,000).
RACE_ROUNDS = int(os.environ.get("AEROSPIKE_CLUSTER_RACE_ROUNDS", "1000"))

MODEL = "cluster-it"
#: Past the 1 MiB record cap: a meta record plus four segments.
SHARDED_BYTES = 3 * 1024 * 1024
#: Under the record cap: one meta record holding the payload inline.
INLINE_BYTES = 64 * 1024
TIMEOUT = 30.0
CTL_TIMEOUT = 300.0
#: Past the namespace's flush-max-ms (1000), so stored records are on disk.
FLUSH_WAIT_S = 2.5
_EMPTY_LAYOUT = MemoryLayoutDesc(shapes=[], dtypes=[])

#: One writer for T-FLT-10. Reads a round number per stdin line, stores the
#: shared key with words ``round << 21 | writer << 20 | i`` and answers
#: ``ok <round>`` or ``fail <round>``, so the parent can release both
#: writers together and tell their bytes apart word by word.
_RACE_WRITER = r"""
import select, sys, torch
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.memory_management import (
    MemoryFormat, MemoryObjMetadata, TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd

hosts, namespace, set_name, model, num_bytes, writer, key_index = sys.argv[1:8]
num_bytes, writer, key_index = int(num_bytes), int(writer), int(key_index)
adapter = create_l2_adapter_from_registry(
    AerospikeL2AdapterConfig(hosts=hosts, namespace=namespace, set_name=set_name,
                             num_workers=1)
)
poller = select.poll()
poller.register(adapter.get_store_event_fd(), select.POLLIN)
base = torch.arange(num_bytes // 4, dtype=torch.int32) + (writer << 20)
key = ObjectKey(ObjectKey.IntHash2Bytes(key_index), model, 0)
print("ready", flush=True)
for line in sys.stdin:
    rnd = int(line)
    values = (base + (rnd << 21)).view(torch.float32)
    obj = TensorMemoryObj(values, MemoryObjMetadata(
        shape=values.shape, dtype=values.dtype, address=0, phy_size=num_bytes,
        fmt=MemoryFormat.KV_2LTD, ref_count=1), parent_allocator=None)
    task = adapter.submit_store_task([key], [obj])
    while task not in (done := adapter.pop_completed_store_tasks()):
        poller.poll(1000)
        try:
            consume_fd(adapter.get_store_event_fd())
        except BlockingIOError:
            pass
    print(("ok " if done[task].is_successful() else "fail ") + str(rnd), flush=True)
adapter.close()
"""

#: One LMCache "host" for T-EVT-06: a storage manager over the cluster that
#: stores every key in argv through reserve_write/finish_write, waits until
#: they are all in L2, then keeps running for ``linger`` seconds so its L2
#: eviction controller (if any) gets several cycles. Payload words are
#: ``index << 16 | i``: the same for every host storing that key.
_HOST = r"""
import select, sys, time, torch
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.config import (
    EvictionConfig, L1ManagerConfig, L1MemoryManagerConfig, StorageManagerConfig,
)
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.distributed.storage_manager import StorageManager
from lmcache.v1.platform import consume_fd

hosts, namespace, set_name, model, num_bytes, capacity_gb, linger, keys = sys.argv[1:9]
num_bytes, capacity_gb, linger = int(num_bytes), float(capacity_gb), float(linger)
indices = [int(k) for k in keys.split(",")]
adapter_config = AerospikeL2AdapterConfig(
    hosts=hosts, namespace=namespace, set_name=set_name, num_workers=2,
    max_capacity_gb=capacity_gb,
)
if capacity_gb > 0:
    adapter_config.eviction_config = EvictionConfig(
        eviction_policy="LRU", trigger_watermark=0.5, eviction_ratio=0.5
    )
manager = StorageManager(StorageManagerConfig(
    l1_manager_config=L1ManagerConfig(
        memory_config=L1MemoryManagerConfig(
            size_in_bytes=256 * 1024 * 1024, use_lazy=False, align_bytes=4096,
            shm_name="",
        ),
        write_ttl_seconds=600,
    ),
    eviction_config=EvictionConfig(eviction_policy="LRU"),
    l2_adapter_config=L2AdaptersConfig([adapter_config]),
))
layout = MemoryLayoutDesc(shapes=[torch.Size([num_bytes // 4])], dtypes=[torch.float32])
key_of = {i: ObjectKey(ObjectKey.IntHash2Bytes(i), model, 0) for i in indices}
for i in indices:
    reserved = manager.reserve_write([key_of[i]], layout, "new")
    words = torch.arange(num_bytes // 4, dtype=torch.int32) + (i << 16)
    reserved[key_of[i]].tensor.view(torch.int32).copy_(words)
    manager.finish_write([key_of[i]])

probe = create_l2_adapter_from_registry(AerospikeL2AdapterConfig(
    hosts=hosts, namespace=namespace, set_name=set_name, num_workers=1))
# A host that evicts may delete a key before the probe sees it, so only a
# host without L2 eviction waits for every key.
deadline = time.monotonic() + (120 if capacity_gb == 0 else 10)
pending = list(indices)
while pending and time.monotonic() < deadline:
    task = probe.submit_lookup_and_lock_task(
        [key_of[i] for i in pending], {0: MemoryLayoutDesc(shapes=[], dtypes=[])}
    )
    poller = select.poll()
    poller.register(probe.get_lookup_and_lock_event_fd(), select.POLLIN)
    while (found := probe.query_lookup_and_lock_result(task)) is None:
        poller.poll(1000)
        try:
            consume_fd(probe.get_lookup_and_lock_event_fd())
        except BlockingIOError:
            pass
    present = [i for n, i in enumerate(pending) if found.test(n)]
    if present:
        probe.submit_unlock([key_of[i] for i in present])
    pending = [i for i in pending if i not in present]
    if pending:
        time.sleep(0.2)
print(f"stored {len(indices) - len(pending)} of {len(indices)}", flush=True)
time.sleep(linger)
for _, l2 in manager.l2_adapters():
    print(f"usage {l2.get_usage()}", flush=True)
probe.close()
manager.close()
print("done", flush=True)
"""


def _cluster_available() -> bool:
    if not RUN_CLUSTER_IT:
        return False
    try:
        # Third Party
        import aerospike

        client = aerospike.client({"hosts": _host_tuples()}).connect()
        info = client.info_random_node(f"namespace/{NAMESPACE}")
        client.close()
        return "nsup-period" in info
    except Exception:
        return False


def _native_extension_available() -> bool:
    try:
        # First Party
        from lmcache.lmcache_aerospike import LMCacheAerospikeClient  # noqa: F401

        return True
    except ImportError:
        return False


def _host_tuples() -> list[tuple[str, int]]:
    out = []
    for part in CLUSTER_HOSTS.split(","):
        host, port = part.rsplit(":", 1)
        out.append((host, int(port)))
    return out


pytestmark = [
    pytest.mark.skipif(
        not _cluster_available(),
        reason=(
            f"Aerospike cluster not available at {CLUSTER_HOSTS} "
            "(set RUN_AEROSPIKE_CLUSTER_INTEGRATION=1)"
        ),
    ),
    pytest.mark.skipif(
        not _native_extension_available(),
        reason="lmcache.lmcache_aerospike extension not built",
    ),
]


def _ctl(script: str, command: str, arg: str = "") -> str:
    """Run ``<script>.sh <command> [arg]`` on the host through the control
    daemon and return its output.

    Raises:
        AssertionError: if the daemon does not answer in ``CTL_TIMEOUT`` or
            the command exits non-zero.
    """
    request_id = uuid.uuid4().hex
    request = CTL_DIR / f"{request_id}.req"
    tmp = CTL_DIR / f"{request_id}.tmp"
    tmp.write_text(f"{script} {command} {arg}\n")
    tmp.rename(request)
    rc_path = CTL_DIR / f"{request_id}.rc"
    deadline = time.monotonic() + CTL_TIMEOUT
    while not rc_path.exists():
        assert time.monotonic() < deadline, f"no answer to {script} {command} {arg}"
        time.sleep(0.2)
    output = (CTL_DIR / f"{request_id}.out").read_text()
    rc = int(rc_path.read_text())
    rc_path.unlink()
    (CTL_DIR / f"{request_id}.out").unlink()
    assert rc == 0, f"{script} {command} {arg} exited {rc}: {output}"
    print(f"[ctl] {script} {command} {arg}: {output.strip()}")
    return output


def _wait_fd(fd: int, timeout: float = TIMEOUT) -> None:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(timeout * 1000), "timed out waiting for eventfd"
    try:
        consume_fd(fd)
    except BlockingIOError:
        pass


def _payload(index: int, num_bytes: int) -> torch.Tensor:
    """Words that differ within and across objects, so misplacement shows."""
    words = torch.arange(num_bytes // 4, dtype=torch.int32) + index * (1 << 20)
    return words.view(torch.float32)


def _tensor_obj(values: torch.Tensor) -> TensorMemoryObj:
    metadata = MemoryObjMetadata(
        shape=values.shape,
        dtype=values.dtype,
        address=0,
        phy_size=values.numel() * values.element_size(),
        fmt=MemoryFormat.KV_2LTD,
        ref_count=1,
    )
    return TensorMemoryObj(values, metadata, parent_allocator=None)


def _key(index: int) -> ObjectKey:
    return ObjectKey(ObjectKey.IntHash2Bytes(index), MODEL, 0)


def _adapter(set_name: str) -> L2AdapterInterface:
    # First Party
    from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
        AerospikeL2AdapterConfig,
    )

    return create_l2_adapter_from_registry(
        AerospikeL2AdapterConfig(
            hosts=CLUSTER_HOSTS, namespace=NAMESPACE, set_name=set_name, num_workers=2
        )
    )


def _store(adapter: L2AdapterInterface, key: ObjectKey, values: torch.Tensor) -> bool:
    task = adapter.submit_store_task([key], [_tensor_obj(values)])
    _wait_fd(adapter.get_store_event_fd())
    return adapter.pop_completed_store_tasks()[task].is_successful()


def _exists(adapter: L2AdapterInterface, key: ObjectKey) -> bool:
    task = adapter.submit_lookup_and_lock_task([key], {0: _EMPTY_LAYOUT})
    _wait_fd(adapter.get_lookup_and_lock_event_fd())
    found = adapter.query_lookup_and_lock_result(task)
    assert found is not None
    present = found.test(0)
    if present:
        adapter.submit_unlock([key])
    return present


def _load(
    adapter: L2AdapterInterface, key: ObjectKey, num_bytes: int
) -> tuple[bool, torch.Tensor]:
    """Load ``key`` into a zeroed buffer: ``(loaded, buffer)``."""
    target = torch.zeros(num_bytes // 4, dtype=torch.float32)
    task = adapter.submit_load_task([key], [_tensor_obj(target)])
    _wait_fd(adapter.get_load_event_fd())
    bitmap = adapter.query_load_result(task)
    return bitmap is not None and bitmap.test(0), target


def _namespace_counts(inspector: object) -> dict[str, tuple[int, int]]:
    """Per live node: ``(master_objects, prole_objects)`` in the namespace."""
    replies = inspector.info_all(f"namespace/{NAMESPACE}")  # type: ignore[attr-defined]
    out = {}
    for node, (err, reply) in replies.items():
        assert err is None, f"info from {node}: {err}"
        fields = dict(
            f.split("=", 1)
            for f in reply.split("\t")[-1].strip().split(";")
            if "=" in f
        )
        out[node] = (int(fields["master_objects"]), int(fields["prole_objects"]))
    return out


def _totals(inspector: object) -> tuple[int, int]:
    counts = _namespace_counts(inspector).values()
    return sum(m for m, _ in counts), sum(p for _, p in counts)


def _size_of(index: int) -> int:
    """Even indices are sharded objects, odd ones inline."""
    return SHARDED_BYTES if index % 2 == 0 else INLINE_BYTES


def _read_all(
    adapter: L2AdapterInterface, indices: list[int]
) -> tuple[list[int], list[int]]:
    """Load every index: ``(byte_exact, missed)``.

    Raises:
        AssertionError: if any load reports success with bytes that differ
            from what was stored.
    """
    exact, missed = [], []
    for i in indices:
        loaded, buffer = _load(adapter, _key(i), _size_of(i))
        if not loaded:
            missed.append(i)
            continue
        assert torch.equal(buffer, _payload(i, _size_of(i))), (
            f"key {i} loaded with wrong bytes"
        )
        exact.append(i)
    return exact, missed


class _Reader(threading.Thread):
    """Loads ``indices`` round-robin until stopped, recording each outcome."""

    def __init__(self, adapter: L2AdapterInterface, indices: list[int]) -> None:
        super().__init__(daemon=True)
        self._adapter = adapter
        self._indices = indices
        self.stop_event = threading.Event()
        #: (monotonic time, index, "exact" | "miss" | "wrong").
        self.outcomes: list[tuple[float, int, str]] = []
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                for i in self._indices:
                    if self.stop_event.is_set():
                        break
                    loaded, buffer = _load(self._adapter, _key(i), _size_of(i))
                    if not loaded:
                        outcome = "miss"
                    elif torch.equal(buffer, _payload(i, _size_of(i))):
                        outcome = "exact"
                    else:
                        outcome = "wrong"
                    self.outcomes.append((time.monotonic(), i, outcome))
        except BaseException as error:  # reported by the test
            self.error = error


@pytest.fixture
def inspector() -> Iterator[object]:
    """A plain Aerospike client over the cluster, for info and truncation."""
    # Third Party
    import aerospike

    client = aerospike.client({"hosts": _host_tuples()}).connect()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def set_name(inspector: object) -> Iterator[str]:
    name = f"cluster_{uuid.uuid4().hex[:12]}"
    yield name
    inspector.truncate(NAMESPACE, name, 0)  # type: ignore[attr-defined]


def _cluster_up(rf: int) -> None:
    """All three nodes running with replication factor ``rf``, settled."""
    _ctl("cluster", "start", str(rf))


def _store_objects(adapter: L2AdapterInterface, indices: list[int]) -> None:
    for i in indices:
        assert _store(adapter, _key(i), _payload(i, _size_of(i))), f"store {i}"


# T-STO-08: replication factor 2, commit level all.


def test_rf2_store_returns_only_after_the_replica_holds_the_records(
    inspector: object, set_name: str
) -> None:
    """After every store returns, the cluster's replica (prole) object count
    has grown exactly as much as its master count: commit level all makes
    the master wait for the replica before answering."""
    _cluster_up(2)
    adapter = _adapter(set_name)
    try:
        master0, prole0 = _totals(inspector)
        assert master0 == prole0, "cluster not settled before the test"
        last = 0
        for i in range(40):
            assert _store(adapter, _key(i), _payload(i, _size_of(i)))
            master, prole = _totals(inspector)
            added_master, added_prole = master - master0, prole - prole0
            assert added_master > last, f"store {i} added no master record"
            assert added_prole == added_master, (
                f"after store {i} returned: {added_master} master records "
                f"but {added_prole} replicas"
            )
            last = added_master
        print(f"[T-STO-08] 40 stores, {last} records, each with its replica")
    finally:
        adapter.close()


def test_rf2_every_key_reads_back_byte_exact_with_one_node_down(
    set_name: str,
) -> None:
    """Kill one node; every key loads byte-exact from the survivors. Restart
    it, wait for migrations; every key still loads byte-exact."""
    _cluster_up(2)
    indices = list(range(60))
    adapter = _adapter(set_name)
    try:
        _store_objects(adapter, indices)
        _ctl("cluster", "kill-node", "2")
        try:
            exact, missed = _read_all(adapter, indices)
            assert missed == [], f"missed with one node down at RF 2: {missed}"
        finally:
            _ctl("cluster", "restart-node", "2")
        exact, missed = _read_all(adapter, indices)
        assert missed == [], f"missed after the node rejoined: {missed}"
        print(f"[T-STO-08] {len(exact)} keys byte-exact with n2 down and after")
    finally:
        adapter.close()


# T-FLT-02 / T-FLT-03 / T-FLT-04, storage halves.


def _kill_mid_fetch(
    adapter: L2AdapterInterface, indices: list[int], node: str
) -> _Reader:
    """Start a reader, kill ``node`` while it reads, let it read on for two
    seconds after the cluster settles, stop it. Returns the reader."""
    reader = _Reader(adapter, indices)
    reader.start()
    time.sleep(1.0)
    killed_at = time.monotonic()
    _ctl("cluster", "kill-node", node)
    settled_at = time.monotonic()
    time.sleep(2.0)
    reader.stop_event.set()
    reader.join(TIMEOUT * 3)
    assert not reader.is_alive(), "a load hung after the node was killed"
    assert reader.error is None, f"reader raised: {reader.error!r}"
    during = [o for t, _, o in reader.outcomes if killed_at <= t < settled_at]
    print(
        f"[kill n{node}] {len(reader.outcomes)} loads; between the kill and "
        f"the settled 2-node cluster ({settled_at - killed_at:.1f} s): "
        f"{during.count('exact')} exact, {during.count('miss')} miss"
    )
    return reader


def test_rf1_node_killed_mid_fetch_misses_cleanly_and_adapter_stays_usable(
    set_name: str,
) -> None:
    """RF 1: loads racing a node kill end as byte-exact or a miss, never
    wrong bytes or a hang. Once settled, the dead node's keys miss and the
    rest load; new stores and loads work. After the node restarts (its data
    file survives), every key is byte-exact again (T-FLT-04, CE half)."""
    _cluster_up(1)
    indices = list(range(60))
    adapter = _adapter(set_name)
    try:
        _store_objects(adapter, indices)
        # CE buffers device writes in a write block flushed every
        # flush-max-ms (1 s) without commit-to-device, so SIGKILL loses a
        # node's last second of writes; at RF 1 nothing else holds them.
        time.sleep(FLUSH_WAIT_S)
        try:
            reader = _kill_mid_fetch(adapter, indices, "3")
            assert all(o != "wrong" for _, _, o in reader.outcomes)
            exact, missed = _read_all(adapter, indices)
            print(f"[T-FLT-02] n3 down: {len(exact)} exact, {len(missed)} miss")
            assert exact, "no key readable from the two surviving nodes"
            assert missed, "no key missed although a third of partitions died"
            fresh = list(range(1000, 1010))
            _store_objects(adapter, fresh)
            exact_fresh, missed_fresh = _read_all(adapter, fresh)
            assert missed_fresh == [], "new stores unreadable with a node down"
        finally:
            _ctl("cluster", "restart-node", "3")
        exact, missed = _read_all(adapter, indices + fresh)
        print(f"[T-FLT-04] n3 back: {len(exact)} exact, {len(missed)} miss")
        assert missed == [], f"missed after the node rejoined: {missed}"
    finally:
        adapter.close()


def test_rf2_node_killed_mid_fetch_fails_over_to_the_replica(
    set_name: str,
) -> None:
    """RF 2: loads racing a node kill end byte-exact or as a miss, never
    wrong bytes; once the 2-node cluster settles every key loads byte-exact
    (no recompute needed). The node then restarts and rejoins."""
    _cluster_up(2)
    indices = list(range(60))
    adapter = _adapter(set_name)
    try:
        _store_objects(adapter, indices)
        try:
            reader = _kill_mid_fetch(adapter, indices, "1")
            assert all(o != "wrong" for _, _, o in reader.outcomes)
            exact, missed = _read_all(adapter, indices)
            assert missed == [], f"missed at RF 2 after settling: {missed}"
        finally:
            _ctl("cluster", "restart-node", "1")
        exact, missed = _read_all(adapter, indices)
        assert missed == [], f"missed after the node rejoined: {missed}"
    finally:
        adapter.close()


# T-FLT-10: two engines store the same chunk at the same time.


def _start_writer(
    set_name: str, num_bytes: int, writer: int, key_index: int
) -> "subprocess.Popen[str]":
    """Start one ``_RACE_WRITER`` process and wait until it is ready."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _RACE_WRITER,
            CLUSTER_HOSTS,
            NAMESPACE,
            set_name,
            MODEL,
            str(num_bytes),
            str(writer),
            str(key_index),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _race(
    set_name: str, num_bytes: int, rounds: int, key_index: int
) -> tuple[dict[str, int], list[str]]:
    """Release two writer processes on the same key ``rounds`` times and
    classify what a third adapter reads after each round.

    Returns:
        ``(tally, examples)``: rounds per outcome (``writer1``, ``writer2``,
        ``mixed``, ``miss``, ``store_failed``), and a description of up to
        five mixed reads.
    """
    writers = [_start_writer(set_name, num_bytes, w, key_index) for w in (1, 2)]
    reader = _adapter(set_name)
    key = _key(key_index)
    base = torch.arange(num_bytes // 4, dtype=torch.int32)
    tally = {"writer1": 0, "writer2": 0, "mixed": 0, "miss": 0, "store_failed": 0}
    first_mixed: list[str] = []
    try:
        for rnd in range(rounds):
            for proc in writers:
                proc.stdin.write(f"{rnd}\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
            replies = [proc.stdout.readline().split() for proc in writers]  # type: ignore[union-attr]
            if any(r[0] != "ok" for r in replies):
                tally["store_failed"] += 1
            loaded, buffer = _load(reader, key, num_bytes)
            if not loaded:
                tally["miss"] += 1
                continue
            words = buffer.view(torch.int32)
            from1 = words == base + ((1 << 20) + (rnd << 21))
            from2 = words == base + ((2 << 20) + (rnd << 21))
            if bool(from1.all()):
                tally["writer1"] += 1
            elif bool(from2.all()):
                tally["writer2"] += 1
            else:
                tally["mixed"] += 1
                if len(first_mixed) < 5:
                    neither = int((~(from1 | from2)).sum())
                    first_mixed.append(
                        f"round {rnd}: {int(from1.sum())} words from writer 1, "
                        f"{int(from2.sum())} from writer 2, {neither} from neither"
                    )
    finally:
        reader.close()
        for proc in writers:
            proc.stdin.close()  # type: ignore[union-attr]
            proc.wait(TIMEOUT)
    return tally, first_mixed


def test_two_writers_racing_on_one_inline_chunk_never_mix(set_name: str) -> None:
    """One-record objects: every read after a race equals one writer's
    bytes. The record write is atomic, so this is the control."""
    _cluster_up(2)
    tally, examples = _race(set_name, INLINE_BYTES, RACE_ROUNDS, key_index=7)
    print(f"[T-FLT-10 inline, {RACE_ROUNDS} rounds] {tally} {examples}")
    assert tally["mixed"] == 0 and tally["miss"] == 0 and tally["store_failed"] == 0


def test_two_writers_racing_on_one_sharded_chunk_never_mix(set_name: str) -> None:
    """Sharded objects (four segments and a meta record): every read after
    a race equals one writer's bytes, never a mix (plan T-FLT-10)."""
    _cluster_up(2)
    tally, examples = _race(set_name, SHARDED_BYTES, RACE_ROUNDS, key_index=8)
    print(f"[T-FLT-10 sharded, {RACE_ROUNDS} rounds] {tally} {examples}")
    assert tally["miss"] == 0 and tally["store_failed"] == 0
    assert tally["mixed"] == 0, f"mixed reads: {tally} {examples}"


# T-EVT-06: two hosts share the cluster with client-side eviction off.

#: 512 KiB: one inline record per key, so "present" is one record.
HOST_BYTES = 512 * 1024
SHARED = list(range(0, 24))
ONLY_A = list(range(100, 124))
ONLY_B = list(range(200, 224))


def _host_payload(index: int) -> torch.Tensor:
    words = torch.arange(HOST_BYTES // 4, dtype=torch.int32) + (index << 16)
    return words.view(torch.float32)


def _run_hosts(set_name: str, capacity_a_gb: float) -> list[str]:
    """Run host A (``SHARED + ONLY_A``) and host B (``SHARED + ONLY_B``) at
    the same time; host A's L2 eviction is on when ``capacity_a_gb > 0``."""
    procs = []
    for capacity, keys in ((capacity_a_gb, SHARED + ONLY_A), (0.0, SHARED + ONLY_B)):
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _HOST,
                    CLUSTER_HOSTS,
                    NAMESPACE,
                    set_name,
                    MODEL,
                    str(HOST_BYTES),
                    str(capacity),
                    "6",
                    ",".join(str(k) for k in keys),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )
    outputs = []
    for name, proc in zip("AB", procs, strict=True):
        out, _ = proc.communicate(timeout=300)
        tail = [
            line
            for line in out.splitlines()
            if line.startswith(("stored", "usage", "done"))
        ]
        print(f"[host {name}] exit {proc.returncode}: {tail}")
        assert proc.returncode == 0, out[-3000:]
        outputs.append(out)
    return outputs


def _present(adapter: L2AdapterInterface, indices: list[int]) -> list[int]:
    out = []
    for i in indices:
        loaded, buffer = _load(adapter, _key(i), HOST_BYTES)
        if loaded:
            assert torch.equal(buffer, _host_payload(i)), f"key {i} wrong bytes"
            out.append(i)
    return out


def test_two_hosts_without_client_eviction_keep_each_others_entries(
    set_name: str,
) -> None:
    """Both hosts' storage managers run with no L2 capacity (client LRU
    off): afterwards every key either stored is present and byte-exact."""
    _cluster_up(2)
    outputs = _run_hosts(set_name, capacity_a_gb=0.0)
    per_host = len(SHARED) + len(ONLY_A)
    for out in outputs:
        assert f"stored {per_host} of {per_host}" in out
    adapter = _adapter(set_name)
    try:
        everything = SHARED + ONLY_A + ONLY_B
        present = _present(adapter, everything)
        missing = sorted(set(everything) - set(present))
        assert present == everything, f"missing: {missing}"
    finally:
        adapter.close()


def test_a_host_with_client_eviction_never_deletes_keys_it_did_not_store(
    set_name: str,
) -> None:
    """Contrast: host A's L2 LRU is on with an 8 MiB capacity, under the
    24 MiB it stores. A deletes only keys it stored itself: every key only
    B stored survives. Shared keys A evicted are gone for B too (B's own
    probe may then never see them); the test reports how many."""
    _cluster_up(2)
    _run_hosts(set_name, capacity_a_gb=8 / 1024)
    adapter = _adapter(set_name)
    try:
        only_b = _present(adapter, ONLY_B)
        deleted = sorted(set(ONLY_B) - set(only_b))
        assert only_b == ONLY_B, f"B's own keys deleted: {deleted}"
        shared = _present(adapter, SHARED)
        only_a = _present(adapter, ONLY_A)
        print(
            f"[T-EVT-06 contrast] after A's eviction: shared {len(shared)}/"
            f"{len(SHARED)}, A-only {len(only_a)}/{len(ONLY_A)}, "
            f"B-only {len(only_b)}/{len(ONLY_B)}"
        )
        assert len(only_a) < len(ONLY_A), "A's eviction deleted nothing"
    finally:
        adapter.close()
