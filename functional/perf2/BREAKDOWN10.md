# kv-sink breakdown, day 3 round 9 (Sriram, idle-CPU diagnostic, 2026-10-07)

Sriram's request (Slack, 2026-10-06 20:19 PT): his AWS replica (20 vCPU, kernel 6.8 + rxe
from 6.11, 2 asd nodes) reaches ~90% of `ib_write_bw` with ~18 of 20 CPUs busy; this box
reaches 67-72% with ~10 busy. Find what keeps half the CPUs idle. No code changes.

- Droplet 165.245.134.42 (restored from the 2026-10-05 snapshot; setup in
  `functional/HOST-CHANGES.md`). asd `3f3940e42` (same tree as `3b1d52fb3`), defaults,
  memory namespace, `KV_SINK_STATS=1`. `kvlayers` on client `e8158149`.
- Reference: `ib_write_bw -s 524288 -q 16 -t 2 -D 10` on `rxe0` in `lmc-c`, mpstat 5 s.
- Each run: one server cycle, `kvlayers --fill --reps 1`, then `kvlayers --qps 16
  --duration 20`; 3 s in: `mpstat -P ALL 1 10`, `top -b -H` (2 frames, 2 s apart), and
  `/proc/interrupts` before/after the stream.
  - (a) vLLM + LMCache MP server up and idle (a `perf_session.sh` aon session whose only
    step is `sleep secs=90`, started before the stream).
  - (b) both stopped.
- Script: `scripts/breakdown10.sh`; tables: `scripts/breakdown10_report.py`.
- Outputs: `breakdown10/report.md` (full tables), `breakdown10/breakdown10.out`,
  `facts/`, `raw/`, `run_a/`, `run_b/`.
- `run_a_failed_native_mismatch/`: first attempt at (a). vLLM's `register_kv_cache`
  failed with `KeyError: 'dtypes'`: the LMCache head (`bf69253b`) has product commits
  from 2026-10-06 that need a native rebuild, and the container's native extension is
  from `07c13847`. The box tree was moved to `4461293f` (product code of `07c13847`) and
  (a) was rerun.

## Setup

- One asd node in every breakdown (1-9) and here: the mesh heartbeat is on 127.0.0.1 with
  no seeds. The stats lines come from that one node.
- `docker inspect`: no CPU limits on either container (`NanoCpus=0 CpuQuota=0
  CpusetCpus=''`). `aero-kvsink-bp`: `NetworkMode=host IpcMode=private`; `lmc-c`:
  `NetworkMode=host IpcMode=host`.
- In breakdowns 1-9, the `kvlayers` runs had vLLM and LMCache stopped (`perf.sh` sessions
  stop both at their end); only the lw runs had them up. So (b) matches those runs.

## Machine

- KVM guest, 20 vCPU Intel Xeon Platinum 8568Y+, 1 socket, 1 NUMA node, 235 GiB RAM,
  no swap. Kernel `6.8.0-138-generic`.
- `/proc/cmdline`: no `isolcpus`, `nohz_full` or `irqaffinity`; `isolated` and
  `nohz_full` in sysfs are empty. Workqueue cpumask `fffff` (all 20 CPUs), including
  `ib-comp-unb-wq`.
- `rdma_rxe`: srcversion `1AE0DE565BFD276050040E6`, upstream v6.11 rxe rebuilt on this
  droplet against MLNX OFED 24.10 (`/root/rxe-build/v6.11-20261007/rdma_rxe.ko`), on `lo`.
- No cpufreq driver and no cpuidle driver ("CPUidle driver: none", no idle states): the
  guest exposes no frequency or C-state control.
- irqbalance inactive. Steal since boot ~0.

## Results

| | GiB/s | % of ib_write_bw | CPUs busy | %usr | %sys | %soft | %steal |
|---|---|---|---|---|---|---|---|
| ib_write_bw q16 t2 | 11.94 (102.56 Gb/s) | 100% | 16.9 | | | | |
| (a) vLLM + LMCache up | 7.95 | 67% | 9.8 | 4.2 | 44.1 | 0.8 | 0.00 |
| (b) both stopped | 7.83 | 66% | 9.6 | 4.0 | 43.3 | 0.6 | 0.00 |

Stats lines (one node, 2 steady seconds):

- (a): 8.19 and 8.15 GiB/s; wire 6480/6726 us per write; 128 writes / 64 MiB in flight;
  16.0 of 16 QPs busy; placer queue 12-17; starved 0%.
- (b): 8.30 and 7.99 GiB/s; wire 6317/6714 us; same in-flight numbers.

Readings against Sriram's list:

- Steal is 0.00 on every CPU in both runs: not hypervisor steal (as seen from the guest).
- (a) and (b) are within 2%: not competing load from vLLM/LMCache.
- Interrupts: no device interrupts on the data path (rxe runs on `lo`). The top sources
  are local timer (~16k/s) and function-call IPIs (~11k/s), spread over all 20 CPUs;
  virtio sources are < 10/s. Softirq is at most 6% (CPU 0-2).
- Workqueue cpumask covers all CPUs; no isolcpus.
- No guest C-states (no cpuidle driver).

What the data does show: the idle time is spread evenly. Every CPU is 46-55% idle and
41-47% `%sys`; it is not 10 busy CPUs and 10 idle ones. `top -H` shows dozens of
`rxe_wq` kworkers at 10-15% each and the asd thread at ~13%. With 64 MiB in flight
at ~8 GiB/s, each write spends ~7.8 ms in flight (wire ~6.5 ms); `ib_write_bw` keeps
16 MiB in flight (16 QPs x depth 2) and runs at 11.94 GiB/s with 17 CPUs busy. So the
kv-sink stream is waiting on rxe work that is spread across many short-lived kworkers,
not starved of CPUs. One setup difference from the AWS replica: there are 2 asd nodes
there and one here.
