set -uo pipefail
source /etc/profile.d/lmcache-bench.sh
H=10.240.1.10:3000
OBJ=983040   # 960 KiB: one segment under the Arm A cap, so no client-side sharding
run() {
  NS=$1; LABEL=$2; KEYS=$3
  echo "===== $LABEL ($NS, $KEYS keys x 960KiB = $((KEYS*960/1024)) MiB) ====="
  asbench -h $H -n $NS -s ceil -o B$OBJ -k $KEYS -w I -z 64 -t 40 2>&1 | tail -3
  echo "--- read ---"
  asbench -h $H -n $NS -s ceil -o B$OBJ -k $KEYS -w RU,100 -z 256 -t 60 2>&1 | grep -E "read\(|hwm" | tail -6
}
run lmcache       "FLASH-RESIDENT" 60000
run lmcache_mem   "DRAM-RESIDENT"  20000