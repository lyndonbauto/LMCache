#!/bin/bash
# Host: kvsink_restart through host_actions.sh with the new-stack env.
set -u
cd /root/lmc-work/LMCache-1a
docker ps --format '{{.Names}}' | tr '\n' ' '; echo
ss -ltn | grep -E '127.0.0.1:37[0-9]{2} ' | awk '{print $4}' | paste -sd' '
CLONE=/root/lmc-work/LMCache-1a . functional/newstack/kvsink_bp_env.sh
TREE_HOST=/root/lmc-work/LMCache-1a TREE_CTR=/work/LMCache PY314=/bin/true
source functional/harness/host_actions.sh
kvsink_restart /root/lmc-work/functional/newstack/logs/harness_check
echo "host pid: $(kvsink_host_pid)"
grep -E "kv-sink: (RC|using)" /root/lmc-work/functional/newstack/logs/harness_check/asd-kvsink.log
