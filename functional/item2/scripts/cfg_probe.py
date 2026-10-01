# T-CFG-06 / T-CFG-07 probe: build a StorageManager whose Aerospike adapter
# registers RDMA windows with the kv-sink server, and report whether startup
# fails and with what message.
# Usage: cfg_probe.py <gid_index> <l1_bytes> <window_count> <window_bytes>
import logging, os, resource, sys, time, traceback
sys.path.insert(0, "/work/LMCache-cpu")
from lmcache.v1.distributed.config import (EvictionConfig, L1ManagerConfig,
    L1MemoryManagerConfig, StorageManagerConfig)
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import AerospikeL2AdapterConfig
from lmcache.v1.distributed.l2_adapters.config import L2AdaptersConfig
from lmcache.v1.distributed.l2_adapters.rdma_registration import (L1RdmaConfig,
    RdmaTransport, RdmaWindowPlan)
from lmcache.v1.distributed.storage_manager import StorageManager

gid, l1_bytes, count, wbytes = (int(a) for a in sys.argv[1:5])
print("RLIMIT_MEMLOCK", resource.getrlimit(resource.RLIMIT_MEMLOCK), flush=True)
print(f"write_ttl={os.environ.get('WRITE_TTL', '600')} fetch_timeout=5.0 gid_index={gid} l1_bytes={l1_bytes} windows={count}x{wbytes}", flush=True)
rdma = L1RdmaConfig(transport=RdmaTransport.RC, device_name="rxe0", gid_index=gid,
    window_plan=RdmaWindowPlan(window_count=count, window_bytes=wbytes),
    fetch_timeout_seconds=5.0)
t0 = time.monotonic()
try:
    mgr = StorageManager(StorageManagerConfig(
        l1_manager_config=L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(size_in_bytes=l1_bytes, use_lazy=False,
                align_bytes=4096, shm_name="", rdma_window_count=count,
                rdma_window_bytes=wbytes),
            write_ttl_seconds=int(os.environ.get('WRITE_TTL', '600'))),
        eviction_config=EvictionConfig(eviction_policy="LRU"),
        l2_adapter_config=L2AdaptersConfig([AerospikeL2AdapterConfig(
            hosts="127.0.0.1:3100", namespace="lmcache", set_name="cfg_probe",
            num_workers=1, rdma=rdma)])))
except BaseException as e:
    print(f"STARTUP RAISED after {time.monotonic()-t0:.2f}s: {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
    sys.exit(3)
print(f"STARTUP OK after {time.monotonic()-t0:.2f}s", flush=True)
from tests.v1.layerwise.vllm_requests import GROUP_LAYOUTS, KERNEL_LAYERS
mgr.set_object_group_layouts(dict(GROUP_LAYOUTS), KERNEL_LAYERS)
print("layouts set", flush=True)
deadline = time.monotonic() + 15
while True:
    try:
        print(f"pipelined node after {time.monotonic()-t0:.2f}s:", mgr.pipelined_fetch_node_name(), flush=True)
        break
    except BaseException as e:
        if time.monotonic() > deadline:
            print(f"pipelined_fetch_node_name raised after {time.monotonic()-t0:.2f}s: {type(e).__name__}: {e}", flush=True)
            break
        time.sleep(0.2)
mgr.close()
print("closed", flush=True)
