# Stage 4: host changes

## gpu-stage4 (2026-10-02 02:15-03:15Z): GPU run on the new kv-sink stack

No host change: no package, kernel module, ufw, sshd or container
configuration was changed. Every service listened on 127.0.0.1. The
public-listener check ran after every LMCache and vLLM start (39 checks,
0 failures; `udp:4791` from `rdma_rxe` and `tcp:22` allowed, as before).

| Change | Where | Approved by | State left |
|---|---|---|---|
| kv-sink server (`asd`) started and restarted once per group (8 groups) | `aero-kvsink-bp`, 127.0.0.1:3700-3703; logs in each section's `kvsink_*/` | Work brief | Stopped |
| LMCache servers (`--max-gpu-workers 2` for pipe08/pipe10) and vLLM on 8000, second vLLM on 8001 | `lmc-c`; 127.0.0.1:6555 (ZMQ), 8080 (HTTP), 8000, 8001 | Work brief | None running; USED_VRAM 285 MB |
| Results | `/root/lmc-work/functional/stage4/` (run 1 kept as `run1_pipe08`, `run1_pipe10`, `run1_e2e09pipe`) | n/a | Kept |
| Harness | `9decc574`, `f79e00bf` (`functional/` only) | Work brief (harness changes) | Pushed, box pulled |

## cpu-prep-s4-flt07 (2026-10-01): harness and CPU dry run

No host change. The GPU, ports 8000/6555, `aerospike-ce`, rxe0 and
`/root/lmc-work/LMCache` were not touched. Every service listened on
127.0.0.1.

| Change | Where | Approved by | State left |
|---|---|---|---|
| kv-sink server (`asd`) started, warmed and stopped for the dry run | `aero-kvsink`, 127.0.0.1:3100-3103; log `/root/lmc-work/functional/stage4/dry/kvsink/` | Work brief | Stopped |
| CPU `lmcache server` instances for the dry run (4 configurations), each stopped by PID | `lmc-c`, 127.0.0.1:6655 (ZMQ), 8180 (HTTP), 9190 (Prometheus) | Work brief | None running |
| Harness files | `/root/lmc-work/LMCache-cpu/functional/{harness,stage4}/`, results in `/root/lmc-work/functional/stage4/` | n/a (files under /root/lmc-work) | Kept |
