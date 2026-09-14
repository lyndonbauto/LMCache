#!/usr/bin/env bash
#
# Half A: LMCache L2 adapter size sweep against the Aerospike cluster.
# Run from the client node. See docs/half-a-storage-ceiling.md.
#
# Usage:
#   ./half_a_sweep.sh <results-dir>
#
# Expects LMCACHE_AEROSPIKE_HOSTS / _NAMESPACE / _SET in the environment; the
# client node's /etc/profile.d/lmcache-bench.sh sets them at boot.

set -euo pipefail

RESULTS_DIR="${1:?usage: half_a_sweep.sh <results-dir>}"
mkdir -p "$RESULTS_DIR"

: "${LMCACHE_AEROSPIKE_HOSTS:?not set -- source /etc/profile.d/lmcache-bench.sh}"
NAMESPACE="${LMCACHE_AEROSPIKE_NAMESPACE:-lmcache}"
SET_NAME="${LMCACHE_AEROSPIKE_SET:-kv_chunks}"

# target_segment_bytes and max_record_bytes are 0 = "discover from the server",
# which is what puts the server-side tuning on the measured path.
#
# Timeouts are 30 s, not the 1 s/2 s defaults. An 80 MiB object at a 1 MiB
# record cap is 87 sequential round trips and will time out at the default,
# which silently drops the most interesting end of the sweep.
SPEC=$(cat <<JSON
{"type":"aerospike",
 "hosts":"${LMCACHE_AEROSPIKE_HOSTS}",
 "namespace":"${NAMESPACE}",
 "set_name":"${SET_NAME}",
 "num_workers":8,
 "read_timeout_ms":30000,
 "write_timeout_ms":30000,
 "target_segment_bytes":0,
 "max_record_bytes":0}
JSON
)

# Sizes in KB. 960/1024 and 8128/8192 straddle the (cap - 64 KiB) segmentation
# threshold at the 1 MiB and 8 MiB record caps respectively, so they capture the
# discontinuity where one round trip becomes three.
SIZES_KB=(128 256 512 960 1024 2048 4096 8128 8192 16384 32768 65536 81920)

echo "record cap as LMCache will discover it:"
asinfo -h "${LMCACHE_AEROSPIKE_HOSTS%%:*}" -v "namespace/${NAMESPACE}" \
  | tr ';' '\n' | grep -E 'max-record-size|write-block-size' \
  | tee "$RESULTS_DIR/record-cap.txt"

for kb in "${SIZES_KB[@]}"; do
  echo "=== ${kb} KB ==="

  # Populate before measuring; --only load against absent keys measures nothing.
  lmcache bench l2 --l2-adapter "$SPEC" \
    --data-size-kb "$kb" --num-keys 8 --in-flight 1 \
    --rounds 3 --warmup-rounds 1 --only store \
    > "$RESULTS_DIR/store-${kb}kb.txt" 2>&1

  lmcache bench l2 --l2-adapter "$SPEC" \
    --data-size-kb "$kb" --num-keys 8 --in-flight 1 \
    --rounds 20 --warmup-rounds 3 --only load \
    > "$RESULTS_DIR/load-${kb}kb.txt" 2>&1
done

# Off-CPU flamegraph at the top of the range. A serial round-trip chain shows up
# as off-CPU wait, not CPU time, so on-cpu alone would show nothing.
lmcache bench l2 --l2-adapter "$SPEC" \
  --data-size-kb 81920 --only load --rounds 10 \
  --flamegraph on --flamegraph-mode on-cpu,off-cpu \
  > "$RESULTS_DIR/flamegraph-81920kb.txt" 2>&1

echo "done -- results in $RESULTS_DIR"
