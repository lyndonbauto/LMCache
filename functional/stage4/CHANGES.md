# Stage 4: host changes

## cpu-prep-s4-flt07 (2026-10-01): harness and CPU dry run

No host change. The GPU, ports 8000/6555, `aerospike-ce`, rxe0 and
`/root/lmc-work/LMCache` were not touched. Every service listened on
127.0.0.1.

| Change | Where | Approved by | State left |
|---|---|---|---|
| kv-sink server (`asd`) started, warmed and stopped for the dry run | `aero-kvsink`, 127.0.0.1:3100-3103; log `/root/lmc-work/functional/stage4/dry/kvsink/` | Work brief | Stopped |
| CPU `lmcache server` instances for the dry run (4 configurations), each stopped by PID | `lmc-c`, 127.0.0.1:6655 (ZMQ), 8180 (HTTP), 9190 (Prometheus) | Work brief | None running |
| Harness files | `/root/lmc-work/LMCache-cpu/functional/{harness,stage4}/`, results in `/root/lmc-work/functional/stage4/` | n/a (files under /root/lmc-work) | Kept |
