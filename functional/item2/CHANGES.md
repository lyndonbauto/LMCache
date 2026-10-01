# Host changes: work item 2

| When (UTC) | Change | Approved by |
| --- | --- | --- |
| 2026-10-01 16:24 | Started container `aerospike-ce-t2` (Aerospike CE 8.2, host network, bound to 127.0.0.1:3200–3202) from `scripts/aerospike-t2.conf`; restarted at 16:40 to add namespace `lmcache_evict`. Removed at the end of the item (`scripts/aerospike_t2.sh stop`) | Standing rule: "start, stop and reconfigure Aerospike containers bound to 127.0.0.1" |
| 2026-10-01 16:29 | Started the kv-sink server in `aero-kvsink` (127.0.0.1:3100–3103) with `functional/harness/kvsink_server.sh start`, log in `logs/asd-kvsink.log`; stopped at the end of the item | Same rule; control tower brief |
| 2026-10-01 16:36 | Built the RDMA C++ test binaries in the clone (`tests/v1/distributed/rdma/build/`, git-ignored) | Not a host change; recorded for completeness |

No change to `aerospike-ce`, `lmc-b`, `rxe0`, `rdma_rxe`, ufw, sshd or
`/root/lmc-work/LMCache`. Test processes lowered their own memlock limit
and dropped their own `CAP_IPC_LOCK` (`prlimit`, `setpriv`); nothing else
changed in `lmc-c`.
