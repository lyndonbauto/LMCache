#!/bin/bash
# D-17 narrowing (S1: wrong output after a failed layerwise load under
# recompute). Every row repeats T-PIPE-05 on P-exact-10 (store, settle,
# restart, delete segment 5 of chunk 1, probe, again) and changes one factor.
# Results go to stage3/d17/<row>/; see D17-NARROWING.md.
# Usage: d17.sh <row>...   (host; same requirements as stage3.sh)
# Rows:
#   repro     layerwise on, pipelined on, recompute (Stage 3's pipe05)
#   v1runner  repro with vLLM's V1 model runner (VLLM_USE_V2_MODEL_RUNNER=0)
#   nopipe    layerwise on, --pipelined-fetch off (whole-object load fails)
#   nolw      layerwise off, --pipelined-fetch off (async load fails)
#   nolw_v1   nolw with the V1 model runner
#   apc       repro with vLLM prefix caching on (the harness default is off)
#   failpol   repro under kv_load_failure_policy fail
#   nopipe_v1 nopipe with the V1 model runner
#   pipe06_v1 Stage 3's pipe06 (kv-sink frozen 2.5 s, 3 times) with the V1 runner
#   noasync_v1 repro with the V1 runner and async scheduling off
#   fix_noasync  repro, V2 runner, async off, with a scratch vLLM carrying only
#              part 1 of vllm#49250 (d17_mkfix.py; PYTHONPATH, installed vLLM untouched)
#   fix_noasync_r2  a repeat of fix_noasync
#   fix_async  as fix_noasync with async scheduling on (part 2 of vllm#49250 not applied)
#   pipe06_v1na pipe06 with the V1 runner and async scheduling off
set -u
# shellcheck source=stage3.sh
source "${TREE_HOST:-/root/lmc-work/LMCache}/functional/stage3/stage3.sh"

# session_x <dir> <tag> <policy> <layerwise> <step>...: stage3.sh's session
# with the layerwise flag and EXTRA_ENV (docker -e words) chosen by the row.
session_x() {
  local dir=$1 tag=$2 policy=$3 lw=$4; shift 4
  mkdir -p $S/$dir
  echo "##### $tag $(date -u +%T) $(wait_idle) policy=$policy layerwise=$lw env=${EXTRA_ENV:-}"
  watch_host $S/$dir & local watcher=$!
  # shellcheck disable=SC2086
  timeout 7200 docker exec -e VLLM_BATCH_INVARIANT=1 ${EXTRA_ENV:-} -e LMCACHE_SERVER_EXTRA="$SERVER_FLAGS" \
    -e LMCACHE_SERVER_EXTRA_ALT="${SERVER_FLAGS_ALT:-}" -e L2_PORT=3100 \
    -e CORPUS="${CORPUS_S:-$CB}" -e KV_LOAD_FAILURE_POLICY="$policy" \
    -e SEND_TIMEOUT=${SEND_TIMEOUT:-900} -e VLLM_EXTRA="${VLLM_EXTRA:-}" lmc-c \
    bash $H/run_steps.sh $W/$dir $tag "$lw" "$@" > $S/$dir/session_$tag.txt 2>&1
  local rc=$?
  kill $watcher 2>/dev/null
  echo "rc=$rc"; grep -E "===|correct|exited|did not|FAILED|warning" $S/$dir/session_$tag.txt | tail -n 30
  return $rc
}
plain_flags() {
  echo "--l2-adapter {\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:3100\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"}"
}
# summary <dir> <tag>: the evidence lines for D17-NARROWING.md.
summary() {
  local d=$S/$1 tag=$2 v
  v=$(ls $d/vllm_${tag}_*.log 2>/dev/null | head -n 1)
  {
    echo "row=$tag"
    echo "model_runner: $(grep -ohE 'Using V[12] Model Runner|using the V1 model runner[^;]*' $v | sort -u | paste -sd';')"
    echo "async_scheduling: $(grep -oE 'async_scheduling=[A-Za-z]+' $v | sort -u | paste -sd' ')"
    echo "prefix_caching: $(grep -oE 'enable_prefix_caching=[A-Za-z]+' $v | sort -u | paste -sd' ')"
    echo "outcomes: $(grep -oE 'pipelined_outcome=[a-z_]+' $d/lmcache_$tag.log | sort | uniq -c | sed 's/^ *//' | paste -sd' ')"
    echo "pipelined fetch failed: $(grep -c 'Pipelined fetch failed on layer' $d/lmcache_$tag.log)"
    echo "connector reported: $(grep -ohE "Layerwise KV load failed at layer '[^']*'.*reporting [0-9]+ blocks" $v | sort | uniq -c | sed 's/^ *//' | paste -sd'|')"
    echo "vllm recovered: $(grep -ohE 'Recovered from KV load failure: [^.]*\.' $v | sort | uniq -c | sed 's/^ *//' | paste -sd'|')"
    echo "vllm failed reqs: $(grep -c 'Failing [0-9]* request(s) due to KV load failure' $v)"
    echo "bypass after failed async load: $(grep -c 'Bypassing LMCache for request' $v)"
    echo "retrieve errors: $(grep -ohE 'MP retrieve end:[^|]*' $d/lmcache_$tag.log | grep -oE 'retrieved_count=[0-9]+|status=[a-z_]+' | sort | uniq -c | sed 's/^ *//' | paste -sd' ')"
    echo "report: $(tail -n 1 $d/report_${tag}_all.md | cut -c1-500)"
  } > $d/summary_$tag.txt
  progress "d17 $tag: $(paste -sd' ' $d/summary_$tag.txt | cut -c1-700)"
}
PIPE05_STEPS=(server "vllm model=$LLAMA" "send name=store sets=P-exact ids=P-exact-10" settle restart
  "l2seg prompt=P-exact-10 chunk=1 seg=5"
  "send name=probe sets=P-exact ids=P-exact-10 stats=1 errors=1" "send name=again sets=P-exact ids=P-exact-10 errors=1")
# pipe05_row <row> <policy> <layerwise>; SERVER_FLAGS, EXTRA_ENV, VLLM_EXTRA from the caller.
pipe05_row() {
  local row=$1 policy=$2 lw=$3 dir=d17/$1 tag=d17_$1
  group $dir $tag
  session_x $dir $tag "$policy" "$lw" "${PIPE05_STEPS[@]}"
  report $dir $tag all "--allow-error=*" "--no-hit-check=${tag}_probe" "--no-hit-check=${tag}_again" store probe again
  summary $dir $tag
}

row_repro()     { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV=""; VLLM_EXTRA=""; pipe05_row repro recompute true; }
row_v1runner()  { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV="-e VLLM_USE_V2_MODEL_RUNNER=0"; VLLM_EXTRA=""; pipe05_row v1runner recompute true; }
row_noasync_v1() { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV="-e VLLM_USE_V2_MODEL_RUNNER=0"; VLLM_EXTRA="--no-async-scheduling"; pipe05_row noasync_v1 recompute true; }
row_nopipe()    { SERVER_FLAGS=$(plain_flags); EXTRA_ENV=""; VLLM_EXTRA=""; pipe05_row nopipe recompute true; }
row_nopipe_v1() { SERVER_FLAGS=$(plain_flags); EXTRA_ENV="-e VLLM_USE_V2_MODEL_RUNNER=0"; VLLM_EXTRA=""; pipe05_row nopipe_v1 recompute true; }
row_nolw()      { SERVER_FLAGS=$(plain_flags); EXTRA_ENV=""; VLLM_EXTRA=""; pipe05_row nolw recompute false; }
row_nolw_v1()   { SERVER_FLAGS=$(plain_flags); EXTRA_ENV="-e VLLM_USE_V2_MODEL_RUNNER=0"; VLLM_EXTRA=""; pipe05_row nolw_v1 recompute false; }
FIX_ENV="-e PYTHONPATH=/work/scratch/vllm-d17fix"
row_fix_noasync() { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV=$FIX_ENV; VLLM_EXTRA="--no-async-scheduling"; pipe05_row fix_noasync recompute true; }
row_fix_noasync_r2() { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV=$FIX_ENV; VLLM_EXTRA="--no-async-scheduling"; pipe05_row fix_noasync_r2 recompute true; }
row_fix_async() { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV=$FIX_ENV; VLLM_EXTRA=""; pipe05_row fix_async recompute true; }
row_pipe06_v1na() { PIPE06_ENV="-e VLLM_USE_V2_MODEL_RUNNER=0" PIPE06_VLLM="--no-async-scheduling" pipe06_row pipe06_v1na; }
row_pipe06_fixna() { PIPE06_ENV=$FIX_ENV PIPE06_VLLM="--no-async-scheduling" pipe06_row pipe06_fixna; }
row_apc()       { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV=""; VLLM_EXTRA="--enable-prefix-caching"; pipe05_row apc recompute true; }
row_failpol()   { SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV=""; VLLM_EXTRA=""; pipe05_row failpol fail true; }
row_pipe06_v1() { PIPE06_ENV="-e VLLM_USE_V2_MODEL_RUNNER=0" PIPE06_VLLM="" pipe06_row pipe06_v1; }
# pipe06_row <row>: Stage 3's pipe06 under PIPE06_ENV / PIPE06_VLLM.
pipe06_row() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK); EXTRA_ENV=$PIPE06_ENV; VLLM_EXTRA=$PIPE06_VLLM
  local dir=d17/$1 tag=d17_$1 ids5 i
  group $dir $tag
  ids5=$(ids P-exact 4 4)
  local steps=(server "vllm model=$LLAMA" "send name=store sets=P-exact ids=$ids5" settle restart)
  for i in 10 11 12; do
    steps+=("host action=freeze_kvsink secs=2.5 on=lookup_end"
            "send name=stall$i sets=P-exact ids=P-exact-$i errors=1" "sleep secs=4" "vllm_check name=stall$i"
            "vllm_ensure model=$LLAMA")
  done
  steps+=("send name=after13 sets=P-exact ids=P-exact-13" "send name=after14 sets=P-exact ids=P-exact-14")
  session_x $dir $tag recompute true "${steps[@]}"
  report $dir $tag all "--allow-error=*" "--no-hit-check=*" store stall10 stall11 stall12 after13 after14
  summary $dir $tag
}

mkdir -p $S/d17
for r in "$@"; do
  progress "d17 row $r started"
  row_$r
  progress "d17 row $r finished"
done
KVSINK_CONF=$TREE_HOST/functional/configs/aerospike-kvsink.conf KVSINK_LOG=$KVDIR/asd-kvsink.log \
  bash "$TREE_HOST/functional/harness/kvsink_server.sh" stop
echo "##### d17 DONE $(date -u +%T) $(wait_idle)"
