#!/bin/bash
# Basic performance sanity check: LMCache + Aerospike (kv-sink server on a
# drive) on the MI300X, Llama-3.1-8B, TP=1. Runs on the host; each mode is one
# perf_session.sh session in lmc-c. Results: /root/lmc-work/functional/perf/.
#
# Usage: perf.sh [section ...]   (launch detached with functional/perf/launch.sh)
# Sections:
#   precheck   refuse to run if 8000/6555/8080/3700-3703 are taken, the GPU is
#              busy or rxe0 is down
#   nocache    one vLLM session without the connector: every length in LENGTHS
#              x every concurrency in CONCS
#   cached:<L> one length in the cached modes: start aero-kvsink-bp with a
#              device namespace (a file on the boot disk, direct-files, no
#              post-write cache) sized for L, store all 32 prompts through
#              LMCache (aon session), check the record count, then aon points
#              and lw points (LMCache restarted before every point, so every
#              hit comes from Aerospike's disk), then stop the server and
#              delete the data file
#   smoke      a short end-to-end check at 8k (2 prompts): store, aon point,
#              lw point; validates the device namespace and shared records
#   idle       wait for the GPU to be idle and print USED_VRAM
# Environment: LENGTHS ("8192 16384 32768 65536 130816"), CONCS ("1 2 4 8 16
#   32"), CAP (--pipelined-max-chunks, default 64 = 16k, the longest prompt of
#   phase 1), WINDOW_COUNT (8), L1_GEN_GB (100: general L1; lw adds the
#   windows), MIN_FREE_GB (60: refuse a data file that would leave less),
#   STOP_GRACE (60, D-18), STORE_CONC (8).
set -u
TREE_HOST=${TREE_HOST:-/root/lmc-work/LMCache}
TREE_CTR=/work/LMCache
S=/root/lmc-work/functional/perf
W=/work/functional/perf
H=$TREE_HOST/functional/harness
LENGTHS=${LENGTHS:-8192 16384 32768 65536 130816}
CONCS=${CONCS:-1 2 4 8 16 32}
CAP=${CAP:-64}
WINDOW_COUNT=${WINDOW_COUNT:-8}
L1_GEN_GB=${L1_GEN_GB:-100}
MIN_FREE_GB=${MIN_FREE_GB:-60}
STORE_CONC=${STORE_CONC:-8}
export STOP_GRACE=${STOP_GRACE:-60}
LLAMA_CHUNK=$((32 * 2 * 8 * 128 * 256 * 2))   # 32 MiB: 32 layers, K and V, 8 KV heads x 128, bf16
WIN_BYTES=$((CAP * LLAMA_CHUNK))
WIN_GB=$(( (WINDOW_COUNT * WIN_BYTES + (1 << 30) - 1) >> 30 ))
DATA_DIR=/root/lmc-work/perf-aero
DATA_FILE=$DATA_DIR/lmcache.dat
PERF_CONF=$S/aerospike-kvsink-bp-perf.conf
CONF_TMPL=$TREE_HOST/functional/configs/aerospike-kvsink-bp-perf.conf.in
CLONE=$TREE_HOST . "$TREE_HOST/functional/newstack/kvsink_bp_env.sh"
# Both cached modes allocate L1 up front (--no-l1-use-lazy, which lw's RDMA
# windows require): with lazy L1, the first retrieve after a restart took
# 9.4 s (run 1, superseded/run1_8k_lazyL1), a server cold start, not L2.
AON_L2="--no-l1-use-lazy --l2-adapter {\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$KVSINK_PORT\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"}"
LW_L2="--no-l1-use-lazy --pipelined-fetch --pipelined-max-chunks $CAP --l2-adapter {\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$KVSINK_PORT\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\",\"rdma\":{\"transport\":\"RC\",\"device_name\":\"rxe0\",\"gid_index\":1,\"window_count\":$WINDOW_COUNT,\"window_bytes\":$WIN_BYTES}}"

vram() { amd-smi metric --mem-usage 2>/dev/null | grep -m1 USED_VRAM | grep -oE '[0-9]+'; }
wait_idle() { local u=""; for _ in $(seq 60); do u=$(vram); [ -n "$u" ] && [ "$u" -lt 4000 ] && break; sleep 3; done; echo "VRAM ${u} MB"; }
progress() { echo "$(date -u +%FT%TZ) $*" | tee -a $S/progress.log; }
free_gb() { df -BG --output=avail / | tail -n 1 | tr -dc 0-9; }
asd_read_bytes() {
  local pid; pid=$(docker top "$KVSINK_CTR" -eo pid,comm 2>/dev/null | awk '$2=="asd"{print $1}')
  [ -n "$pid" ] && awk '/^read_bytes/{print $2}' "/proc/$pid/io" || echo 0
}

# session <name> <mode> <l1-gb> <lmcache-extra> <step>...
session() {
  local name=$1 mode=$2 l1=$3 extra=$4; shift 4
  mkdir -p $S/$name
  local rb0; rb0=$(asd_read_bytes)
  progress "session $name ($mode) started; $(wait_idle); free disk $(free_gb) GB"
  timeout 14400 docker exec -e LMC_EXTRA="$extra" -e L1_GB="$l1" -e STOP_GRACE="$STOP_GRACE" \
    -e L2_PORT="$KVSINK_PORT" lmc-c bash $TREE_CTR/functional/perf/perf_session.sh $W/$name "$name" "$mode" "$@" \
    > $S/$name/session_$name.txt 2>&1
  local rc=$? rb1; rb1=$(asd_read_bytes)
  echo "$((rb1 - rb0))" > $S/$name/asd_read_bytes.txt
  grep -q "listen_check.*FAIL" $S/$name/session_$name.txt && progress "!! $name: a listener off loopback (listen_check FAIL)"
  progress "session $name rc=$rc; asd read_bytes (disk) during it: $(( (rb1 - rb0) >> 20 )) MiB; \
$(grep -cE '^point ' $S/$name/session_$name.txt) points; errors: $(grep -E '^point ' $S/$name/session_$name.txt | grep -vc 'errors=0'); \
LMCache stops: $(grep -oE 'stopped \([^)]*\) exit=[0-9]+' $S/$name/session_$name.txt | sort | uniq -c | paste -sd' ')"
  return $rc
}
point_steps() {
  local len=$1 c out=()
  for c in $CONCS; do out+=("point len=$len c=$c"); done
  printf '%s\n' "${out[@]}"
}

aero_start() {
  local len=$1 fs_gb
  # KV per length: 32 prompts x len/256 chunks x 32 MiB; +40% for record
  # headers, 1 MiB write blocks and defragmentation headroom.
  fs_gb=$(( (32 * len / 256 * 32 * 14 / 10 + 1023) / 1024 ))
  [ "${2:-}" = smoke ] && fs_gb=8
  local avail; avail=$(free_gb)
  if [ $((avail - fs_gb)) -lt "$MIN_FREE_GB" ]; then
    progress "refusing: a ${fs_gb}G data file would leave $((avail - fs_gb)) GB free (< $MIN_FREE_GB)"; return 1
  fi
  bash $H/kvsink_server.sh stop >/dev/null
  rm -f "$DATA_FILE"; mkdir -p "$DATA_DIR"
  sed -e "s|@FILE@|$DATA_FILE|" -e "s|@FILESIZE@|${fs_gb}G|" "$CONF_TMPL" > "$PERF_CONF"
  KVSINK_CONF=$PERF_CONF KVSINK_LOG=$S/asd-kvsink-bp-perf.log bash $H/kvsink_server.sh start || return 1
  progress "aero-kvsink-bp started on a device namespace: file $DATA_FILE filesize ${fs_gb}G; \
$(grep -A12 'storage-engine device' "$PERF_CONF" | grep -E 'direct-files|post-write-cache|read-page-cache|max-write-cache' | paste -sd' ' | tr -s ' '); \
storage engine reported: $(python3 $H/as_info.py "$KVSINK_PORT" 'get-config:context=namespace;id=lmcache' | tr ';' '\n' | grep -E '^storage-engine(=|\.file|\.direct-files|\.post-write-cache|\.read-page-cache)' | paste -sd' '); free disk $(free_gb) GB"
}
aero_stop_delete() {
  bash $H/kvsink_server.sh stop
  rm -f "$DATA_FILE"
  progress "aero-kvsink-bp stopped, data file deleted; free disk $(free_gb) GB"
}
# store_check <session> <len> <prompts>: the namespace stats perf_session.sh
# saved right after the store settled, against 65 records per chunk (1 meta
# + 64 K/V planes of 512 KiB, each under the 1 MiB record cap).
store_check() {
  local name=$1 len=$2 nprompts=$3 objs used want f
  f=$(ls $S/$name/l2stat_*store*.txt 2>/dev/null | head -n 1)
  objs=$(tr ';' '\n' < "$f" | grep -E '^objects=' | cut -d= -f2)
  used=$(tr ';' '\n' < "$f" | grep -E '^data_used_bytes=' | cut -d= -f2)
  want=$(( nprompts * len / 256 * 65 ))
  echo "objects=$objs want=$want data_used_bytes=$used" > $S/$name/store_check.txt
  progress "store check $name: objects $objs (want $want = $nprompts prompts x $((len / 256)) chunks x 65 records), \
data_used_bytes $used; DEVICE_OVERLOAD lines $(grep -c DEVICE_OVERLOAD $S/$name/lmcache_$name.log), \
L1 refusals $(grep -ciE 'refus' $S/$name/lmcache_$name.log)"
}

sec_precheck() {
  local busy; busy=$(ss -ltn | grep -E "127.0.0.1:(8000|6555|8080|3700|3701|3702|3703) |:(8000|6555|8080) ")
  [ -z "$busy" ] || { progress "precheck: ports in use: $busy"; exit 1; }
  rdma link show | grep -q 'rxe0/1 state ACTIVE' || { progress "precheck: rxe0 not active"; exit 1; }
  progress "precheck ok: ports free, rxe0 active, $(wait_idle), free disk $(free_gb) GB, CAP=$CAP WINDOW_COUNT=$WINDOW_COUNT \
window_bytes=$WIN_BYTES (windows ${WIN_GB} GB), L1 general ${L1_GEN_GB} GB"
}
sec_nocache() {
  local steps=() len
  for len in $LENGTHS; do mapfile -t -O "${#steps[@]}" steps < <(point_steps "$len"); done
  session nocache nocache 1 "" "${steps[@]}"
}
sec_cached() {
  local len=$1 name=L$1
  aero_start "$len" || return 1
  local steps=()
  mapfile -t steps < <(point_steps "$len")
  session ${name}_aon aon "$L1_GEN_GB" "$AON_L2" "store len=$len ids=0-31 conc=$STORE_CONC" "${steps[@]}"
  store_check ${name}_aon "$len" 32
  session ${name}_lw lw $((L1_GEN_GB + WIN_GB)) "$LW_L2" "${steps[@]}"
  aero_stop_delete
}
sec_smoke() {
  aero_start 8192 smoke || return 1
  session smoke_aon aon 20 "$AON_L2" "store len=8192 ids=0-1 conc=2" "point len=8192 c=2 n=2"
  store_check smoke_aon 8192 2
  session smoke_lw lw $((20 + WIN_GB)) "$LW_L2" "point len=8192 c=2 n=2"
  aero_stop_delete
}
sec_idle() { progress "idle: $(wait_idle)"; }

mkdir -p $S
for sec in "$@"; do
  progress "section $sec started"
  case $sec in cached:*) sec_cached "${sec#cached:}";; *) "sec_$sec";; esac
  progress "section $sec finished"
done
echo "##### PERF DONE $(date -u +%T) $(wait_idle)"
