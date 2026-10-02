# Host changes outside a stage folder

| When (UTC) | Change | Approved by |
|---|---|---|
| 2026-10-01T16:12:44Z | Stopped the `rocm` container (JupyterLab on 0.0.0.0:8888) and stopped + disabled `caddy` (port 80 proxy to it). Container and image kept. Undo: `docker start rocm; systemctl enable --now caddy` | Lyndon Bauto (Slack, "Agent: ... remove it via mitigation 1") |
| 2026-10-02T17:43:13Z | Mounted the DigitalOcean scratch disk `/dev/vdc1` (5 TB, ext4, label DOSCRATCH, empty apart from `lost+found`) at `/mnt/scratch`, `rw,relatime`, not in fstab (does not survive a reboot). No format. Used for the performance sweep's Aerospike data file at 32k-128k tokens. Undo: `umount /mnt/scratch` | Lyndon Bauto (Slack, "Agent: try mitigation 1", disk-space thread) |
