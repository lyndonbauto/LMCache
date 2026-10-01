# Host changes outside a stage folder

| When (UTC) | Change | Approved by |
|---|---|---|
| 2026-10-01T16:12:44Z | Stopped the `rocm` container (JupyterLab on 0.0.0.0:8888) and stopped + disabled `caddy` (port 80 proxy to it). Container and image kept. Undo: `docker start rocm; systemctl enable --now caddy` | Lyndon Bauto (Slack, "Agent: ... remove it via mitigation 1") |
