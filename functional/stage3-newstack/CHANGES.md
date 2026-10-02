# Stage 3 on the new kv-sink stack: changes

## Host

None. No ufw, sshd, kernel-module or package change on the host. The
`rdma_rxe` module and `rxe0` were already loaded and were not reloaded.

## Containers

| When (UTC) | Container | Change | Approved by |
| --- | --- | --- | --- |
| 2026-10-01 23:45 | `lmc-c` | `/work/LMCache` tree reset to origin: the two uncommitted harness files from `gpu-stage3b` discarded (`git checkout`; their content is on origin as `b6b9d76f`, `4c7a3df3`), then `git pull --ff-only` from `d10a376f` to `b6b0caae` | Control tower (worker brief) |
| 2026-10-01 23:46-23:47 | `lmc-c` | LMCache rebuilt (editable) against the kv-sink client `523d51ea` (`/work/deps/aerospike-kvsink-install-523d51ea`). After this `lmc-c` cannot do pipelined fetches from the old server on 3100. Back out: rebuild from a pre-merge tree with `/work/deps/aerospike-install/usr/{include,lib}` | Lyndon Bauto, Slack 2026-10-01 22:00Z ("integrate as soon as possible") |
| throughout | `aero-kvsink-bp` | kv-sink server `046e8558d` started and stopped by the harness (`kvsink_restart` per group), 127.0.0.1:3700-3703 | Standing rule (Aerospike containers bound to 127.0.0.1) |
| throughout | `aerospike-ce` | Set `lmcache.kv_chunks` truncated by `run_steps.sh reset` (plain-path check) | Standing rule |

## Harness (functional/ only, no product code)

| Commit | Change | Approved by |
| --- | --- | --- |
| `adf9aa5f` | "Harness: bind vLLM to loopback + public-listener check": `loopback_env.sh` (sourced by every vLLM launcher), `loopback_shim/sitecustomize.py` (torch TCPStore master on a 127.0.0.1 socket via `master_listen_fd`), `listen_check.sh` after every LMCache server and vLLM start | Lyndon Bauto, Slack 2026-10-01 23:00Z ("Fix it if it is not going to delay testing much") |
| `6cb99246` | `stage3-newstack/sanity.sh`; `run_steps.sh` `term` step and LMCache exit status; `stage3.sh` `STAGE_DIR`, `LW_S`, listener alert | Worker brief (harness changes allowed) |
| `1e364379`, `a4716acb` | `term grace=<s>`; D-15 probe waits 60 s for a clean shutdown and restarts the server before waiting on the interrupted send | Worker brief |
| `b2ea0f98` | `configs/aerospike-kvsink-bp.conf` namespace `lmcache` `data-size` 8G → 16G (the old server's size); `sanity.sh e2e04s16` reruns the e2e04 short groups | Worker brief (harness config) |
