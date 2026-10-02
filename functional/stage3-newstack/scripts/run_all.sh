#!/bin/bash
# Step 3 D-15 probe, then all of Stage 3 on the new kv-sink stack, unattended.
# Run on the host from launch.sh (new-stack env, STAGE_DIR=stage3-newstack).
T=/root/lmc-work/LMCache/functional
P=/root/lmc-work/functional/stage3-newstack/progress.log
step() { echo "$(date -u +%FT%TZ) run_all: $*" >> $P; }
step "d15 start"; bash $T/stage3-newstack/sanity.sh d15
step "stage3.sh start"; CAPS="64 4" FAULT_POLICY=fail bash $T/stage3/stage3.sh cfg08 e2e04 e2e05 pipe05 pipe06 pipe12 rdma06gpu
step "stage3_gpt.sh start"; bash $T/stage3/stage3_gpt.sh e2e08p e2e08pl2 pipe11
bash $T/harness/kvsink_server.sh stop >> $P 2>&1
step "all done; $(amd-smi metric --mem-usage | grep -m1 USED_VRAM)"
