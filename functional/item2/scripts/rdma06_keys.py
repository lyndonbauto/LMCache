# SPDX-License-Identifier: Apache-2.0
"""Derive P-exact object keys for Llama-3.1-8B and look a few up read-only.

Usage: rdma06_keys.py CORPUS HOST PORT NAMESPACE SET MODEL_NAME
Only ``exists``/``get`` on meta records; nothing is written.
"""

# Standard
import json
import sys

# Third Party
import aerospike

# First Party
from lmcache.v1.distributed.api import ipc_key_to_object_keys
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    object_key_to_string,
)
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.token_hasher import TokenHasher

corpus, host, port, ns, set_name, model_name = sys.argv[1:7]
d = json.load(open(corpus))
hasher = TokenHasher(chunk_size=256)
client = aerospike.client({"hosts": [(host, int(port))]}).connect()
found = 0
total = 0
for prompt in d["sets"]["P-exact"]:
    toks = prompt["token_ids"]
    end = (len(toks) // 256) * 256
    key = IPCCacheServerKey(
        model_name=model_name,
        world_size=1,
        worker_id=0,
        token_ids=tuple(toks),
        start=0,
        end=end,
        request_id="x",
        cache_salt="",
    )
    hashes = [
        TokenHasher.hash_to_bytes(h) for h in hasher.compute_chunk_hashes(toks, end=end)
    ]
    objs = ipc_key_to_object_keys(key, hashes, [0])[0]
    for o in objs[:2]:
        s = object_key_to_string(o)
        _, meta = client.exists((ns, set_name, s + "|m"))
        total += 1
        if meta is not None:
            found += 1
            if found == 1:
                _, _, bins = client.select((ns, set_name, s + "|m"), ["nseg", "seg_b", "tot_b", "plane_b"])
                print("example", s, bins)
print(f"found {found}/{total}")
client.close()
