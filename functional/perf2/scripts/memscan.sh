#!/bin/bash
# perf2 step 5 (Sriram's open item): the lw 8k c=1 fetch on a storage-engine
# memory namespace, at queue_pairs 1 and 8, next to the device runs of
# qpscan.sh. Stores prompts 0-3 (the c=1 points use n=4) into a 16G
# namespace, runs aon c=1 and lw c=1 per queue_pairs, then E3 (the box-only
# pump.py print patch, as in qpscan.sh) with asd's threads sampled.
# Captures uname -r and numactl -H. Results: /root/lmc-work/functional/perf2/memns/.
set -u
T=/root/lmc-work/LMCache
export PERF_OUT=/root/lmc-work/functional/perf2/memns DATA_DIR=/mnt/scratch/perf-aero CONCS="1"
export CONF_TMPL=$T/functional/configs/aerospike-kvsink-bp-perf-mem.conf.in FS_PCT=50 STORE_IDS=0-3
PERF="bash $T/functional/perf/perf.sh"
E3_PATCH=${E3_PATCH:-/root/lmc-work/functional/perf2/e3_pump_patch.py}
PUMP=lmcache/v1/layerwise/pump.py
mkdir -p $PERF_OUT
say() { echo "$(date -u +%FT%TZ) memscan: $*" | tee -a $PERF_OUT/progress.log; }
clean_tree() { [ -z "$(git -C $T status --porcelain -- lmcache csrc)" ]; }

clean_tree || { say "lmcache/ or csrc/ not clean before the scan; stopping"; exit 1; }
{ echo "host uname -r: $(uname -r)"; echo "== host numactl -H"; numactl -H 2>&1 || docker exec lmc-c numactl -H; \
  echo "== nproc $(nproc)"; lscpu | grep -E 'Model name|NUMA|Thread|Core|Socket'; } > $PERF_OUT/host_topology.txt
$PERF precheck qpstore:8192 qplw:8192:1 qplw:8192:8
grep -E 'storage-engine' $PERF_OUT/progress.log | tail -n 1 | grep -q 'storage-engine=memory' \
  && say "namespace reported storage-engine=memory" || say "!! namespace did not report storage-engine=memory"
python3 "$E3_PATCH" "$T/$PUMP" && say "E3 patch applied to $PUMP"
$PERF timeline:1 timeline:8
git -C $T checkout -- $PUMP
clean_tree && say "E3 patch reverted; lmcache/ and csrc/ clean" || { say "!! tree not clean after the E3 revert"; exit 1; }
$PERF exp_stop
say "MEMSCAN DONE"
