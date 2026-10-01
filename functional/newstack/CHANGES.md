# New kv-sink stack: host changes

All by worker `cpu-newstack`, under the control tower's brief for the new-stack
integration (Lyndon Bauto, Slack, 2026-10-01 22:00 UTC: "integrate the C
client changes now, retest"). Nothing of the old stack was touched: not
`lmc-c`, `aero-kvsink`, `aerospike-ce`, ports 3000/3100-3103/6555/8000, the
GPU, `rxe0`/`rdma_rxe`, or `/root/lmc-work/LMCache`.

| Time (UTC) | Change | Approval |
| --- | --- | --- |
| 2026-10-01 22:17 | Copied (rsync) server source `046e8558d` to `/root/lmc-work/aerospike-server-kvsink-bp` (no `.git`, no build outputs), the private C client `523d51ea` (with `.git`, for its public submodules) to `/root/lmc-work/aerospike-client-c-kvsink-bp`, and the merged LMCache tree to `/root/lmc-work/LMCache-1a`. The client is not pushed anywhere | Brief: rsync only |
| 2026-10-01 22:20 | Created containers `aero-kvsink-bp` and `lmc-newstack` from `lmcache-rocm:day1` (host network, `uverbs0`, memlock unlimited, `IPC_LOCK`, no GPU devices); `functional/newstack/scripts/create_containers.sh` | Rules: more containers from `lmcache-rocm:day1` |
| 2026-10-01 22:20 | `apt-get install libyaml-dev` in `lmc-newstack` (the C client build); `aero-kvsink-bp` already had the server's build dependencies | Rules: apt inside containers |
| 2026-10-01 22:20 | Built the server in `aero-kvsink-bp` (`scripts/build_server.sh`; container-local `/work/VERSION`, `/work/EVENT`), the client and LMCache in `lmc-newstack` (`scripts/build_client_lmcache.sh`) | Brief |
| 2026-10-01 22:22 | Started the new kv-sink server in `aero-kvsink-bp` on 127.0.0.1:3700-3703 (work dir `/root/lmc-work/aerospike-kvsink-bp-data/work`), after checking `docker ps` and `ss -ltnp` | Rules: Aerospike containers on 127.0.0.1 |
| 2026-10-01 22:28 | Copied the built client to `/root/lmc-work/deps/aerospike-kvsink-install-523d51ea` (new directory; `deps/aerospike-install` unchanged) so `lmc-c` can build against it without its own client build | Brief (switch instructions) |
