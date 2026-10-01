# Stage 3: host changes

## cpu-prep-s3s5 (2026-10-01)

No host change. No packages, ufw, sshd, rdma_rxe or rxe0 changes, and
nothing on the GPU. Ports 8000/6555, `aerospike-ce` and
`/root/lmc-work/LMCache` were not touched. Every service listened on
127.0.0.1.

| Change | Where | Approved by | State left |
|---|---|---|---|
| kv-sink server (`asd`) started and stopped several times: once per T-RDMA-06 run, and once per dry run | `aero-kvsink`, 127.0.0.1:3100-3103; logs under `/root/lmc-work/functional/stage3/{logs/rdma06,dry}/` | Work brief | Stopped |
| CPU `lmcache server` instances for the dry runs, each stopped by PID | `lmc-c`, 127.0.0.1:6655 (ZMQ), 8180 (HTTP), 9190 (Prometheus) | Work brief | None running |
| Llama-3.3-70B-Instruct download (`functional/stage6/scripts/dl70b.sh`; token passed as an env var over stdin, never written) | `lmc-c`, `/work/hf` (= `/root/lmc-work/hf`), 132 GB | Work brief | Done; no process left |
| Results, logs and scripts | `/root/lmc-work/functional/{stage3,stage5,stage6}/`, `/root/lmc-work/LMCache-cpu` (worker clone) | n/a (files under /root/lmc-work) | Kept |

## cpu-ledger-kvsink (2026-10-01)

All within the standing permissions: new containers from `lmcache-rocm:day1`,
apt inside containers, and Aerospike bound to 127.0.0.1.

| Change | Approval |
|---|---|
| Container `aero-kvsink` from `lmcache-rocm:day1`: `--network host --device /dev/infiniband/uverbs0 --ulimit memlock=-1:-1 --cap-add IPC_LOCK --security-opt seccomp=unconfined -v /root/lmc-work:/root/lmc-work`, no GPU devices | Standing permission |
| apt inside `aero-kvsink`: server build deps (`apt_deps.sh`) | Standing permission |
| Container-local files in `aero-kvsink`: `/work/VERSION`, `/work/EVENT`, `/work/deps -> /root/lmc-work/deps` | Standing permission |
| New directories: `/root/lmc-work/aerospike-server-kvsink`, `/root/lmc-work/aerospike-kvsink-data`, `/root/lmc-work/LMCache-cpu`, `/root/lmc-work/functional/stage3` | n/a (files under /root/lmc-work) |
| kv-sink server started on 127.0.0.1:3100-3103 for the smoke, then stopped cleanly | Standing permission |
