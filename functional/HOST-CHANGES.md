# Host changes outside a stage folder

| When (UTC) | Change | Approved by |
|---|---|---|
| 2026-10-01T16:12:44Z | Stopped the `rocm` container (JupyterLab on 0.0.0.0:8888) and stopped + disabled `caddy` (port 80 proxy to it). Container and image kept. Undo: `docker start rocm; systemctl enable --now caddy` | Lyndon Bauto (Slack, "Agent: ... remove it via mitigation 1") |
| 2026-10-02T17:43:13Z | Mounted the DigitalOcean scratch disk `/dev/vdc1` (5 TB, ext4, label DOSCRATCH, empty apart from `lost+found`) at `/mnt/scratch`, `rw,relatime`, not in fstab (does not survive a reboot). No format. Used for the performance sweep's Aerospike data file at 32k-128k tokens. Undo: `umount /mnt/scratch` | Lyndon Bauto (Slack, "Agent: try mitigation 1", disk-space thread) |

## New droplet 129.212.177.62 (perf2, from 2026-10-05)

A fresh droplet of the same size, set up for `functional/perf2/`. The rows above are the old
droplet's.

| When (UTC) | Change | Approved by |
|---|---|---|
| 2026-10-05T18:13:48Z | Stopped the `rocm` container (JupyterLab) and stopped + disabled `caddy`, as on the old droplet, so only port 22 is public. Container and image kept. Undo: `docker start rocm; systemctl enable --now caddy` | Valentyn Kahamlyk (request to rerun on this droplet; announced in Slack at the start) |
| about 2026-10-05T18:16Z | Soft-RoCE: the in-tree `rdma_rxe` does not load against MLNX OFED 24.10's `ib_core` (`disagrees about version of symbol`, err -22). Built the upstream v6.11 `rdma_rxe` out of tree against the OFED headers and symbols (`/root/rxe-build/v6.11/`, `make -C /lib/modules/6.8.0-138-generic/build M=$PWD CONFIG_RDMA_RXE=m KBUILD_EXTRA_SYMBOLS=/usr/src/ofa_kernel/x86_64/6.8.0-138-generic/Module.symvers` with the OFED include path), then `modprobe udp_tunnel ip6_udp_tunnel; insmod /root/rxe-build/v6.11/rdma_rxe.ko; rdma link add rxe0 type rxe netdev lo`. Not persistent across a reboot. Undo: `rdma link delete rxe0; rmmod rdma_rxe` | Valentyn Kahamlyk (same request) |
| before 2026-10-05T18:19:46Z | Mounted the scratch disk `/dev/vdc1` at `/mnt/scratch`, `rw,relatime`, not in fstab. No format. Holds the Aerospike data file (`/mnt/scratch/perf-aero/`). Undo: `umount /mnt/scratch` | Valentyn Kahamlyk (same request) |
| 2026-10-05 | Containers created for perf2 (`functional/perf2/scripts/create_containers.sh`): `aero-kvsink-bp` (ubuntu:24.04) and `lmc-c` (`vllm/vllm-openai-rocm:v0.27.1`), host network, every listener on 127.0.0.1. Packages added inside them are in `functional/perf2/CHANGES.md` | Valentyn Kahamlyk (same request) |
| 2026-10-05T22:21:03Z-22:22:06Z | Follow-up D: built a traced kv-sink server (box-only patch, `functional/perf2/FOLLOWUP-A-D.md`). A copy of the server tree failed to build (its CMake caches hold the original tree's paths), and its `make` reinstalled the ICU module's install files into the original tree's `modules/icu/icu4c/installation/` (same files, from the same sources); the copy was then deleted. The patch was then applied in place, built, copied to `/root/lmc-work/asd-trace/asd`, reverted and rebuilt, and the original `asd` put back from `/root/lmc-work/asd-trace/asd.orig` (md5 `0b953fb421486d003e28ccc90dde2d7b`, as before). `kv_sink.c`/`kv_sink.h` match their originals. Undo: `rm -r /root/lmc-work/asd-trace` | Valentyn Kahamlyk (Slack, "Agent: start with A and D") |
| 2026-10-05T22:56Z-23:05Z | Breakdown (`functional/perf2/BREAKDOWN.md`): backed up the box LMCache tree's uncommitted CRLF-only copies to `/root/lmc-work/lmcache-tree-backup-20261005T225556Z/` and reset it to `b947831d`. Built asd `314564cfb` in place in `/root/lmc-work/aerospike-server-kvsink-bp` (originals in `/root/lmc-work/asd-314564cfb/orig/`), copied the binary to `/root/lmc-work/asd-314564cfb/asd`, restored the four files (`cmp` clean), rebuilt, and put back the original `asd` (md5 `0b953fb421486d003e28ccc90dde2d7b`, checked again after the runs). Undo: `rm -r /root/lmc-work/asd-314564cfb /root/lmc-work/lmcache-tree-backup-20261005T225556Z` | Sriram Subramanian (Slack request in the final-results thread), authorized by Valentyn Kahamlyk (`Agent: follow Sriram's instructions`) |

## Droplet 134.199.201.175 (from the 2026-10-05 snapshot)

Created by Valentyn from a snapshot of 129.212.177.62, so the containers, builds, model and `/root/lmc-work` came over. The scratch disk did not (empty `/dev/vdc1`), and nothing that a reboot clears survived.

| When (UTC) | Change | Approved by |
|---|---|---|
| 2026-10-06T15:13:25Z | `caddy` was active again on public port 80 after the restore: `systemctl stop caddy; systemctl disable caddy`. The `rocm` container stayed stopped. Undo: `systemctl enable --now caddy` | Valentyn Kahamlyk (control-tower prompt, day 2) |
| 2026-10-06T15:13:25Z | Mounted `/dev/vdc1` at `/mnt/scratch` (empty, not in fstab; no format). Undo: `umount /mnt/scratch` | same |
| 2026-10-06T15:13:25Z | Soft-RoCE: `modprobe udp_tunnel ip6_udp_tunnel; insmod /root/rxe-build/v6.11/rdma_rxe.ko; rdma link add rxe0 type rxe netdev lo` (RC, GID index 1 `::ffff:127.0.0.1`, MTU 4096). Undo: `rdma link delete rxe0; rmmod rdma_rxe` | same |
| 2026-10-06T15:13:25Z | `docker start aero-kvsink-bp lmc-c` (exited on the restore) | same |
| 2026-10-06 | `/root/lmc-work/LMCache` fast-forwarded to `prototype-stage-1b`; day-2 harness scripts deployed into its `functional/` before they were committed | same |
