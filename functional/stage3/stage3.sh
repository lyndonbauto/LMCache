#!/bin/bash
# Stage 3: the pipelined path end to end on Soft-RoCE (c9-bring-up.md stage 3,
# functional-test-plan.md 5.4-5.6). Llama-3.1-8B (and gpt-oss-120b for
# T-PIPE-11) with LMCache's Aerospike RDMA adapter against the kv-sink server
# (127.0.0.1:3100, RC on rxe0 GID 1), --use-layerwise --pipelined-fetch.
# Runs on the host; each session is a run_steps.sh session in lmc-c.
#
# Usage: stage3.sh [section ...]
# Sections (default: precheck cfg08 e2e04 e2e05 pipe05 pipe06 pipe12 pipe11 rdma06gpu idle):
#   precheck   refuse to run if 8000/6555/8080/$KVSINK_PORT are taken or the GPU is busy
#   cfg08      runbook stage 3: registration line (T-CFG-08) and one pure L2 hit
#              of a 4-chunk prompt, pipelined, equal to the baseline
#   e2e04      T-E2E-04: P-exact + P-ragged cold, LMCache restart, warm from L2.
#              Prompts within the cap must be `pipelined`; over it `not_deferred`
#   e2e05      T-E2E-05 pipelined half: one prompt at the cap, one a chunk over
#   pipe05     T-PIPE-05: one segment record deleted (meta kept): `fell_back`, correct
#   pipe06     T-PIPE-06 (+ T-PIPE-07 partial): kv-sink frozen 2.5 s right after
#              lookup, three times; then the window is leased again
#   pipe12     T-PIPE-12: stored under max_record_bytes 256 KiB, read under the
#              discovered 1 MiB, and the reverse
#   pipe11     T-PIPE-11: gpt-oss-120b pure L2 hit; rxe0 packet count of the
#              fetch against the full and the sliding-window-limited sizes
#   rdma06gpu  T-RDMA-06 GPU half: vLLM stores P-exact-00..16; the byte oracle
#              reads the 100 production records by RDMA and by plain get
#   idle       wait for the GPU to be idle and print USED_VRAM
#   dry        CPU-only checks: script syntax, every server config parses, kv-sink
#              start/warm/stop (no vLLM, no GPU)
# Environment:
#   CAPS        --pipelined-max-chunks values for e2e04/e2e05 (default "64 4":
#               D-12 decision, the default cap records D-12, 4 keeps every
#               fetch to one server command)
#   TREE_HOST / TREE_CTR  the LMCache tree (default /root/lmc-work/LMCache, /work/LMCache)
#   GPT_BASE    gpt-oss oracle: vLLM's own prefix cache, block size 16 (D-06),
#               client.py output covering P-exact (pipe11 refuses without it)
#   GPT_SERVER_FLAGS  extra LMCache flags for gpt-oss (default --separate-object-groups,
#               which T-PIPE-11 needs so sliding-window layers get their own group)
#   KVSINK_PORT the kv-sink server's service port (default 3100, the old
#               info-command server; 3700 for the batch-read server: source
#               functional/newstack/kvsink_bp_env.sh, which also sets the
#               container, binary, config and KVSINK_WARM=0 for kvsink_restart)
#   FAULT_POLICY  kv_load_failure_policy for pipe05/pipe06 (default fail: under
#               recompute a mid-forward layerwise failure hits D-17, vllm#49250,
#               so the recompute half is recorded as blocked by D-17)
# The other sections run under fail, so a bad load errors instead of
# recomputing silently.
# The kv-sink server is restarted (and warmed) before every group: it keeps
# data in memory only, and every LMCache kill -9 leaks a region (issue 9).
#   STAGE_DIR   results directory under functional/ (default stage3)
#   LW_S        layerwise flag of every session (default true)
#   STOP_GRACE  seconds run_steps.sh waits after SIGTERM before kill -9 on an
#               LMCache restart (default 5; a clean shutdown needs ~14 s, D-18)
set -u
S=/root/lmc-work/functional/${STAGE_DIR:-stage3}
W=/work/functional/${STAGE_DIR:-stage3}
TREE_HOST=${TREE_HOST:-/root/lmc-work/LMCache}
TREE_CTR=${TREE_CTR:-/work/LMCache}
H=$TREE_CTR/functional/harness
LLAMA=meta-llama/Llama-3.1-8B-Instruct
GPT=openai/gpt-oss-120b
CB=/work/functional/corpus/corpus_llama-3.1-8b-instruct.stage2b.json
CG=/work/functional/corpus/corpus_gpt-oss-120b.v2.json
BASE1=/work/functional/day1/step4/bi_run1.json
BASE2=/work/functional/stage2/base2b/bi_run1.json
GPT_BASE=${GPT_BASE:-}
GPT_SERVER_FLAGS=${GPT_SERVER_FLAGS:---separate-object-groups}
CAPS=${CAPS:-64 4}
KVSINK_PORT=${KVSINK_PORT:-3100}
FAULT_POLICY=${FAULT_POLICY:-fail}
# rxe0's rcvd_pkts counts about 1 KiB packets on the old server (a 128 MiB
# fetch: 133,120). The batch-read server and client negotiate the RC path MTU
# as the smaller port's active MTU, 4096 on rxe0 (kvsink_bp_env.sh sets it).
RXE_PKT=${RXE_PKT:-1024}
export PY314=/root/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14
# One 256-token chunk, all layers, K and V, bf16.
LLAMA_CHUNK=$((32 * 2 * 8 * 128 * 256 * 2))   # 32 MiB
GPT_CHUNK=$((36 * 2 * 8 * 64 * 256 * 2))      # 18 MiB
# shellcheck source=../harness/host_actions.sh
source "$TREE_HOST/functional/harness/host_actions.sh"

vram() { amd-smi metric --mem-usage 2>/dev/null | grep -m1 USED_VRAM | grep -oE '[0-9]+'; }
wait_idle() { for _ in $(seq 60); do u=$(vram); [ -n "$u" ] && [ "$u" -lt 4000 ] && break; sleep 3; done; echo "VRAM ${u} MB"; }
progress() { echo "$(date -u +%FT%TZ) $*" | tee -a $S/progress.log; }

# l2_json <cap> <chunk-bytes> [rdma-extra] [adapter-extra]: the adapter spec,
# no spaces (run_steps.sh word-splits the server flags). Two windows of
# <cap> chunks each (registration checks one window holds the cap).
l2_json() {
  local cap=$1 chunk=$2 rdma_extra=${3:-} adapter_extra=${4:-}
  echo "{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$KVSINK_PORT\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"${adapter_extra:+,$adapter_extra},\"rdma\":{\"transport\":\"RC\",\"device_name\":\"rxe0\",\"gid_index\":1,\"window_count\":2,\"window_bytes\":$((cap * chunk))${rdma_extra:+,$rdma_extra}}}"
}
# server_flags <cap> <chunk-bytes> [rdma-extra] [adapter-extra]
# RDMA windows live in a fixed L1 slab, so lazy L1 allocation must be off
# (it is the default whenever pinned memory is supported, i.e. on the GPU;
# the CPU dry run had it off implicitly).
server_flags() {
  echo "--no-l1-use-lazy --pipelined-fetch --pipelined-max-chunks $1 --l2-adapter $(l2_json "$@")"
}
# ids <set> <min-chunks> <max-chunks> [corpus]: comma-separated prompt ids.
ids() {
  docker exec lmc-c python -c "
import json, sys
d = json.load(open('${4:-$CB}'))
print(','.join(p['id'] for p in d['sets']['$1'] if $2 <= p['n_tokens'] // 256 <= $3))"
}

# group <dir> <name>: a fresh, warmed kv-sink server for the next sessions;
# its log and warm-up output go to <dir>/kvsink_<name>/.
KVDIR=""
group() {
  KVDIR=$S/$1/kvsink_$2
  local out
  out=$(kvsink_restart $KVDIR 2>&1) || { progress "group $1/$2: kv-sink did not start: $(echo "$out" | tail -n 3)"; exit 1; }
  progress "group $1/$2: $(echo "$out" | tail -n 1)"
}

# session <dir> <tag> <policy> <step>...: one run_steps.sh session.
# SERVER_FLAGS, SERVER_FLAGS_ALT, VLLM_EXTRA and CORPUS_S come from the caller;
# so do (Stage 6) SERVER2_FLAGS (host B's server, default SERVER_FLAGS),
# L2_PORT_S (the L2 port the steps' tools use, default 3100) and L1_GB_S /
# L1_GB2_S (L1 sizes, default 40).
session() {
  local dir=$1 tag=$2 policy=$3; shift 3
  mkdir -p $S/$dir
  echo "##### $tag $(date -u +%T) $(wait_idle) policy=$policy"
  watch_host $S/$dir & local watcher=$!
  timeout 7200 docker exec -e VLLM_BATCH_INVARIANT=1 -e LMCACHE_SERVER_EXTRA="$SERVER_FLAGS" \
    -e LMCACHE_SERVER_EXTRA_ALT="${SERVER_FLAGS_ALT:-}" -e L2_PORT=${L2_PORT_S:-$KVSINK_PORT} \
    -e LMCACHE_SERVER2_EXTRA="${SERVER2_FLAGS:-}" -e L1_SIZE_GB=${L1_GB_S:-40} \
    -e L1_SIZE_GB2=${L1_GB2_S:-${L1_GB_S:-40}} \
    -e CORPUS="${CORPUS_S:-$CB}" -e KV_LOAD_FAILURE_POLICY="$policy" \
    -e SEND_TIMEOUT=${SEND_TIMEOUT:-900} -e VLLM_EXTRA="${VLLM_EXTRA:-}" \
    -e STOP_GRACE=${STOP_GRACE:-5} lmc-c \
    bash $H/run_steps.sh $W/$dir $tag "${LW_S:-true}" "$@" > $S/$dir/session_$tag.txt 2>&1
  local rc=$?
  kill $watcher 2>/dev/null
  echo "rc=$rc"; grep -E "===|correct|exited|did not|FAILED|warning" $S/$dir/session_$tag.txt | tail -n 40
  grep -q "listen_check.*FAIL" $S/$dir/session_$tag.txt && progress "!! $tag: a listener off loopback (listen_check FAIL)"
  echo "-- error lines: $(grep -cE 'Traceback|ERROR|Error' $S/$dir/lmcache_$tag.log $S/$dir/vllm_${tag}_*.log \
    $(ls $S/$dir/lmcache2_$tag.log $S/$dir/vllm2_${tag}_*.log 2>/dev/null) | paste -sd' ')"
  echo "-- registration: $(grep -hoE '[^ ]+ fetches layer by layer from L2 adapter [0-9]+, reading records of at most [0-9]+ bytes|Cannot fetch [^ ]+ layer by layer|No pipelined sink is installed|serves world size 1 only' $S/$dir/lmcache_$tag.log | sort | uniq -c | sed 's/^ *//' | paste -sd';')"
  echo "-- outcomes: $(grep -oE 'pipelined_outcome=[a-z_]+' $S/$dir/lmcache_$tag.log | sort | uniq -c | sed 's/^ *//' | paste -sd' ')"
  echo "-- kv-sink: late completions $(grep -c 'late completion' $KVDIR/asd-kvsink.log 2>/dev/null), region error lines $(grep -c 'in error state' $KVDIR/asd-kvsink.log 2>/dev/null), \
failed writes/posts $(grep -cE 'kv-sink: region [0-9]+ (write|post) failed' $KVDIR/asd-kvsink.log 2>/dev/null), \
regions dropped $(grep -c 'dropped after a failed write' $KVDIR/asd-kvsink.log 2>/dev/null)"
  ls $S/$dir/HANG_* 2>/dev/null && echo "!! a send failed; stacks in $S/$dir/pystacks_*"
  return $rc
}
# report <dir> <tag> <name> [hit_report args] <send>...: per-request report
# against the Llama baselines (BASES overrides, CORPUS_S the corpus).
report() {
  local dir=$1 tag=$2 name=$3; shift 3
  local args=() runs=() b
  for a in "$@"; do
    case $a in --*) args+=("$a");; *) runs+=("$W/$dir/${tag}_$a.json");; esac
  done
  for b in ${BASES:-$BASE1 $BASE2}; do args+=("--baseline" "$b"); done
  docker exec lmc-c python $H/hit_report.py --corpus "${CORPUS_S:-$CB}" \
    --lmcache-log $W/$dir/lmcache_$tag.log --out $W/$dir/report_${tag}_$name.json "${args[@]}" "${runs[@]}" \
    > $S/$dir/report_${tag}_$name.md 2>&1
  progress "$tag $name: $(tail -n 1 $S/$dir/report_${tag}_$name.md | cut -c1-400)"
}
# rxe_delta <dir> <tag> <before> <after>: packets rxe0 received between snapshots.
rxe_delta() {
  local b a
  b=$(grep -oE 'rcvd_pkts [0-9]+' $S/$1/rxe_${2}_$3.txt | grep -oE '[0-9]+')
  a=$(grep -oE 'rcvd_pkts [0-9]+' $S/$1/rxe_${2}_$4.txt | grep -oE '[0-9]+')
  echo $((a - b))
}

sec_precheck() {
  local busy; busy=$(ss -ltn | grep -E "127.0.0.1:(8000|6555|8080|$KVSINK_PORT) ")
  [ -z "$busy" ] || { echo "ports in use (another session?):"; echo "$busy"; exit 1; }
  echo "ports 8000/6555/8080/$KVSINK_PORT free; $(wait_idle)"
}

sec_cfg08() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  tag=cfg08_c4
  group cfg08 $tag
  session cfg08 $tag fail server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=P-exact-10" settle restart \
    "rxe name=b" "send name=l2hit sets=P-exact ids=P-exact-10 stats=1" "rxe name=a"
  report cfg08 $tag all --outcomes=${tag}_l2hit=pipelined --require=${tag}_l2hit=pipelined store l2hit
  local line; line=$(grep -c "fetches layer by layer from L2 adapter 0" $S/cfg08/lmcache_$tag.log)
  progress "T-CFG-08: registration line seen $line time(s) (want >= 1, one per registration); \
rxe0 packets during the L2 hit: $(rxe_delta cfg08 $tag b a) (4 chunks = $((4 * LLAMA_CHUNK / RXE_PKT)) data packets); \
metrics: $(grep -E 'lmcache_mp_num_deferred_retrieves_total\{.*pipelined' $S/cfg08/metrics_$tag.txt | head -n 1)"
}

# e2e04_group <cap> <name> <P-exact ids> <P-ragged ids or "">
e2e04_group() {
  local cap=$1 name=$2 exact=$3 ragged=$4 tag sets ids elig over
  SERVER_FLAGS=$(server_flags "$cap" $LLAMA_CHUNK)
  tag=e2e04_c${cap}_$name
  sets=P-exact; ids=$exact
  [ -n "$ragged" ] && { sets=P-exact,P-ragged; ids=$exact,$ragged; }
  elig=$( { ids P-exact 1 "$cap"; ids P-ragged 1 "$cap"; } | paste -sd, | tr ',' '\n' | grep -xF -f <(echo "$ids" | tr ',' '\n') | paste -sd,)
  over=$( { ids P-exact $((cap + 1)) 999; ids P-ragged $((cap + 1)) 999; } | paste -sd, | tr ',' '\n' | grep -xF -f <(echo "$ids" | tr ',' '\n') | paste -sd,)
  group e2e04 $tag
  local steps=(server "vllm model=$LLAMA" "send name=cold sets=$sets ids=$ids" restart)
  [ -n "$elig" ] && steps+=("send name=warmelig sets=$sets ids=$elig stats=1")
  [ -n "$over" ] && steps+=("send name=warmover sets=$sets ids=$over stats=1")
  session e2e04 $tag fail "${steps[@]}"
  local sends=(cold) rargs=()
  [ -n "$elig" ] && { sends+=(warmelig); rargs+=("--outcomes=${tag}_warmelig=pipelined" "--require=${tag}_warmelig=pipelined"); }
  [ -n "$over" ] && sends+=(warmover)
  report e2e04 $tag all "${rargs[@]}" "${sends[@]}"
}
sec_e2e04() {
  local cap
  for cap in $CAPS; do
    e2e04_group "$cap" short "$(ids P-exact 1 4)" "$(ids P-ragged 1 999)"
    e2e04_group "$cap" long1 P-exact-15,P-exact-16,P-exact-17 ""
    e2e04_group "$cap" long2 P-exact-18,P-exact-19 ""
  done
}

sec_e2e05() {
  local cap at over sets tag
  for cap in $CAPS; do
    if [ "$cap" -ge 64 ]; then at=P-long-00; over=P-long-05; sets=P-long
    else at=$(ids P-exact "$cap" "$cap" | cut -d, -f1); over=$(ids P-multi $((cap + 1)) $((cap + 1)) | cut -d, -f1); sets=P-exact,P-multi
    fi
    SERVER_FLAGS=$(server_flags "$cap" $LLAMA_CHUNK)
    tag=e2e05_c$cap
    group e2e05 $tag
    session e2e05 $tag fail server "vllm model=$LLAMA" "send name=cold sets=$sets ids=$at,$over" restart \
      "send name=atcap sets=$sets ids=$at stats=1" "send name=overcap sets=$sets ids=$over stats=1"
    report e2e05 $tag all --outcomes=${tag}_atcap=pipelined --require=${tag}_atcap=pipelined cold atcap overcap
  done
}

sec_pipe05() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  tag=pipe05
  group pipe05 $tag
  session pipe05 $tag "$FAULT_POLICY" server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=P-exact-10" settle restart \
    "l2seg prompt=P-exact-10 chunk=1 seg=5" \
    "send name=probe sets=P-exact ids=P-exact-10 stats=1 errors=1" \
    "send name=again sets=P-exact ids=P-exact-10 errors=1" "vllm_check name=end"
  report pipe05 $tag all "--allow-error=${tag}_probe" "--allow-error=${tag}_again" \
    "--no-hit-check=${tag}_probe" "--no-hit-check=${tag}_again" \
    --outcomes=${tag}_probe=fell_back,failed --require=${tag}_probe=fell_back \
    --outcomes=${tag}_again=pipelined,fell_back,not_deferred store probe again
  progress "pipe05: $(grep -c 'Pipelined fetch failed on layer' $S/pipe05/lmcache_$tag.log) 'Pipelined fetch failed' warning(s)"
}

sec_pipe06() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  tag=pipe06
  group pipe06 $tag
  local ids5; ids5=$(ids P-exact 4 4)
  local steps=(server "vllm model=$LLAMA" "send name=store sets=P-exact ids=$ids5" settle restart)
  for i in 10 11 12; do
    steps+=("host action=freeze_kvsink secs=2.5 on=lookup_end"
            "send name=stall$i sets=P-exact ids=P-exact-$i errors=1" "sleep secs=4" "vllm_check name=stall$i"
            "vllm_ensure model=$LLAMA")
  done
  # An abandoned fetch quarantines its window for fetch_timeout_seconds (30 s
  # by default); after13/14 check that the windows are leased again after it.
  steps+=("sleep secs=${PIPE06_REUSE_WAIT:-32}"
          "send name=after13 sets=P-exact ids=P-exact-13" "send name=after14 sets=P-exact ids=P-exact-14"
          "vllm_check name=end")
  session pipe06 $tag "$FAULT_POLICY" "${steps[@]}"
  report pipe06 $tag all "--allow-error=*" "--no-hit-check=${tag}_stall10" \
    "--no-hit-check=${tag}_stall11" "--no-hit-check=${tag}_stall12" \
    --outcomes=${tag}_stall10=fell_back,failed,pipelined --outcomes=${tag}_stall11=fell_back,failed,pipelined \
    --outcomes=${tag}_stall12=fell_back,failed,pipelined \
    --outcomes=${tag}_after13=pipelined --require=${tag}_after13=pipelined \
    --outcomes=${tag}_after14=pipelined --require=${tag}_after14=pipelined \
    store stall10 stall11 stall12 after13 after14
  progress "pipe06: freezes: $(cat $S/pipe06/HOSTFAULT_* 2>/dev/null | paste -sd'|'); \
layer timeouts: $(grep -c 'will never arrive' $S/pipe06/lmcache_$tag.log); \
engine stops: $(grep -h generation_timeout_errors $S/pipe06/vllm_check_* | paste -sd' ')"
}

sec_pipe12() {
  group pipe12 small_then_discover
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK "" '"max_record_bytes":262144')
  SERVER_FLAGS_ALT=$(server_flags 4 $LLAMA_CHUNK)
  session pipe12 pipe12_small_then_discover fail server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=P-exact-10" settle "restart extra=alt" \
    "send name=read sets=P-exact ids=P-exact-10 stats=1"
  report pipe12 pipe12_small_then_discover all --outcomes=pipe12_small_then_discover_read=fell_back,not_deferred \
    --require=pipe12_small_then_discover_read=fell_back store read
  group pipe12 discover_then_small
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  SERVER_FLAGS_ALT=$(server_flags 4 $LLAMA_CHUNK "" '"max_record_bytes":262144')
  session pipe12 pipe12_discover_then_small fail server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=P-exact-11" settle "restart extra=alt" \
    "send name=read sets=P-exact ids=P-exact-11 stats=1"
  report pipe12 pipe12_discover_then_small all --outcomes=pipe12_discover_then_small_read=fell_back,not_deferred \
    --require=pipe12_discover_then_small_read=fell_back store read
  SERVER_FLAGS_ALT=""
}

sec_pipe11() {
  [ -n "$GPT_BASE" ] || { echo "pipe11: set GPT_BASE to the gpt-oss prefix-cache oracle (D-06)"; return 1; }
  SERVER_FLAGS="$GPT_SERVER_FLAGS $(server_flags 4 $GPT_CHUNK)"
  tag=pipe11
  group pipe11 $tag
  CORPUS_S=$CG VLLM_EXTRA="${GPT_VLLM_EXTRA:-}" session pipe11 $tag fail server "vllm model=$GPT" \
    "send name=store sets=P-exact ids=P-exact-10" settle restart \
    "rxe name=b" "send name=l2hit sets=P-exact ids=P-exact-10 stats=1" "rxe name=a"
  CORPUS_S=$CG BASES=$GPT_BASE report pipe11 $tag all --outcomes=${tag}_l2hit=pipelined \
    --require=${tag}_l2hit=pipelined store l2hit
  local half=$((GPT_CHUNK / 2 / RXE_PKT))   # packets per chunk for half the layers
  progress "pipe11: $(grep -hoE 'Per-layer staging matches the pipelined fetch plan for all [0-9]+ layers' $S/pipe11/lmcache_$tag.log | head -n 1); \
rxe0 packets during the L2 hit: $(rxe_delta pipe11 $tag b a); full 4-chunk fetch $((4 * GPT_CHUNK / RXE_PKT)), \
sliding-window layers limited to 1 or 2 chunks: $((4 * half + half)) or $((4 * half + 2 * half)) (data packets, plus acks and smaller writes)"
}

sec_rdma06gpu() {
  SERVER_FLAGS=$(server_flags 4 $LLAMA_CHUNK)
  group rdma06gpu store
  local ids17; ids17=$(docker exec lmc-c python -c "print(','.join(f'P-exact-{i:02d}' for i in range(17)))")
  session rdma06gpu rdma06gpu fail server "vllm model=$LLAMA" "send name=store sets=P-exact ids=$ids17" settle
  local snap; snap=$(docker exec -e MODEL_NAME=$LLAMA -e HF_HOME=/work/hf lmc-c \
    python $H/l2_segments.py --port $KVSINK_PORT --corpus $CB model prompt=P-exact-00)
  progress "rdma06gpu: keys stored under model name '$snap'"
  docker exec -w $TREE_CTR lmc-c env HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= \
    PYTHONDONTWRITEBYTECODE=1 RUN_AEROSPIKE_INTEGRATION=1 \
    AEROSPIKE_TEST_HOST=127.0.0.1 AEROSPIKE_TEST_PORT=$KVSINK_PORT AEROSPIKE_TEST_NAMESPACE=lmcache \
    RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 RDMA_ORACLE_CORPUS=$CB RDMA_ORACLE_MODEL_NAME="$snap" \
    RDMA_ORACLE_STORED_SET=kv_chunks \
    timeout 1500 python -m pytest -p no:cacheprovider -v -rfEs \
    --junitxml=$W/rdma06gpu/rdma06gpu.junit.xml -k stored_by_vllm \
    tests/v1/distributed/test_aerospike_rdma_byte_oracle_integration.py > $S/rdma06gpu/rdma06gpu.txt 2>&1
  progress "T-RDMA-06 GPU half: $(tail -n 1 $S/rdma06gpu/rdma06gpu.txt)"
}

sec_idle() { echo "##### idle: $(wait_idle)"; }

sec_dry() {
  local f rc=0
  for f in $TREE_HOST/functional/harness/run_steps.sh $TREE_HOST/functional/harness/host_actions.sh \
      $TREE_HOST/functional/stage3/stage3.sh; do
    bash -n "$f" && echo "syntax ok: $f" || rc=1
  done
  for cfg in "$(l2_json 64 $LLAMA_CHUNK)" "$(l2_json 4 $LLAMA_CHUNK)" "$(l2_json 4 $GPT_CHUNK)" \
      "$(l2_json 4 $LLAMA_CHUNK "" '"max_record_bytes":262144')"; do
    docker exec lmc-c python -c "
import json, sys
from lmcache.v1.distributed.l2_adapters.aerospike_l2_adapter import AerospikeL2AdapterConfig
c = AerospikeL2AdapterConfig.from_dict(json.loads(sys.argv[1]))
print('adapter config ok:', c.hosts, c.max_record_bytes, c.rdma.transport.name, c.rdma.device_name,
      c.rdma.gid_index, c.rdma.window_plan.window_count, c.rdma.window_plan.window_bytes)" "$cfg" || rc=1
  done
  echo "e2e04 groups: short=$(ids P-exact 1 4),$(ids P-ragged 1 999)"
  echo "cap-4 eligible: $(ids P-exact 1 4),$(ids P-ragged 1 4); over: $(ids P-ragged 5 999)"
  echo "e2e05 cap 4: at=$(ids P-exact 4 4 | cut -d, -f1) over=$(ids P-multi 5 5 | cut -d, -f1)"
  bash $TREE_HOST/functional/stage3/dryrun_server.sh || rc=1
  return $rc
}

# Sourced (by stage5.sh) for the helpers above: stop here.
(return 0 2> /dev/null) && return 0
mkdir -p $S
# shellcheck disable=SC2048
for sec in ${*:-precheck cfg08 e2e04 e2e05 pipe05 pipe06 pipe12 pipe11 rdma06gpu idle}; do
  progress "section $sec started"
  sec_$sec
  progress "section $sec finished"
done
echo "##### STAGE 3 DONE $(date -u +%T) $(wait_idle)"
