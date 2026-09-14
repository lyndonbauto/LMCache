#!/usr/bin/env bash
# Verify the Aerospike cluster formed as ONE cluster of N, not N clusters of one.
#
# Checking a single node is not enough: a mesh misconfiguration produces
# isolated single-node clusters that each report themselves as perfectly
# healthy. Only comparing cluster_size across every node catches it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPECTED="${EXPECTED_CLUSTER_SIZE:-5}"

OUT=$("$HERE/ssm-run.sh" all 'command -v asinfo >/dev/null 2>&1 || { echo "cluster_size=NA(no-asinfo)"; exit 0; }; asinfo -h 127.0.0.1 -v statistics 2>/dev/null | tr ";" "\n" | grep -E "^cluster_size=|^cluster_key=" || echo "cluster_size=NA(aerospike-down)"')
echo "$OUT"

SIZES=$(echo "$OUT" | grep -oP 'cluster_size=\K[0-9]+' | sort -u)
echo "---------------------------------------------"
if [ "$(echo "$SIZES" | wc -l)" -eq 1 ] && [ "$SIZES" = "$EXPECTED" ]; then
  echo "PASS: every node reports cluster_size=$EXPECTED"
else
  echo "FAIL: cluster_size values seen: $(echo "$SIZES" | tr '\n' ' ')(expected all =$EXPECTED)"
  exit 1
fi
