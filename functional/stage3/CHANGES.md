# Stage 3: host changes

## gpu-stage3b (2026-10-01 21:45-23:45Z)

No host change. No packages, and no ufw, sshd, rdma_rxe or rxe0 changes.
`lmc-b`, `lmc-d`, `lmc-newstack`, `aero-kvsink-bp` and `/root/lmc-work/LMCache-1a`
were not touched. Every service listened on 127.0.0.1.

| Change | Where | Approved by | State left |
|---|---|---|---|
| Box tree `/root/lmc-work/LMCache`: fast-forwarded `a3504150` → `d10a376f` (functional only). A later `git pull` reached `b6b9d76f`, which carries origin's newstack product merge. The tree was reset (`git reset --hard d10a376f`) before anything ran, so the build and the code match. `stage3_gpt.sh` from `b6b9d76f` and the pipe06 wait (`4c7a3df3`) were applied as working-tree changes | host | Work brief (pull harness; no rebuild without product changes) | At `d10a376f` plus those 2 modified files. It is behind origin, which needs a rebuild for the newstack code |
| First-run result directories renamed: `e2e04` → `e2e04_r1`, `e2e05` → `e2e05_r1`, `pipe05` → `pipe05_r1_recompute`, `pipe06` → `pipe06_r1_recompute`; this run's first pipe06 → `pipe06_r2_fail_nowait` | `/root/lmc-work/functional/stage3/` | n/a (files under /root/lmc-work) | Kept |
| kv-sink restarted and warmed before every group (e2e04 ×6, e2e05 ×2, pipe05, pipe06 ×2, pipe12 ×2, pipe11, gpt_e2e08p, gpt_e2e08pl2, rdma06gpu), SIGSTOPped 3 × 2.5 s in each pipe06 | `aero-kvsink`, 127.0.0.1:3100-3103 | Work brief | Stopped |
| vLLM (8000) and LMCache (6555, HTTP 8080) sessions, Llama-3.1-8B and gpt-oss-120b | `lmc-c` | Work brief | Stopped; USED_VRAM 285 MB |
| Scripts `s3b_chain.sh`, `s3b_l2.sh`, `s3b_pipe06.sh`, `s3b_collect.sh`, `pipe06wait.patch` | `/root/lmc-work/functional/stage3/` | n/a | Kept |

## gpu-stage3 (2026-10-01 20:00-20:50Z)

No host change. No packages, and no ufw, sshd, rdma_rxe or rxe0 changes.
`lmc-d`, `aerospike-ce-d14`, `aero-n1..n3` and `/root/lmc-work/LMCache-cpu`
were not touched. Every service listened on 127.0.0.1.

| Change | Where | Approved by | State left |
|---|---|---|---|
| Box tree `/root/lmc-work/LMCache` fast-forwarded `d872268c` → `30734468` (harness only: `functional/stage3/`, checked with `git diff --stat`) | host | Work brief | At `30734468`; not pulled further (origin now has product change `e9cd0689`) |
| kv-sink server restarted and warmed before every group (cfg08, e2e04 ×6, e2e05 ×2, pipe05, pipe06, the S1 diagnostic), SIGSTOPped 3 × 2.5 s in pipe06 | `aero-kvsink`, 127.0.0.1:3100-3103 | Work brief | Stopped |
| vLLM (8000) and LMCache (6555, HTTP 8080) sessions, Llama-3.1-8B only | `lmc-c` | Work brief | Stopped; USED_VRAM 285 MB |
| Results, logs and scripts (`s3kill.sh`, `s3clean.sh`, `s1_diag.sh`) | `/root/lmc-work/functional/stage3/` | n/a (files under /root/lmc-work) | Kept |

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
