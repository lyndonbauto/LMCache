#!/usr/bin/env bash
#
# Run the L2 adapter size sweep for one arm and emit a CSV of per-object
# latency. Executed ON the client node.
#
#   run_arm.sh <A|B> <results-dir>
#
# Reports per-object p50/p99 and the derived round-trip count, NOT aggregate
# throughput: the connector's num_workers parallelises across objects, not
# across the segments of a single object, so the sequential-segment chain is
# invisible in a throughput-only number.

set -euo pipefail

ARM="${1:?usage: run_arm.sh <A|B> <results-dir>}"
OUT="${2:?usage: run_arm.sh <A|B> <results-dir>}"
mkdir -p "$OUT"

source /etc/profile.d/lmcache-bench.sh

NS="${LMCACHE_AEROSPIKE_NAMESPACE:-lmcache}"
SEED="${LMCACHE_AEROSPIKE_HOSTS%%:*}"

# ---------------------------------------------------------------------------
# GATE: the live record cap, not the config file.
#
# Replicates discover_record_cap(): max-record-size first if > 0, else
# write-block-size, else the connector's hardcoded 1 MiB. Collecting data from
# a cluster that is not in the configuration you believe it is in is the
# failure mode that produces plausible, meaningless numbers.
# ---------------------------------------------------------------------------
INFO=$(asinfo -h "$SEED" -v "namespace/$NS" | tr ';' '\n')
MRS=$(echo "$INFO" | grep -oP '^max-record-size=\K[0-9]+' || echo "")
WBS=$(echo "$INFO" | grep -oP '^write-block-size=\K[0-9]+' || echo "")

if [ -n "$MRS" ] && [ "$MRS" -gt 0 ]; then
  CAP="$MRS"; SRC="max-record-size"
elif [ -n "$WBS" ] && [ "$WBS" -gt 0 ]; then
  CAP="$WBS"; SRC="write-block-size"
else
  CAP=1048576; SRC="connector hardcoded default (neither field present)"
fi

case "$ARM" in
  A) WANT=1048576 ;;
  B) WANT=8388608 ;;
  *) echo "error: arm must be A or B" >&2; exit 2 ;;
esac

echo "arm=$ARM live cap=$CAP from $SRC (want $WANT)"
if [ "$CAP" != "$WANT" ]; then
  echo "GATE FAILED: cluster is not in Arm $ARM. Refusing to collect data." >&2
  exit 1
fi
echo "GATE PASSED"

SEG=$(( CAP - 65536 ))   # connector subtracts a 64 KiB safety margin
echo "effective segment size = $SEG bytes"

SPEC="{\"type\":\"aerospike\",\"hosts\":\"$LMCACHE_AEROSPIKE_HOSTS\",\"namespace\":\"$NS\",\"set_name\":\"kv_arm$ARM\",\"num_workers\":8,\"read_timeout_ms\":120000,\"write_timeout_ms\":120000,\"target_segment_bytes\":0,\"max_record_bytes\":0}"

SIZES_KB="128 256 512 960 1024 2048 4096 8128 8192 16384 32768 65536 81920"
CSV="$OUT/arm${ARM}_sweep.csv"
echo "size_kb,cap_bytes,nseg,round_trips,store_p50_ms,load_p50_ms,load_p99_ms,load_mbps" > "$CSV"

for KB in $SIZES_KB; do
  BYTES=$(( KB * 1024 ))
  if [ "$BYTES" -le "$SEG" ]; then NSEG=1; RT=1; else
    NSEG=$(( (BYTES + SEG - 1) / SEG )); RT=$(( NSEG + 1 ))
  fi

  SL="$OUT/store_${KB}.txt"; LL="$OUT/load_${KB}.txt"
  timeout 900 lmcache bench l2 --l2-adapter "$SPEC" --data-size-kb "$KB" \
    --num-keys 4 --rounds 10 --warmup-rounds 2 --only store > "$SL" 2>&1 || echo "store $KB FAILED"
  timeout 900 lmcache bench l2 --l2-adapter "$SPEC" --data-size-kb "$KB" \
    --num-keys 4 --rounds 10 --warmup-rounds 2 --only load  > "$LL" 2>&1 || echo "load $KB FAILED"

  # The report prints a "Store"/"Load" section; take the values under each.
  sp50=$(awk '/----- Store -----/{f=1} f&&/Duration p50/{print $4; exit}' "$SL")
  lp50=$(awk '/----- Load -----/{f=1} f&&/Duration p50/{print $4; exit}' "$LL")
  lp99=$(awk '/----- Load -----/{f=1} f&&/Duration p99/{print $4; exit}' "$LL")
  lmb=$(awk '/----- Load -----/{f=1} f&&/Throughput avg/{print $4; exit}' "$LL")

  echo "$KB,$CAP,$NSEG,$RT,${sp50:-NA},${lp50:-NA},${lp99:-NA},${lmb:-NA}" >> "$CSV"
  echo "  ${KB}KB nseg=$NSEG rt=$RT load_p50=${lp50:-NA}ms p99=${lp99:-NA}ms"
done

echo "=== $CSV ==="
cat "$CSV"
