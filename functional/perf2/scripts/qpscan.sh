#!/bin/bash
# perf2 step 4: lw full hits at 8k and 16k, c = 1, 2, 4, at queue_pairs
# QPS (1 4 8 16), against an aon store + aon points on the same data file,
# plus the E3 timeline at 8k c=1 for each queue_pairs value. Runs on the host,
# results under /root/lmc-work/functional/perf2/.
#
# E3 needs a throwaway print patch in lmcache/v1/layerwise/pump.py. It lives
# outside the repo (E3_PATCH, applied with python, reverted with git checkout)
# and is never committed; the script stops if the tree is not clean after.
set -u
T=/root/lmc-work/LMCache
# FS_PCT 200: the data file at 2x the KV (32 prompts), well under stop-writes.
export PERF_OUT=/root/lmc-work/functional/perf2 DATA_DIR=/mnt/scratch/perf-aero CONCS="1 2 4" FS_PCT=200
PERF="bash $T/functional/perf/perf.sh"
QPS=${QPS:-1 4 8 16}
E3_PATCH=${E3_PATCH:-/root/lmc-work/functional/perf2/e3_pump_patch.py}
PUMP=lmcache/v1/layerwise/pump.py
say() { echo "$(date -u +%FT%TZ) qpscan: $*" | tee -a $PERF_OUT/progress.log; }
clean_tree() { [ -z "$(git -C $T status --porcelain -- lmcache csrc)" ]; }

clean_tree || { say "lmcache/ or csrc/ not clean before the scan; stopping"; exit 1; }
lw8=(); lw16=(); tl=()
for qp in $QPS; do lw8+=("qplw:8192:$qp"); lw16+=("qplw:16384:$qp"); tl+=("timeline:$qp"); done

$PERF precheck qpstore:8192 "${lw8[@]}"
python3 "$E3_PATCH" "$T/$PUMP" && say "E3 patch applied to $PUMP"
$PERF "${tl[@]}"
git -C $T checkout -- $PUMP
clean_tree && say "E3 patch reverted; lmcache/ and csrc/ clean" || { say "!! tree not clean after the E3 revert"; exit 1; }
$PERF exp_stop
$PERF qpstore:16384 "${lw16[@]}" exp_stop
say "QPSCAN DONE"
