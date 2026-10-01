# SPDX-License-Identifier: Apache-2.0
"""D-14 read cost: the pipelined path's extra batch read of meta records.

Stores 64 sharded 3 MiB objects through the adapter, then times, on the
caller thread:
  - read_write_ids(keys) for 1, 4, 16 and 64 keys: the one extra round trip
    a pipelined fetch now makes before planning;
  - one plain meta-record get (aerospike Python client), for scale;
  - one whole-object load through the adapter (meta + 4 segments), whose
    round trips the fix does not change.
Usage: read_cost.py <hosts> <set> [reps]
"""

# Standard
import select
import statistics
import sys
import time

# Third Party
import aerospike
import torch

# First Party
from lmcache.lmcache_aerospike import LMCacheAerospikeClient
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import (
    AerospikeL2AdapterConfig,
)
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd

hosts, set_name = sys.argv[1], sys.argv[2]
reps = int(sys.argv[3]) if len(sys.argv) > 3 else 200
NB = 3 * 1024 * 1024
adapter = create_l2_adapter_from_registry(
    AerospikeL2AdapterConfig(
        hosts=hosts, namespace="lmcache", set_name=set_name, num_workers=2
    )
)


def obj(t):
    return TensorMemoryObj(
        t,
        MemoryObjMetadata(
            shape=t.shape,
            dtype=t.dtype,
            address=0,
            phy_size=NB,
            fmt=MemoryFormat.KV_2LTD,
            ref_count=1,
        ),
        parent_allocator=None,
    )


def wait(fd):
    p = select.poll()
    p.register(fd, select.POLLIN)
    p.poll(30000)
    try:
        consume_fd(fd)
    except BlockingIOError:
        pass


keys = [ObjectKey(ObjectKey.IntHash2Bytes(i), "readcost", 0) for i in range(64)]
for k in keys:
    t = adapter.submit_store_task([k], [obj(torch.zeros(NB // 4))])
    wait(adapter.get_store_event_fd())
    assert adapter.pop_completed_store_tasks()[t].is_successful()
strs = [object_key_to_string(k) for k in keys]
native = LMCacheAerospikeClient(hosts, "lmcache", set_name, 1)


def us(fn):
    out = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1e6)
    out.sort()
    return (
        f"p50 {statistics.median(out):7.0f} us  p90 {out[int(0.9 * len(out))]:7.0f} us"
    )


print(f"hosts={hosts} reps={reps}")
for n in (1, 4, 16, 64):
    ids = native.read_write_ids(strs[:n])
    assert len(ids) == n and all(len(v) == 16 for v in ids.values()), ids
    batch = strs[:n]
    timing = us(lambda batch=batch: native.read_write_ids(batch))
    print(f"read_write_ids({n:2d} keys):   {timing}")
host, port = hosts.split(",")[0].split(":")
pc = aerospike.client({"hosts": [(host, int(port))]}).connect()
meta_key = ("lmcache", set_name, strs[0] + "|m")
print(f"one meta get (py client):  {us(lambda: pc.get(meta_key))}")
target = torch.zeros(NB // 4)


def load():
    t = adapter.submit_load_task([keys[0]], [obj(target)])
    wait(adapter.get_load_event_fd())
    adapter.query_load_result(t)


print(f"whole load 3 MiB (adapter): {us(load)}")
pc.truncate("lmcache", set_name, 0)
pc.close()
native.close()
adapter.close()
