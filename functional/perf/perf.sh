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
#   lwwait:<L> store again, then lw points with the connector's layerwise
#              wait timeout raised to LW_WAIT (600 s) (mode lw_wait600)
#   cached2:<L> phase 2 (32k-128k), one length, one data file: store in
#              batches of STORE_BATCH_GB of KV with an LMCache restart after
#              each (a store burst larger than L1's headroom is refused,
#              D-25), aon points, lw points with the default 5 s wait (the
#              series stops at the first engine stop), lw points with the
#              wait raised to LW_WAIT (600 s up to 32k, 1800 s above; mode
#              lw_wait<secs>), then delete the data file. The pipelined cap
#              is the prompt's chunk count, and window_count windows of
#              cap x 32 MiB fill LW_WINDOWS_GB (2 to 8 windows)
#              With AON_ONLY=1, only the store and the aon points.
#   finish:<L> wait for a driverless L<L>_aon session to exit, then the
#              store check and data file deletion
#   fio        O_DIRECT read and write throughput of the data disk(s) with
#              fio in the kv-sink container (FIO_DIRS)
#   smoke      a short end-to-end check at 8k (2 prompts): store, aon point,
#              lw point; validates the device namespace and shared records
#   smoke2     cached2's 128k setup with one prompt: store, aon c=1 n=1, lw
#              c=1 n=1 (checks the 2 x 16 GiB windows and the L1 sizes)
#   idle       wait for the GPU to be idle and print USED_VRAM
# Environment: LENGTHS ("8192 16384 32768 65536 130816"), CONCS ("1 2 4 8 16
#   32"), CAP (--pipelined-max-chunks, default 64 = 16k, the longest prompt of
#   phase 1), WINDOW_COUNT (8), L1_GEN_GB (100: general L1; lw adds the
#   windows), MIN_FREE_GB (60: refuse a data file that would leave less on
#   its filesystem), STOP_GRACE (60, D-18), STORE_CONC (8), DATA_DIR (the
#   Aerospike data file's directory, default /root/lmc-work/perf-aero on the
#   boot disk). Phase 2 (cached2): AON_L1_GB (180), LW_GEN_GB (140),
#   LW_WINDOWS_GB (32), STORE_BATCH_GB (64), LW_WAIT (by length).
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
DATA_DIR=${DATA_DIR:-/root/lmc-work/perf-aero}
DATA_FILE=$DATA_DIR/lmcache.dat
AON_L1_GB=${AON_L1_GB:-180}
LW_GEN_GB=${LW_GEN_GB:-140}
LW_WINDOWS_GB=${LW_WINDOWS_GB:-32}
STORE_BATCH_GB=${STORE_BATCH_GB:-64}
FIO_DIRS=${FIO_DIRS:-/mnt/scratch/perf-aero /root/lmc-work/perf-aero}
PERF_CONF=$S/aerospike-kvsink-bp-perf.conf
CONF_TMPL=$TREE_HOST/functional/configs/aerospike-kvsink-bp-perf.conf.in
CLONE=$TREE_HOST . "$TREE_HOST/functional/newstack/kvsink_bp_env.sh"
# Both cached modes allocate L1 up front (--no-l1-use-lazy, which lw's RDMA
# windows require): with lazy L1, the first retrieve after a restart took
# 9.4 s (run 1, superseded/run1_8k_lazyL1), a server cold start, not L2.
AON_L2="--no-l1-use-lazy --l2-adapter {\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$KVSINK_PORT\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"}"
# lw_l2 <cap> <window_count>: lw server flags; each window holds cap chunks.
lw_l2() {
  echo "--no-l1-use-lazy --pipelined-fetch --pipelined-max-chunks $1 --l2-adapter {\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$KVSINK_PORT\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\",\"rdma\":{\"transport\":\"RC\",\"device_name\":\"rxe0\",\"gid_index\":1,\"window_count\":$2,\"window_bytes\":$(($1 * LLAMA_CHUNK))}}"
}
LW_L2=$(lw_l2 "$CAP" "$WINDOW_COUNT")

vram() { amd-smi metric --mem-usage 2>/dev/null | grep -m1 USED_VRAM | grep -oE '[0-9]+'; }
wait_idle() { local u=""; for _ in $(seq 60); do u=$(vram); [ -n "$u" ] && [ "$u" -lt 4000 ] && break; sleep 3; done; echo "VRAM ${u} MB"; }
progress() { echo "$(date -u +%FT%TZ) $*" | tee -a $S/progress.log; }
free_gb() { df -BG --output=avail "${1:-/}" | tail -n 1 | tr -dc 0-9; }
disks() { echo "free: / $(free_gb) GB, data dir $(free_gb "$DATA_DIR") GB"; }
asd_read_bytes() {
  local pid; pid=$(docker top "$KVSINK_CTR" -eo pid,comm 2>/dev/null | awk '$2=="asd"{print $1}')
  [ -n "$pid" ] && awk '/^read_bytes/{print $2}' "/proc/$pid/io" || echo 0
}

# session <name> <mode> <l1-gb> <lmcache-extra> <step>...
session() {
  local name=$1 mode=$2 l1=$3 extra=$4; shift 4
  mkdir -p $S/$name
  local rb0; rb0=$(asd_read_bytes)
  progress "session $name ($mode) started; L1 ${l1} GB; $(wait_idle); $(disks)"
  ( while sleep 10; do echo "$(date -u +%T) $(free -m | awk '/^Mem/{print "used_mb="$3" avail_mb="$7}')"; done ) \
    > $S/$name/hostmem.txt 2>&1 &
  local memloop=$!
  timeout 21600 docker exec -e LMC_EXTRA="$extra" -e L1_GB="$l1" -e STOP_GRACE="$STOP_GRACE" \
    -e LW_WAIT_TIMEOUT="${SESSION_LW_WAIT:-}" -e STOP_ON_ENGINE_STOP="${STOP_ON_ENGINE_STOP:-0}" \
    -e L2_PORT="$KVSINK_PORT" lmc-c bash $TREE_CTR/functional/perf/perf_session.sh $W/$name "$name" "$mode" "$@" \
    > $S/$name/session_$name.txt 2>&1
  local rc=$? rb1; rb1=$(asd_read_bytes)
  kill "$memloop" 2>/dev/null
  echo "$((rb1 - rb0))" > $S/$name/asd_read_bytes.txt
  progress "session $name host memory: min available $(grep -oE 'avail_mb=[0-9]+' $S/$name/hostmem.txt | cut -d= -f2 | sort -n | head -n 1) MB"
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
  # KV per length: 32 prompts x len/256 chunks x 32 MiB, times FS_PCT/100
  # (140 in phase 1). Aerospike stops writes at stop-writes-used-pct (default
  # 70), so the full store must stay under 70% of the file: 140 puts it at
  # about 71% (the 32k store of run 2b lost 34 chunks); cached2 uses 200.
  fs_gb=$(( (32 * len / 256 * 32 * ${FS_PCT:-140} / 100 + 1023) / 1024 ))
  [ "${2:-}" = smoke ] && fs_gb=8
  [ "${2:-}" = smoke2 ] && fs_gb=32
  bash $H/kvsink_server.sh stop >/dev/null
  rm -f "$DATA_FILE"; mkdir -p "$DATA_DIR"
  local avail; avail=$(free_gb "$DATA_DIR")
  if [ $((avail - fs_gb)) -lt "$MIN_FREE_GB" ]; then
    progress "refusing: a ${fs_gb}G data file would leave $((avail - fs_gb)) GB free (< $MIN_FREE_GB)"; return 1
  fi
  sed -e "s|@FILE@|$DATA_FILE|" -e "s|@FILESIZE@|${fs_gb}G|" "$CONF_TMPL" > "$PERF_CONF"
  KVSINK_CONF=$PERF_CONF KVSINK_LOG=$S/asd-kvsink-bp-perf.log bash $H/kvsink_server.sh start || return 1
  progress "aero-kvsink-bp started on a device namespace: file $DATA_FILE filesize ${fs_gb}G; \
$(grep -A12 'storage-engine device' "$PERF_CONF" | grep -E 'direct-files|post-write-cache|read-page-cache|max-write-cache' | paste -sd' ' | tr -s ' '); \
storage engine reported: $(python3 $H/as_info.py "$KVSINK_PORT" 'get-config:context=namespace;id=lmcache' | tr ';' '\n' | grep -E '^storage-engine(=|\.file|\.direct-files|\.post-write-cache|\.read-page-cache)' | paste -sd' '); $(disks)"
}
aero_stop_delete() {
  bash $H/kvsink_server.sh stop
  rm -f "$DATA_FILE"
  progress "aero-kvsink-bp stopped, data file deleted; $(disks)"
}
# store_check <session> <len> <prompts>: the namespace stats perf_session.sh
# saved right after the store settled, against 65 records per chunk (1 meta
# + 64 K/V planes of 512 KiB, each under the 1 MiB record cap).
store_check() {
  local name=$1 len=$2 nprompts=$3 objs used want f
  f=$(ls $S/$name/l2stat_*store*.txt 2>/dev/null | head -n 1)
  objs=$(tr ';' '\n' < "$f" | grep -E '^objects=' | cut -d= -f2)
  used=$(tr ';' '\n' < "$f" | grep -E '^data_used_bytes=' | cut -d= -f2)
  local sw werr pct
  sw=$(tr ';' '\n' < "$f" | grep -E '^stop_writes=' | cut -d= -f2)
  werr=$(tr ';' '\n' < "$f" | grep -E '^client_write_error=' | cut -d= -f2)
  pct=$(tr ';' '\n' < "$f" | grep -E '^data_used_pct=' | cut -d= -f2)
  want=$(( nprompts * len / 256 * 65 ))
  echo "objects=$objs want=$want data_used_bytes=$used stop_writes=$sw client_write_error=$werr data_used_pct=$pct" \
    > $S/$name/store_check.txt
  [ "${objs:-0}" -lt "$want" ] && progress "!! store check $name: fewer records than wanted"
  progress "store check $name: objects $objs (want $want = $nprompts prompts x $((len / 256)) chunks x 65 records), \
data_used_bytes $used (${pct}%), stop_writes $sw, client_write_error $werr; DEVICE_OVERLOAD lines $(grep -c DEVICE_OVERLOAD $S/$name/lmcache_$name.log) (D-26), \
L1 refusals $(grep -ciE 'refus' $S/$name/lmcache_$name.log), \
'Failed to batched allocate' $(grep -c 'Failed to batched allocate' $S/$name/lmcache_$name.log) (D-25)"
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
# lwwait:<len>: lw again with lmcache.mp.layerwise_wait_timeout_seconds
# LW_WAIT (600). With the default 5 s, a retrieve queued behind others on
# LMCache's one worker thread (about 1 s per 8k prompt on Soft-RoCE) times
# out and stops vLLM's engine (run phase1c, 8k c=8). The data is stored
# again by an aon store-only session first.
sec_lwwait() {
  local len=$1 name=L$1
  aero_start "$len" || return 1
  session ${name}_store aon "$L1_GEN_GB" "$AON_L2" "store len=$len ids=0-31 conc=$STORE_CONC"
  store_check ${name}_store "$len" 32
  local steps=()
  mapfile -t steps < <(point_steps "$len")
  SESSION_LW_WAIT=${LW_WAIT:-600} session ${name}_lwwait lw $((L1_GEN_GB + WIN_GB)) "$LW_L2" "${steps[@]}"
  aero_stop_delete
}
sec_smoke() {
  aero_start 8192 smoke || return 1
  session smoke_aon aon 20 "$AON_L2" "store len=8192 ids=0-1 conc=2" "point len=8192 c=2 n=2"
  store_check smoke_aon 8192 2
  session smoke_lw lw $((20 + WIN_GB)) "$LW_L2" "point len=8192 c=2 n=2"
  aero_stop_delete
}
# lw_sizing <len>: echo "<cap> <window_count> <windows GB>" for cached2.
lw_sizing() {
  local cap=$(($1 / 256)) wc
  wc=$((LW_WINDOWS_GB * 1024 / (cap * 32)))
  [ "$wc" -lt 2 ] && wc=2
  [ "$wc" -gt 8 ] && wc=8
  echo "$cap $wc $(( (wc * cap * 32 + 1023) / 1024 ))"
}
# store_steps <len>: store steps in batches of STORE_BATCH_GB of KV, with an
# LMCache restart (empty L1) between batches.
store_steps() {
  local len=$1 bs i last
  bs=$((STORE_BATCH_GB * 1024 / (len / 256 * 32)))
  [ "$bs" -lt 1 ] && bs=1
  [ "$bs" -gt 32 ] && bs=32
  for ((i = 0; i < 32; i += bs)); do
    last=$((i + bs - 1)); [ "$last" -gt 31 ] && last=31
    [ "$i" -gt 0 ] && echo "restart"
    echo "store len=$len ids=$i-$last conc=$((last - i + 1))"
  done
}
sec_cached2() {
  local len=$1 name=L$1 wait cap wc wgb lw
  wait=${LW_WAIT:-$([ "$len" -le 32768 ] && echo 600 || echo 1800)}
  read -r cap wc wgb < <(lw_sizing "$len")
  lw=$(lw_l2 "$cap" "$wc")
  FS_PCT=${FS_PCT:-200} aero_start "$len" || return 1
  local steps=() pts=()
  mapfile -t steps < <(store_steps "$len")
  mapfile -t pts < <(point_steps "$len")
  progress "store plan $name: ${#steps[@]} steps ($(printf '%s; ' "${steps[@]}"))"
  session ${name}_aon aon "$AON_L1_GB" "$AON_L2" "${steps[@]}" "${pts[@]}"
  store_check ${name}_aon "$len" 32
  if [ "${AON_ONLY:-0}" = 1 ]; then
    progress "$name: AON_ONLY=1, no lw sessions"
    aero_stop_delete
    return
  fi
  progress "lw sizing $name: pipelined-max-chunks $cap, window_count $wc x window_bytes $((cap * LLAMA_CHUNK)) \
(${wgb} GB of windows), general L1 ${LW_GEN_GB} GB, L1 total $((LW_GEN_GB + wgb)) GB; long wait ${wait} s"
  STOP_ON_ENGINE_STOP=1 session ${name}_lw lw $((LW_GEN_GB + wgb)) "$lw" "${pts[@]}"
  [ -f $S/${name}_lw/engine_stopped_${name}_lw.txt ] && \
    progress "${name}_lw: series stopped, engine stopped at $(cat $S/${name}_lw/engine_stopped_${name}_lw.txt)"
  SESSION_LW_WAIT=$wait session ${name}_lwwait$wait lw $((LW_GEN_GB + wgb)) "$lw" "${pts[@]}"
  aero_stop_delete
}
# finish:<len>: the end of cached2 for an aon session that ran without its
# driver: wait for the session to exit, check the store, delete the data.
sec_finish() {
  local len=$1
  while pgrep -f "perf_session.sh $W/L${len}_aon " >/dev/null; do sleep 15; done
  progress "L${len}_aon session ended; $(grep -cE '^point ' $S/L${len}_aon/session_L${len}_aon.txt 2>/dev/null) point lines"
  store_check L${len}_aon "$len" 32
  aero_stop_delete
}
sec_smoke2() {
  local cap wc wgb
  read -r cap wc wgb < <(lw_sizing 130816)
  aero_start 130816 smoke2 || return 1
  session smoke2_aon aon "$AON_L1_GB" "$AON_L2" "store len=130816 ids=0-0 conc=1" "point len=130816 c=1 n=1"
  store_check smoke2_aon 130816 1
  progress "smoke2 lw sizing: cap $cap, $wc x $((cap * LLAMA_CHUNK)) B windows, L1 $((LW_GEN_GB + wgb)) GB"
  session smoke2_lw lw $((LW_GEN_GB + wgb)) "$(lw_l2 "$cap" "$wc")" "point len=130816 c=1 n=1"
  aero_stop_delete
}
# fio: O_DIRECT throughput of each data disk, from the kv-sink container
# (the same mount namespace and filesystem asd reads through).
sec_fio() {
  local d tag f
  docker exec "$KVSINK_CTR" bash -c 'command -v fio >/dev/null || (apt-get update -qq && apt-get install -y -qq fio >/dev/null)' \
    || { progress "fio: install failed"; return 1; }
  mkdir -p $S/fio
  for d in $FIO_DIRS; do
    tag=$(echo "$d" | tr '/' '_')
    f=$d/fio.test
    mkdir -p "$d"
    [ $(( $(free_gb "$d") - 32 )) -lt "$MIN_FREE_GB" ] && { progress "fio: skipping $d (free space)"; continue; }
    docker exec "$KVSINK_CTR" bash -c "
      fio --name=seqwrite_1M --filename=$f --size=32G --rw=write --bs=1M --direct=1 --ioengine=libaio --iodepth=32 --end_fsync=1
      fio --name=seqread_1M --filename=$f --size=32G --rw=read --bs=1M --direct=1 --ioengine=libaio --iodepth=32 --runtime=30 --time_based
      fio --name=randread_512k_4x32 --filename=$f --size=32G --rw=randread --bs=512k --direct=1 --ioengine=libaio --iodepth=32 --numjobs=4 --group_reporting --runtime=30 --time_based
      rm -f $f" > $S/fio/fio$tag.txt 2>&1
    progress "fio $d (O_DIRECT, 32 GiB file): $(grep -E '^ *(READ|WRITE): bw=' $S/fio/fio$tag.txt | sed -E 's/^ *//; s/, io=.*//' | paste -sd';')"
  done
}
sec_idle() { progress "idle: $(wait_idle)"; }

mkdir -p $S
for sec in "$@"; do
  progress "section $sec started"
  case $sec in cached:*) sec_cached "${sec#cached:}";; cached2:*) sec_cached2 "${sec#cached2:}";;
    finish:*) sec_finish "${sec#finish:}";;
    lwwait:*) sec_lwwait "${sec#lwwait:}";; *) "sec_$sec";; esac
  progress "section $sec finished"
done
echo "##### PERF DONE $(date -u +%T) $(wait_idle)"
