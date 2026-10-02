# Stage 6 CPU half: host changes

Every change is inside what the work brief allows without asking ("start, stop
and reconfigure Aerospike containers bound to 127.0.0.1"). No packages were
installed, ufw/sshd/rdma_rxe/rxe0 were not touched, and nothing ran on the
GPU or on ports 8000/6555. `aerospike-ce` (3000-3003), `lmc-b` and
`/root/lmc-work/LMCache` were not touched.

| Change | Where | Approved by | State left |
|---|---|---|---|
| Three containers `aero-n1`, `aero-n2`, `aero-n3` from `aerospike/aerospike-server:latest` (the image `aerospike-ce` runs), host networking, `asd` as entrypoint, every port on 127.0.0.1: 3300-3302, 3310-3312, 3320-3322 (CE 8.2 ignores the info stanza, so 33x3 never opens) | `functional/harness/cluster.sh` | Work brief (Aerospike containers on 127.0.0.1) | Stopped, kept; `cluster.sh start [rf]` brings them back |
| Cluster data: one sparse 16 GiB device file per node, configs rendered per node | `/root/lmc-work/aero-cluster/{n1,n2,n3}/lmcache.dat`, `/root/lmc-work/aero-cluster/conf/` | Same | Kept (sparse; `cluster.sh wipe` removes) |
| Three kv-sink `asd` processes in the existing `aero-kvsink` container, ports 3400-3403, 3410-3413, 3420-3423 on 127.0.0.1, namespace memory 2 GiB each (pinned once warm) | `functional/harness/kvsink_cluster.sh`, work dirs and logs under `/root/lmc-work/aero-cluster/kvsink/` | Same | Stopped |
| Per-port builds of the server branch's `examples/kv-sink/kvsink_client` (warm-up) | `/root/lmc-work/aero-cluster/kvsink/kvsink_client_build/` | Same (test tooling) | Kept |
| Host-side control daemon so tests in `lmc-c` (no docker) can kill/restart nodes; a bash loop reading request files | `functional/harness/cluster_ctl_daemon.sh`, requests in `/root/lmc-work/aero-cluster/ctl/` | Same (test tooling) | Stopped (`touch /root/lmc-work/aero-cluster/ctl/stop`) |
| Test-only `kv_sink_fanout_probe` binary built in the box clone's git-ignored RDMA build dir | `/root/lmc-work/LMCache-cpu/tests/v1/distributed/rdma/build/` | Same (test tooling) | Kept |

## GPU half, part A (gpu-stage6a, 2026-10-02)

No host change. Only Aerospike containers on 127.0.0.1 were started and stopped, as
the work brief allows. No packages were installed; ufw, sshd, `rdma_rxe` and `rxe0`
were not touched; `lmc-b`, `lmc-d`, `lmc-newstack` and `aero-kvsink` were not touched.

| Change | Where | Approved by | State left |
|---|---|---|---|
| `aero-n1..3` (CE cluster) wiped and started per session at RF 1 or 2; nodes SIGKILLed / restarted by `flt02/03/04` | `functional/harness/cluster.sh` | Work brief (Aerospike containers on 127.0.0.1) | Stopped |
| kv-sink `asd` in `aero-kvsink-bp` (127.0.0.1:3700-3703) restarted before each kv-sink session and once inside `flt04k` | `functional/harness/kvsink_server.sh` via `kvsink_bp_env.sh` | Same | asd stopped |
| Second LMCache server (127.0.0.1:6556, HTTP 8081, Prometheus 9091) and second vLLM (127.0.0.1:8001) in `lmc-c`; every listener check passed | `functional/harness/run_steps.sh` | Work brief (services on 127.0.0.1) | Stopped |

## T-E2E-11 (gpu-e2e11, 2026-10-02)

No host change. No packages were installed; ufw, sshd, `rdma_rxe` and `rxe0` were not
touched; `lmc-b`, `lmc-d`, `lmc-newstack` and `aero-kvsink` were not touched.

| Change | Where | Approved by | State left |
|---|---|---|---|
| kv-sink `asd` in `aero-kvsink-bp` restarted with a 64G namespace for the 70B session (new file `configs/aerospike-kvsink-bp-70b.conf`; the 16G config is unchanged) | `kvsink_server.sh` via `stage6gpu.sh e2e11` | Work brief (Aerospike containers on 127.0.0.1; namespace resize allowed in the item) | Restarted on the 16G config, then asd stopped |
| `aerospike-ce` set `kv_chunks` truncated twice (`reset` step of `e2e11p`) | `run_steps.sh reset` | Work brief | Holds the 70B plain-path records |
| `aerospike-ce` `max-write-cache` raised 2G → 8 GiB with a dynamic `set-config` during `e2e11p` run 2 (D-26) | `stage6gpu.sh ce_write_cache` | Work brief (reconfigure Aerospike containers on 127.0.0.1) | Restored to 2147483648; config file unchanged |
| LMCache server (6555/8080) and vLLM (8000) in `lmc-c`, every listener check passed | `run_steps.sh` | Work brief | Stopped, USED_VRAM 285 MB |
