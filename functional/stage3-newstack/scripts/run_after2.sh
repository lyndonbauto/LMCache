#!/bin/bash
# After run_after.sh: the e2e04 short groups on the 16G namespace, then stop kv-sink.
T=/root/lmc-work/LMCache/functional
P=/root/lmc-work/functional/stage3-newstack/progress.log
while pgrep -f "stage3-newstack/scripts/run_after.sh" >/dev/null; do sleep 20; done
echo "$(date -u +%FT%TZ) run_after2: e2e04s16 start" >> $P
CAPS="64 4" bash $T/stage3-newstack/sanity.sh e2e04s16
bash $T/harness/kvsink_server.sh stop >> $P 2>&1
echo "$(date -u +%FT%TZ) run_after2: done; $(amd-smi metric --mem-usage | grep -m1 USED_VRAM)" >> $P
