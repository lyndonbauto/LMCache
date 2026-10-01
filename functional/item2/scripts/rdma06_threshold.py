# SPDX-License-Identifier: Apache-2.0
"""Find the request size at which a pipelined Llama-size fetch stops working.

Usage (in lmc-c, cwd = the clone, env as run_rdma06.sh): rdma06_threshold.py N...
For each N: a fresh set and storage manager, N stored 32 MiB objects, one
pipelined retrieve; prints the completion and the slot count.
"""

# Standard
import sys
import uuid

# Third Party
import aerospike

# First Party
from tests.v1.distributed import test_aerospike_rdma_byte_oracle_integration as t
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.distributed.l2_adapters.rdma_registration import L1RdmaConfig
from lmcache.v1.layerwise.pipelined_retrieve import run_pipelined_retrieve

prompts = t._prompts()
pool = [k for p in prompts for k in p.keys]
for n in [int(a) for a in sys.argv[1:]]:
    set_name = f"thr_{uuid.uuid4().hex[:10]}"
    keys = pool[-n:]
    plain = create_l2_adapter_from_registry(t._adapter_config(set_name, L1RdmaConfig()))
    plain.set_object_group_layouts(dict(t.GROUP_LAYOUTS), t.KERNEL_LAYERS)
    for i, k in enumerate(keys):
        t._store(plain, k, t._payload(i))
    manager = t._build_manager(set_name, n)
    try:
        loader = t._Loader()
        tap = t._PlanTap(manager.layer_arrival_source())
        result = run_pipelined_retrieve(
            t._fetch_model(),
            [keys],
            manager.pipelined_max_record_bytes(),
            manager.pipelined_window_placer(t.GROUP_LAYOUTS, t._fetch_model(), n),
            tap,
            loader,
            layer_timeout_seconds=t.FETCH_TIMEOUT,
            poll_interval_seconds=0.001,
        )
        slots = len(tap.plan.slots) if tap.plan is not None else -1
        print(f"RESULT chunks={n} slots={slots} completion={result.completion.value}",
              flush=True)
    finally:
        manager.close()
        plain.close()
        c = aerospike.client({"hosts": [(t.AEROSPIKE_HOST, t.AEROSPIKE_PORT)]}).connect()
        c.truncate(t.AEROSPIKE_NAMESPACE, set_name, 0)
        c.close()
