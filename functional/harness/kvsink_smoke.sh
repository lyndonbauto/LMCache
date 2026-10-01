#!/bin/bash
# Smoke test for the kv-sink server on 127.0.0.1:3100. Runs inside aero-kvsink.
#   1. info: build, status, namespace lmcache.
#   2. the server branch's examples/kv-sink/kvsink_client (kv-sink-fetch, one
#      record, RC on rxe0 GID 1), built from a copy with the port set to 3100.
#   3. LMCache's pipelined integration test (kv-sink-register, then
#      kv-sink-fetch-pipelined with one write-with-immediate per slot) against
#      the server, from LMCACHE_DIR, which must hold built extensions.
# Usage: kvsink_smoke.sh <out-dir>
set -u
OUT=${1:?out dir}
SRV=/root/lmc-work/aerospike-server-kvsink
LMCACHE_DIR=${LMCACHE_DIR:-/root/lmc-work/LMCache-cpu}
mkdir -p "$OUT"
python -c "import socket; socket.create_connection(('127.0.0.1', 3100), 1)" 2>/dev/null \
  || { echo "kv-sink server is not listening on 127.0.0.1:3100"; exit 1; }

echo "== 1. info"
python - <<'EOF' 2>&1 | tee "$OUT/info.txt"
import aerospike
c = aerospike.client({"hosts": [("127.0.0.1", 3100)]}).connect()
for cmd in ("build", "status", "edition", "namespaces"):
    print(cmd, "=>", c.info_random_node(cmd).strip())
ns = c.info_random_node("namespace/lmcache")
keep = ("objects", "storage-engine", "data-size", "replication-factor", "max-record-size")
print("namespace/lmcache =>", ";".join(kv for kv in ns.strip().split("\t")[-1].split(";") if kv.split("=")[0].startswith(keep)))
c.close()
EOF

echo "== 2. examples/kv-sink/kvsink_client (kv-sink-fetch)"
python - <<'EOF' 2>&1 | tee "$OUT/put.txt"
import aerospike
c = aerospike.client({"hosts": [("127.0.0.1", 3100)]}).connect()
key = ("lmcache", "kvsink_smoke", "record-1")
payload = bytes((b"kv-sink smoke " * 4682)[:65536])
c.put(key, {"v": bytearray(payload)})
print("digest", aerospike.calc_digest(*key).hex(), "len", len(payload))
c.close()
EOF
DIGEST=$(awk '/^digest/{print $2}' "$OUT/put.txt")
LEN=$(awk '/^digest/{print $4}' "$OUT/put.txt")
B=/root/lmc-work/functional/stage3/kvsink_client_build
mkdir -p $B
sed 's/^#define SERVICE_PORT 3000$/#define SERVICE_PORT 3100/' $SRV/examples/kv-sink/kvsink_client.c > $B/kvsink_client.c
grep -q 'SERVICE_PORT 3100' $B/kvsink_client.c || { echo "port patch failed"; exit 1; }
cc -O2 -g -Wall -Wextra -std=gnu11 -o $B/kvsink_client $B/kvsink_client.c -libverbs -lefa
$B/kvsink_client --host 127.0.0.1 --ns lmcache --digest "$DIGEST" --len "$LEN" 2>&1 | tee "$OUT/kvsink_client.txt"
echo "kvsink_client exit=${PIPESTATUS[0]}" | tee -a "$OUT/kvsink_client.txt"

echo "== 3. LMCache pipelined integration test (kv-sink-fetch-pipelined)"
cd "$LMCACHE_DIR" || exit 1
PYTHONDONTWRITEBYTECODE=1 RUN_AEROSPIKE_INTEGRATION=1 \
  AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=3100 AEROSPIKE_TEST_NAMESPACE=lmcache \
  RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  timeout 600 python -m pytest -p no:cacheprovider -v -rfEs \
  tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py 2>&1 | tee "$OUT/pipelined_it.txt"
echo "pytest exit=${PIPESTATUS[0]}" | tee -a "$OUT/pipelined_it.txt"
