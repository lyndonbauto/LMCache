#!/bin/bash
# After run_all.sh: the D-15 rerun with a 60 s shutdown grace, then stop kv-sink.
T=/root/lmc-work/LMCache/functional
P=/root/lmc-work/functional/stage3-newstack/progress.log
while pgrep -f "stage3-newstack/scripts/run_all.sh" >/dev/null; do sleep 20; done
echo "$(date -u +%FT%TZ) run_after: d15 (grace 60) start" >> $P
D15_GRACE=60 bash $T/stage3-newstack/sanity.sh d15
bash $T/harness/kvsink_server.sh stop >> $P 2>&1
echo "$(date -u +%FT%TZ) run_after: done; $(amd-smi metric --mem-usage | grep -m1 USED_VRAM)" >> $P
