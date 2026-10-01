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
