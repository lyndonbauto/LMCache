# Host changes: D-14 fix

| When (UTC) | Change | Approved by |
| --- | --- | --- |
| 2026-10-01 19:45 | Created tree `/root/lmc-work/LMCache-d14` (rsync of the local worktree, no `.git`): `LMCache-cpu` held another worker's uncommitted edits, left alone | Brief: own clone |
| 2026-10-01 19:47 | Created container `lmc-d` (`lmcache-rocm:day1`, host network, no GPU devices, `/dev/infiniband/uverbs0`, `/root/lmc-work:/work`, the tree at `/work/LMCache`). Stopped (not removed) at 20:42 | Brief: isolated container |
| 2026-10-01 19:57 | Started `aerospike-ce-d14` (CE 8.2, 127.0.0.1:3200–3202) with `scripts/aerospike_d14.sh`; removed at 20:42 | Standing rule: Aerospike containers bound to 127.0.0.1 |
| 2026-10-01 19:57 | Started the cluster control daemon (`scripts/start_ctl.sh`, from the D-14 tree); stopped at 20:42 | Same |
| 2026-10-01 20:06 | Started the 3-node cluster `aero-n1..n3` at RF 2 (`cluster.sh start 2`); the tests restarted it at RF 1 and 2 and killed/restarted n2/n3. Stopped at 20:42; data files under `/root/lmc-work/aero-cluster/` kept | Same |
| 2026-10-01 20:35–20:36 | Ran the pipelined RDMA integration test twice against the kv-sink server on 127.0.0.1:3100, which another stage had started after my 19:58 check found it stopped | See below |
| 2026-10-01 20:37:40 | **Restarted the kv-sink server** (`kvsink_server.sh stop; start`, same config content, log now `logs/asd-kvsink.log`) while `gpu-stage3` (e2e05) was using it; my "is it in use" check was 40 minutes old. Stopped my own runs at 20:38:10 and left the server running (pid 61964) | **Not approved**: breaks the "don't restart kv-sink if a GPU stage is using it" rule. Reported to the control tower |

No change to `aerospike-ce`, `lmc-c`, `lmc-b`, `rxe0`, `rdma_rxe`, ufw, sshd,
the GPU or `/root/lmc-work/LMCache`.
