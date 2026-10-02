#!/bin/bash
# Stage 6, GPU half: E4 end to end (functional-test-plan.md 5.6-5.9).
# Llama-3.1-8B (and Llama-3.3-70B for T-E2E-11), LMCache MP server + vLLM
# 0.27.1 with --use-layerwise, VLLM_BATCH_INVARIANT=1, on the Stage 3 setup.
# Runs on the host; each session is a run_steps.sh session in lmc-c, built
# from stage3.sh's helpers (session, report, group, ids, l2_json,
# server_flags). Run it after Stages 4-5, on a tree with the D-14 fix
# (e9cd0689) rebuilt.
#
# "Host B" (E4's second LMCache host, plan section 3) is a second LMCache
# server on 127.0.0.1:6556 (HTTP 8081) and a second vLLM on 8001 attached to
# it, on the same MI300X (run_steps.sh server2 / vllm2 ... lmc=2). Both hosts
# share one L2:
#   kvsink   the single-node batch-read kv-sink server (aero-kvsink-bp,
#            127.0.0.1:$KVSINK_PORT = 3700, RC on rxe0 GID 1; launch.sh sources
#            functional/newstack/kvsink_bp_env.sh), pipelined fetches on at
#            cap S6_CAP (default 64, LMCache's default; D-12 does not reproduce
#            on this server). Multi-node pipelined plans are refused (N1), so
#            this is the only pipelined L2.
#   cluster  the 3-node CE 8.2 cluster from functional/harness/cluster.sh
#            (127.0.0.1:3300/3310/3320), plain path (no RDMA).
# Two RDMA windows of cap chunks are reserved at the start of the L1 slab
# (2 x 2 GiB at cap 64), so the L1 sizes below add that reservation.
#
# Usage: stage6gpu.sh [section ...]
# Sections (default: precheck shr01 shr01c shr02 shr02c shr03 evt06 flt02
#   flt03 flt04 flt04k evt04 evt04l2 idle; e2e11 and flt08 run only when named):
#   precheck  refuse to run if 8000/8001/6555/6556/8080/8081/$KVSINK_PORT/
#             3300-3320 are taken or the GPU is busy
#   shr01     T-SHR-01 on kvsink: host A stores P-exact-10..14 (4 chunks) and
#             P-shared; host B (empty L1) sends them: every output equal, B's
#             hits are the full prefix from L2, 4-chunk ones `pipelined`
#   shr01c    T-SHR-01 on the cluster (RF 1), plain path
#   shr02     T-SHR-02 on kvsink: both hosts send P-exact-00..14 at once
#             (SHR02_ROUNDS rounds, L2 truncated between rounds), then each
#             reads them back from L2; l2_integrity.py after every round:
#             every object whole and from one writer, no orphan records (D-14)
#   shr02c    T-SHR-02 on the cluster (RF SHR02_RF, default 2), plain path
#   shr03     T-SHR-03 on kvsink: B fetches A's entries while A's LMCache
#             server is SIGKILLed and started again; B unaffected
#   evt06     T-EVT-06 on the cluster (RF 2), client LRU off on both hosts:
#             A stores P-shared + P-exact-00..09, B P-shared + P-ragged-00..09;
#             nothing is missing afterwards, and both re-read everything from L2
#   evt06c    contrast (record only): host A's L2 LRU on at 0.5 GB
#   flt02     T-FLT-02, cluster RF 1: SIGKILL n3 at the start of a 64-chunk
#             L2 prefetch; recompute; engine alive; later requests succeed
#   flt03     T-FLT-03, cluster RF 2: SIGKILL n2 the same way; after the
#             cluster settles every hit is served (no recompute)
#   flt04     T-FLT-04 CE half, cluster RF 1: n2 restarted (graceful) during
#             a prefetch; afterwards every entry is back
#   flt04k    T-FLT-04 kv-sink half (single node): the kv-sink server restarts
#             under a running LMCache (data and registration gone); a later L2
#             read with the stale registration must fall back and be correct;
#             after an LMCache restart (re-registration) it is `pipelined` again
#   evt04     T-EVT-04, L1 pressure: small L1 (EVT04_L1_GB, default 3, plus
#             the RDMA windows), one server with --max-gpu-workers 2; vLLM 1
#             reads 4-chunk L2 hits (pipelined) while vLLM 2 stores 64-chunk
#             prompts, 3 rounds
#   evt04l2   T-EVT-04, L2 records gone mid-fetch: every segment of chunk 3 of
#             the prompt being fetched is deleted at `MP retrieve start`
#   e2e11     T-E2E-11: Llama-3.3-70B, TP=1, util UTIL70 (0.9): base70 (the
#             batch-invariant no-cache baseline) if missing, then T-E2E-04 at
#             CAP70 (default S6_CAP = 64; D-12 does not reproduce on this
#             server) and T-E2E-06 on a 64G kv-sink namespace
#             (aerospike-kvsink-bp-70b.conf), L1 L70_L1_GB (60) + windows;
#             record layout via l2_sizes.py
#   e2e11p    T-E2E-03/06 for the 70B on the plain path (aerospike-ce 3000)
#   e2e11c    T-E2E-06 for the 70B on the CE cluster (plain path, E4 records)
#   flt08     T-FLT-08 (needs FLT08_APPROVED=1: a host change, see
#             flt08_netem.sh): 5% loss on RoCE packets for 60 s during L2 reads
#   idle      wait for the GPU to be idle and print USED_VRAM
#   dry       CPU-only checks (no GPU, no default ports): see scripts/dry_s6gpu.sh
# T-FLT-05 (LMCache server killed mid-retrieve, recompute and fail) is
# stage5.sh flt05; it needs no second host and is not repeated here.
# Environment: TREE_HOST / TREE_CTR, TWO_VLLM_UTIL (0.3), SHR02_ROUNDS (3),
#   SHR02_RF (2), EVT04_L1_GB (3), S6_CAP (64), CAP70 (S6_CAP), UTIL70 (0.9),
#   L70_L1_GB (60), KVSINK_CONF_70B,
#   BASE70 (70B baseline JSON; default $W/base70/bi70.json), FLT08_MODE
#   (rdma), STOP_GRACE (60, D-18: both servers shut down cleanly),
#   REAP_WAIT (130, D-23: wait before a new vLLM on a server whose engine died).
# Policies: the cluster fault tests (flt02/03/04) run under
# kv_load_failure_policy recompute (plan section 7): the node dies during the
# lookup/prefetch, before the forward pass. evt04, evt04l2 and flt04k can fail
# a pipelined load mid-forward, where recompute gives wrong tokens (D-17,
# vllm#49250), so they run under fail and their recompute half is blocked by
# D-17. The rest run under fail.
set -u
TREE_HOST=${TREE_HOST:-/root/lmc-work/LMCache}
# shellcheck source=../stage3/stage3.sh
source "$TREE_HOST/functional/stage3/stage3.sh"
S=/root/lmc-work/functional/stage6/gpu
W=/work/functional/stage6/gpu
CL=$TREE_HOST/functional/harness/cluster.sh
export STOP_GRACE=${STOP_GRACE:-60}
REAP_WAIT=${REAP_WAIT:-130}
S6_CAP=${S6_CAP:-64}
# GB of L1 the two RDMA windows take at S6_CAP (rounded up).
WIN_GB=$(( (2 * S6_CAP * LLAMA_CHUNK + (1 << 30) - 1) >> 30 ))
TWO_VLLM_UTIL=${TWO_VLLM_UTIL:-0.3}
SHR02_ROUNDS=${SHR02_ROUNDS:-3}
SHR02_RF=${SHR02_RF:-2}
EVT04_L1_GB=${EVT04_L1_GB:-3}
FLT04K_L1_GB=${FLT04K_L1_GB:-4}
LLAMA70=meta-llama/Llama-3.3-70B-Instruct
# One 256-token 70B chunk: 80 layers, K and V, 8 KV heads x 128, bf16.
L70_CHUNK=$((80 * 2 * 8 * 128 * 256 * 2))   # 80 MiB
# D-12 does not reproduce on the batch-read server: LMCache's default cap.
CAP70=${CAP70:-$S6_CAP}
UTIL70=${UTIL70:-0.9}
BASE70=${BASE70:-$W/base70/bi70.json}
# General L1 for the 70B (GB): the cold send stores 552 chunks (43.1 GiB) and
# must stay under the 0.8 eviction watermark, or refused L1 batches never
# reach L2 (D-25). The two RDMA windows of CAP70 chunks come on top.
L70_L1_GB=${L70_L1_GB:-60}
WIN70_GB=$(( (2 * CAP70 * L70_CHUNK + (1 << 30) - 1) >> 30 ))
KVSINK_CONF_70B=${KVSINK_CONF_70B:-$TREE_HOST/functional/configs/aerospike-kvsink-bp-70b.conf}
CE_L2_70='--l2-adapter {"type":"aerospike","hosts":"127.0.0.1:3000","namespace":"lmcache","set_name":"kv_chunks"}'
CLUSTER_HOSTS=127.0.0.1:3300,127.0.0.1:3310,127.0.0.1:3320
P4=P-exact-10,P-exact-11,P-exact-12,P-exact-13,P-exact-14
RACE_IDS=$(for i in $(seq 0 14); do printf 'P-exact-%02d,' "$i"; done | sed 's/,$//')
TWO_HOSTS=(server server2 "vllm model=$LLAMA" "vllm2 model=$LLAMA lmc=2")

# l2_cluster_json [adapter-extra]: the plain adapter on the E4 cluster.
l2_cluster_json() {
  echo "{\"type\":\"aerospike\",\"hosts\":\"$CLUSTER_HOSTS\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"${1:+,$1}}"
}
# cgroup <dir> <name> <rf>: a wiped, freshly started 3-node CE cluster.
cgroup() {
  local dir=$S/$1/cluster_$2 rf=$3
  mkdir -p "$dir"; KVDIR=$dir
  { bash "$CL" stop; bash "$CL" wipe; bash "$CL" start "$rf"; bash "$CL" status; } > "$dir/cluster.txt" 2>&1 \
    || { progress "group $1/$2: cluster did not start: $(tail -n 3 "$dir/cluster.txt" | paste -sd' ')"; exit 1; }
  progress "group $1/$2: $(grep -m1 'cluster size' "$dir/cluster.txt") rf=$rf"
}
# use_l2 <kvsink|cluster> <dir> <name> [rf]: fresh L2 for the next session;
# sets SERVER_FLAGS (cap S6_CAP pipelined on kvsink, plain on the cluster),
# L2_PORT_S and PIPE (1 when retrieves can be pipelined).
use_l2() {
  if [ "$1" = kvsink ]; then
    group "$2" "$3"; L2_PORT_S=$KVSINK_PORT; PIPE=1; SERVER_FLAGS=$(server_flags "$S6_CAP" $LLAMA_CHUNK)
  else
    cgroup "$2" "$3" "${4:-1}"; L2_PORT_S=3300; PIPE=0; SERVER_FLAGS="--l2-adapter $(l2_cluster_json)"
  fi
}
# report_both <dir> <tag> <name> [hit_report args] <send>...: report() over
# both hosts' sends, with both LMCache logs (outcomes are found by request id).
report_both() {
  local dir=$1 tag=$2
  cat "$S/$dir/lmcache_$tag.log" "$S/$dir/lmcache2_$tag.log" > "$S/$dir/lmcache_both_$tag.log" 2>/dev/null
  local args=() runs=() b a
  shift 3
  for a in "$@"; do
    case $a in --*) args+=("$a");; *) runs+=("$W/$dir/${tag}_$a.json");; esac
  done
  for b in ${BASES:-$BASE1 $BASE2}; do args+=("--baseline" "$b"); done
  docker exec lmc-c python $H/hit_report.py --corpus "${CORPUS_S:-$CB}" \
    --lmcache-log $W/$dir/lmcache_both_$tag.log --out $W/$dir/report_${tag}_all.json "${args[@]}" "${runs[@]}" \
    > $S/$dir/report_${tag}_all.md 2>&1
  progress "$tag all: $(tail -n 1 $S/$dir/report_${tag}_all.md | cut -c1-400)"
}
# l2diff <dir> <run>: L2 traffic counters that moved during one stats=1 send.
l2diff() {
  docker exec lmc-c python $H/l2_stats.py diff $W/$1/l2stats_$2_before.json $W/$1/l2stats_$2_after.json 2>&1 | cut -c1-300
}
# integrity_lines <dir> <tag>: the verdict line of every integrity step.
integrity_lines() {
  local f
  for f in $S/$1/integrity_$2_*.txt; do [ -f "$f" ] && echo "$(basename "$f" .txt): $(tail -n 1 "$f")"; done
}
lmc2_errors() { echo "host B error lines: $(grep -cE 'Traceback|ERROR' $S/$1/lmcache2_$2.log 2>/dev/null) (LMCache) $(grep -chE 'Traceback|ERROR' $S/$1/vllm2_$2_*.log 2>/dev/null | paste -sd+) (vLLM)"; }

sec_precheck() {
  local busy; busy=$(ss -ltn | grep -E "127.0.0.1:(8000|8001|6555|6556|8080|8081|$KVSINK_PORT|3300|3310|3320) ")
  [ -z "$busy" ] || { echo "ports in use (another session?):"; echo "$busy"; exit 1; }
  echo "ports 8000/8001/6555/6556/8080/8081/$KVSINK_PORT/3300-3320 free; KVSINK_CTR=$KVSINK_CTR cap=$S6_CAP \
windows=${WIN_GB} GB STOP_GRACE=$STOP_GRACE; $(wait_idle)"
}

# shr01_on <kvsink|cluster>
shr01_on() {
  local l2=$1 tag=shr01_$1 rargs=()
  use_l2 "$l2" shr01 "$tag" 1
  # P-shared prompts are 8 chunks: pipelined at S6_CAP 64 (not_deferred at 4).
  [ "$PIPE" = 1 ] && rargs=("--outcomes=${tag}_b_hit4=pipelined" "--require=${tag}_b_hit4=pipelined"
    "--outcomes=${tag}_a_shared=pipelined,not_deferred" "--outcomes=${tag}_b_hitsh=pipelined,not_deferred")
  [ "$PIPE" = 1 ] && [ "$S6_CAP" -ge 8 ] && rargs+=("--require=${tag}_b_hitsh=pipelined")
  VLLM_EXTRA="--gpu-memory-utilization $TWO_VLLM_UTIL" L1_GB_S=20 session shr01 $tag fail "${TWO_HOSTS[@]}" \
    "send name=a_store sets=P-exact,P-shared ids=$P4" "send name=a_shared sets=P-shared" settle \
    "send name=b_hit4 sets=P-exact ids=$P4 port=8001 stats=1" \
    "send name=b_hitsh sets=P-shared port=8001 stats=1"
  report_both shr01 $tag all "${rargs[@]}" a_store a_shared b_hit4 b_hitsh
  progress "shr01 $l2: host B L2 traffic during b_hit4: $(l2diff shr01 ${tag}_b_hit4); b_hitsh: $(l2diff shr01 ${tag}_b_hitsh); \
host B LMCache L1 had none of these chunks (fresh server, never stored them); $(lmc2_errors shr01 $tag)"
}
sec_shr01() { shr01_on kvsink; }
sec_shr01c() { shr01_on cluster; }

# shr02_on <kvsink|cluster> <rf>
shr02_on() {
  local l2=$1 tag=shr02_$1 r steps=() sends=() rargs=()
  use_l2 "$l2" shr02 "$tag" "$2"
  steps=("${TWO_HOSTS[@]}")
  for r in $(seq 1 "$SHR02_ROUNDS"); do
    steps+=("l2stats name=before$r"
      "send name=a_race$r sets=P-exact ids=$RACE_IDS bg=1"
      "send name=b_race$r sets=P-exact ids=$RACE_IDS bg=1 port=8001" wait_bg settle
      "l2stats name=after$r" "integrity name=round$r ids=$RACE_IDS")
    sends+=("a_race$r" "b_race$r")
    rargs+=("--no-hit-check=${tag}_a_race$r" "--no-hit-check=${tag}_b_race$r"
            "--outcomes=${tag}_a_race$r=pipelined,not_deferred,fell_back"
            "--outcomes=${tag}_b_race$r=pipelined,not_deferred,fell_back")
    # Fresh keys for the next round: empty L2, and both L1s.
    [ "$r" -lt "$SHR02_ROUNDS" ] && steps+=(reset restart2)
  done
  steps+=(restart restart2 "send name=a_l2 sets=P-exact ids=$RACE_IDS stats=1"
    "send name=b_l2 sets=P-exact ids=$RACE_IDS port=8001 stats=1")
  sends+=(a_l2 b_l2)
  # The read-back is checked for hits: after reset, hit_report's model only
  # knows the last round's stores, which are exactly what L2 holds.
  [ "$PIPE" = 1 ] && rargs+=("--outcomes=${tag}_a_l2=pipelined" "--require=${tag}_a_l2=pipelined"
                             "--outcomes=${tag}_b_l2=pipelined" "--require=${tag}_b_l2=pipelined")
  VLLM_EXTRA="--gpu-memory-utilization $TWO_VLLM_UTIL" L1_GB_S=20 session shr02 $tag fail "${steps[@]}"
  report_both shr02 $tag all "${rargs[@]}" "${sends[@]}"
  local werr=""
  for r in $(seq 1 "$SHR02_ROUNDS"); do
    werr="$werr round$r: $(docker exec lmc-c python $H/l2_stats.py diff $W/shr02/l2stats_${tag}_before$r.json \
      $W/shr02/l2stats_${tag}_after$r.json 2>/dev/null | grep -oE '"client_write_error": [0-9]+' || echo 'no write errors')"
  done
  progress "shr02 $l2: $(integrity_lines shr02 $tag | paste -sd';'); create-only meta conflicts (client_write_error,\
 a lost race each):$werr; $(lmc2_errors shr02 $tag)"
}
sec_shr02() { shr02_on kvsink 1; }
sec_shr02c() { shr02_on cluster "$SHR02_RF"; }

sec_shr03() {
  local tag=shr03 ids="$P4,P-exact-05,P-exact-06,P-exact-07,P-exact-08,P-exact-09"
  use_l2 kvsink shr03 $tag
  VLLM_EXTRA="--gpu-memory-utilization $TWO_VLLM_UTIL" L1_GB_S=20 session shr03 $tag fail "${TWO_HOSTS[@]}" \
    "send name=a_store sets=P-exact ids=$ids" settle \
    "send name=b_fetch sets=P-exact ids=$ids port=8001 bg=1" "wait_log what=retrieve_start log=2 timeout=120" \
    kill9 "sleep secs=3" server_up wait_bg "vllm_check name=a_after" "vllm_check name=b_after port=8001" \
    "vllm_ensure model=$LLAMA reap=$REAP_WAIT" \
    "send name=b_again sets=P-exact ids=$ids port=8001" "send name=a_after sets=P-exact ids=$ids stats=1"
  # b_fetch overlaps A's kill; b_again re-reads from L2 (D-13 emptied B's L1
  # copies of L2 hits); a_after is A's first read after its restart.
  report_both shr03 $tag all "--outcomes=${tag}_b_fetch=pipelined" "--require=${tag}_b_fetch=pipelined" \
    "--outcomes=${tag}_b_again=pipelined" "--outcomes=${tag}_a_after=pipelined" \
    a_store b_fetch b_again a_after
  local kill_t; kill_t=$(grep -oE 'SIGKILL to the LMCache server.* [0-9:.]+$' $S/shr03/session_$tag.txt | grep -oE '[0-9:.]+$')
  progress "shr03: A killed at $kill_t; B retrieves ended between $(grep -m1 'MP retrieve end' $S/shr03/lmcache2_$tag.log | cut -c1-30) \
and $(grep 'MP retrieve end' $S/shr03/lmcache2_$tag.log | sed -n 10p | cut -c1-30) (the 10 b_fetch requests); \
$(paste -sd' ' $S/shr03/vllm_check_${tag}_b_after.txt | cut -c1-120); $(lmc2_errors shr03 $tag); \
kv-sink region errors: $(grep -c 'in error state' $KVDIR/asd-kvsink.log 2>/dev/null)"
}

# evt06_run <tag> <host A adapter extra>
evt06_run() {
  local tag=$1 extra=$2 a_own b_own
  a_own=$(for i in $(seq 0 9); do printf 'P-exact-%02d,' "$i"; done | sed 's/,$//')
  b_own=$(for i in $(seq 0 9); do printf 'P-ragged-%02d,' "$i"; done | sed 's/,$//')
  local shared; shared=$(for i in $(seq 0 9); do printf 'P-shared-%02d,' "$i"; done | sed 's/,$//')
  use_l2 cluster evt06 "$tag" 2
  SERVER2_FLAGS=$SERVER_FLAGS
  [ -n "$extra" ] && SERVER_FLAGS="--l2-adapter $(l2_cluster_json "$extra")"
  VLLM_EXTRA="--gpu-memory-utilization $TWO_VLLM_UTIL" L1_GB_S=20 session evt06 $tag fail "${TWO_HOSTS[@]}" \
    "send name=a_store sets=P-shared,P-exact ids=$shared,$a_own" settle \
    "send name=b_store sets=P-shared,P-ragged ids=$shared,$b_own port=8001" settle \
    "integrity name=stored mode=present ids=$shared,$a_own,$b_own" restart restart2 \
    "send name=a_l2 sets=P-shared,P-exact ids=$shared,$a_own stats=1" \
    "send name=b_l2 sets=P-shared,P-ragged ids=$shared,$b_own port=8001 stats=1" \
    "send name=a_cross sets=P-ragged ids=$b_own" \
    "integrity name=end mode=present ids=$shared,$a_own,$b_own"
  SERVER2_FLAGS=""
  local rargs=()
  [ -n "$extra" ] && rargs=("--no-hit-check=${tag}_a_l2" "--no-hit-check=${tag}_b_l2" "--no-hit-check=${tag}_a_cross")
  report_both evt06 $tag all "${rargs[@]}" a_store b_store a_l2 b_l2 a_cross
  progress "$tag: $(integrity_lines evt06 $tag | paste -sd';'); L2 deletes during the session: \
$(grep -oE '"client_delete_success": [0-9]+' <(l2diff evt06 ${tag}_a_l2) || echo none in a_l2)"
}
sec_evt06() { evt06_run evt06 ""; }
sec_evt06c() {
  evt06_run evt06c '"max_capacity_gb":0.5,"eviction":{"eviction_policy":"LRU","trigger_watermark":0.5,"eviction_ratio":0.5}'
}

# flt_cluster <test> <rf> <action> <node> <hit-check-after: 1|0>
flt_cluster() {
  local test=$1 rf=$2 action=$3 node=$4 hitcheck=$5 tag=$1
  local store=P-exact-15,P-exact-16,$P4 rag=P-ragged-12,P-ragged-13,P-ragged-14,P-ragged-15
  use_l2 cluster "$test" "$tag" "$rf"
  # O-1: at RF 1 a SIGKILLed node loses its last flush-max-ms (1 s) of
  # writes, so wait past it after settling.
  SEND_TIMEOUT=600 session "$test" $tag recompute server "vllm model=$LLAMA" \
    "send name=store sets=P-exact,P-ragged ids=$store,$rag" settle "sleep secs=3" restart \
    "host action=$action node=$node on=lookup_start" \
    "send name=victim sets=P-exact ids=P-exact-15 errors=1" "host_wait timeout=300" \
    "vllm_check name=afterfault" "vllm_ensure model=$LLAMA reap=$REAP_WAIT" \
    "send name=after sets=P-exact,P-ragged ids=P-exact-16,$P4,$rag errors=1" \
    "send name=new sets=P-ragged ids=P-ragged-10,P-ragged-11 errors=1" "vllm_check name=end"
  local rargs=("--no-hit-check=${tag}_victim")
  [ "$hitcheck" = 1 ] || rargs+=("--no-hit-check=${tag}_after")
  report "$test" $tag all "${rargs[@]}" store victim after new
  progress "$test: fault $(cat $S/$test/HOSTFAULT_* 2>/dev/null | paste -sd'|' | cut -c1-300); \
victim lookup: $(grep -m1 'MP lookup/prefetch start' $S/$test/lmcache_$tag.log | tail -c 60 | cut -c1-40) .. \
$(grep 'MP lookup/prefetch end' $S/$test/lmcache_$tag.log | tail -n 1 | grep -oE 'found_count=[0-9]+'); \
$(paste -sd' ' $S/$test/vllm_check_${tag}_afterfault.txt | cut -c1-120); cluster after: $(bash "$CL" status | paste -sd' ' | cut -c1-300)"
}
sec_flt02() { flt_cluster flt02 1 cluster_kill 3 0; }
sec_flt03() { flt_cluster flt03 2 cluster_kill 2 1; }
sec_flt04() { flt_cluster flt04 1 cluster_restart 2 1; }

sec_flt04k() {
  local tag=flt04k
  use_l2 kvsink flt04k $tag
  L1_GB_S=$((FLT04K_L1_GB + WIN_GB)) session flt04k $tag fail server "vllm model=$LLAMA" \
    "send name=store sets=P-exact ids=P-exact-10" settle restart \
    "host action=restart_kvsink" \
    "send name=gone sets=P-exact ids=P-exact-10 stats=1 errors=1" settle \
    "send name=fill sets=P-exact,P-ragged ids=P-exact-15,P-exact-16,P-ragged-15,P-ragged-16" settle "sleep secs=3" \
    "send name=reread sets=P-exact ids=P-exact-10 stats=1 errors=1" "vllm_check name=reread" \
    "vllm_ensure model=$LLAMA reap=$REAP_WAIT" restart \
    "send name=rereg sets=P-exact ids=P-exact-10 stats=1" "vllm_check name=end"
  # gone: the restarted server is empty, so nothing hits (hit_report's model
  # does not know that); vLLM recomputes and stores it again. reread: fill
  # pushed it out of the FLT04K_L1_GB (4) GB of general L1 (L1 writes that do
  # not fit are refused, and eviction starts only above the 0.8 watermark, so
  # the two 64-chunk prompts alone stop at 0.78; the P-ragged ones cross it,
  # and LRU evicts P-exact-10 first), the restarted
  # server holds the re-store, but LMCache's registration died with the old
  # server process. Under fail (D-17), a mid-forward failure is a clean 500.
  report flt04k $tag all "--no-hit-check=${tag}_gone" "--allow-error=${tag}_gone" "--allow-error=${tag}_reread" \
    "--outcomes=${tag}_reread=fell_back,failed,pipelined,not_deferred" \
    "--outcomes=${tag}_rereg=pipelined" "--require=${tag}_rereg=pipelined" store gone fill reread rereg
  progress "flt04k: kv-sink restart: $(cat $S/flt04k/HOSTDONE_* 2>/dev/null | paste -sd'|' | cut -c1-200); \
reread outcome: $(grep -oE 'pipelined_outcome=[a-z_]+' $S/flt04k/lmcache_$tag.log | sort | uniq -c | paste -sd' '); \
'kv-sink' warnings: $(grep -ciE 'kv-sink.*(fail|error|no such region)' $S/flt04k/lmcache_$tag.log)"
}

sec_evt04() {
  local tag=evt04 r steps=() sends=(store) rargs=()
  SERVER_FLAGS="--max-gpu-workers 2 $(server_flags "$S6_CAP" $LLAMA_CHUNK)"
  L2_PORT_S=$KVSINK_PORT
  group evt04 $tag
  # vLLM 2 serves one request before the first restart: a vLLM with no
  # heartbeat never re-registers (Stage 4).
  steps=(server "vllm model=$LLAMA" "vllm2 model=$LLAMA" "send name=store15 sets=P-exact ids=P-exact-15" settle
    "send name=store sets=P-exact ids=$P4" "send name=prime2 sets=P-exact ids=P-exact-00 port=8001" settle restart)
  sends+=(store15)
  for r in 1 2 3; do
    steps+=("send name=fill$r sets=P-long ids=P-long-0$r,P-long-0$((r + 3)) salt=fill$r port=8001 bg=1"
      "sleep secs=2" "send name=victim$r sets=P-exact ids=$P4 errors=1" wait_bg
      "vllm_check name=r$r" "vllm_check name=r${r}b port=8001" restart)
    sends+=("fill$r" "victim$r")
    rargs+=("--no-hit-check=${tag}_fill$r" "--allow-error=${tag}_victim$r"
      "--outcomes=${tag}_victim$r=pipelined,fell_back,refused,failed,not_deferred")
  done
  # 4-chunk fetches take about 0.1 s, and the eviction controller runs once a
  # second, so the rounds above rarely overlap an eviction (run 3: the fill's
  # second store is refused about 5.2 s after the fill starts, and the
  # pressure evictions follow at about +6.1 s and +7.1 s). These rounds fetch
  # the 64-chunk P-exact-15 (about 1.5 s), sent 4.5-5.5 s after the fill
  # starts, so the fetch spans that eviction.
  local r2 d
  for r2 in 4 5 6; do
    d=$(awk -v r="$r2" 'BEGIN {print 4.5 + (r - 4) * 0.5}')
    steps+=("send name=fill$r2 sets=P-long ids=P-long-0$((r2 + 3)),P-long-0$(( (r2 + 4) % 10 )) salt=fill$r2 port=8001 bg=1"
      "sleep secs=$d" "send name=victim$r2 sets=P-exact ids=P-exact-15 errors=1" wait_bg
      "vllm_check name=r$r2" "vllm_check name=r${r2}b port=8001" restart)
    sends+=("fill$r2" "victim$r2")
    rargs+=("--no-hit-check=${tag}_fill$r2" "--allow-error=${tag}_victim$r2"
      "--outcomes=${tag}_victim$r2=pipelined,fell_back,refused,failed,not_deferred")
  done
  VLLM_EXTRA="--gpu-memory-utilization $TWO_VLLM_UTIL" L1_GB_S=$((EVT04_L1_GB + WIN_GB)) \
    session evt04 $tag fail "${steps[@]}"
  report evt04 $tag all "${rargs[@]}" "${sends[@]}"
  progress "evt04 (L1 $EVT04_L1_GB GB): outcomes $(grep -oE 'pipelined_outcome=[a-z_]+' $S/evt04/lmcache_$tag.log | sort | uniq -c | sed 's/^ *//' | paste -sd' '); \
L1 eviction lines: $(grep -ciE 'evict' $S/evt04/lmcache_$tag.log); \
engine checks: $(cat $S/evt04/vllm_check_${tag}_r*.txt 2>/dev/null | grep -c vllm=alive) alive of 6"
}

sec_evt04l2() {
  local tag=evt04l2 p steps=() sends=(store) rargs=()
  use_l2 kvsink evt04l2 $tag
  steps=(server "vllm model=$LLAMA" "send name=store sets=P-exact ids=$P4" settle restart)
  # The evictor is loaded and armed before the send (importing lmcache takes
  # seconds); the step after `MP retrieve start` only releases it.
  # A failed fetch's window is quarantined for fetch_timeout_seconds (30 s),
  # so each round waits 32 s for both windows to be free again (else the next
  # fetch is `refused` and loads whole objects).
  for p in 10 11 12; do
    steps+=("l2evict name=e$p prompt=P-exact-$p chunk=3"
      "send name=ev$p sets=P-exact ids=P-exact-$p bg=1 errors=1" "wait_log what=retrieve_start timeout=120"
      "l2evict_go name=e$p" wait_bg "sleep secs=32")
    sends+=("ev$p"); rargs+=("--no-hit-check=${tag}_ev$p" "--allow-error=${tag}_ev$p"
      "--outcomes=${tag}_ev$p=pipelined,fell_back,failed,refused")
  done
  steps+=("vllm_check name=evs" "vllm_ensure model=$LLAMA reap=$REAP_WAIT"
    "send name=after sets=P-exact ids=P-exact-13,P-exact-14" "vllm_check name=end")
  rargs+=("--outcomes=${tag}_after=pipelined" "--require=${tag}_after=pipelined")
  session evt04l2 $tag fail "${steps[@]}"
  report evt04l2 $tag all "${rargs[@]}" "${sends[@]}" after
  progress "evt04l2: evictions $(tail -qn 1 $S/evt04l2/l2evict_${tag}_*.txt 2>/dev/null | paste -sd'|' | cut -c1-400); \
retrieve ends: $(grep 'MP retrieve end' $S/evt04l2/lmcache_$tag.log | tail -n 4 | cut -c1-24 | paste -sd' ')"
}

sec_e2e11() {
  if [ ! -f "${BASE70/#\/work//root/lmc-work}" ]; then
    mkdir -p $S/base70
    echo "##### base70 $(date -u +%T) $(wait_idle)"
    docker exec -e VLLM_BATCH_INVARIANT=1 -e VLLM_EXTRA="--gpu-memory-utilization $UTIL70" lmc-c \
      bash $H/run_baseline.sh $LLAMA70 $CB $W/base70 bi70 P-exact,P-ragged,P-shared > $S/base70/session_base70.txt 2>&1
    progress "base70: $(tail -n 1 $S/base70/session_base70.txt); KV cache: $(grep -m1 -oE 'GPU KV cache size: [0-9,]+ tokens' $S/base70/vllm_bi70.log)"
  fi
  local tag elig over steps sends rargs
  elig=$( { ids P-exact 1 "$CAP70"; ids P-ragged 1 "$CAP70"; } | paste -sd,)
  over=$( { ids P-exact $((CAP70 + 1)) 999; ids P-ragged $((CAP70 + 1)) 999; } | paste -sd,)
  SERVER_FLAGS=$(server_flags "$CAP70" $L70_CHUNK)
  tag=e2e11_04_c$CAP70
  KVSINK_CONF_FILE=$KVSINK_CONF_70B group e2e11 $tag
  steps=(server "vllm model=$LLAMA70" "send name=cold sets=P-exact,P-ragged stats=1" settle "l2stats name=after_cold"
    restart "send name=warmelig sets=P-exact,P-ragged ids=$elig stats=1")
  sends=(cold warmelig)
  rargs=("--outcomes=${tag}_warmelig=pipelined" "--require=${tag}_warmelig=pipelined"
    "--outcomes=${tag}_sh_warm=pipelined" "--require=${tag}_sh_warm=pipelined")
  # At the default cap every prompt (at most 64 chunks) is eligible.
  [ -n "$over" ] && { steps+=("send name=warmover sets=P-exact,P-ragged ids=$over stats=1"); sends+=(warmover); }
  steps+=("send name=sh_cold sets=P-shared" restart "send name=sh_warm sets=P-shared stats=1")
  sends+=(sh_cold sh_warm)
  BASES=$BASE70 VLLM_EXTRA="--gpu-memory-utilization $UTIL70" SEND_TIMEOUT=1800 \
    L1_GB_S=$((L70_L1_GB + WIN70_GB)) session e2e11 $tag fail "${steps[@]}"
  BASES=$BASE70 report e2e11 $tag all "${rargs[@]}" "${sends[@]}"
  e2e11_sizes $tag $KVSINK_PORT
  progress "e2e11: $(grep -hoE '[^ ]+ fetches layer by layer from L2 adapter [0-9]+, reading records of at most [0-9]+ bytes' $S/e2e11/lmcache_$tag.log | sort -u | head -n 1); \
$(grep -m1 -oE 'Per-layer staging matches the pipelined fetch plan for all [0-9]+ layers' $S/e2e11/lmcache_$tag.log); \
$(grep -m1 -oE 'KernelGroupInfo\(layers=[^)]*\)' $S/e2e11/lmcache_$tag.log); \
KV cache: $(grep -m1 -oE 'GPU KV cache size: [0-9,]+ tokens' $S/e2e11/vllm_${tag}_*.log); \
L2 after cold: $(grep -oE '"(objects|data_used_bytes)": "?[0-9]+' $S/e2e11/l2stats_${tag}_after_cold.json 2>/dev/null | paste -sd' '); \
kv-sink late completions $(grep -c 'late completion' $KVDIR/asd-kvsink.log 2>/dev/null), region errors $(grep -c 'in error state' $KVDIR/asd-kvsink.log 2>/dev/null), \
SERVER_FULL $(grep -c 'SERVER_FULL' $S/e2e11/lmcache_$tag.log)"
  # The 64G namespace holds host memory: give the next group the 16G one.
  group e2e11 after_70b
}
# e2e11_sizes <tag> <L2 port>: record layout of a 64-chunk, a ragged
# 48-chunk and a P-shared prompt -> $S/e2e11/sizes_<tag>.txt.
e2e11_sizes() {
  local p
  for p in P-exact-15 P-ragged-19 P-shared-00; do
    docker exec -e MODEL_NAME=$LLAMA70 -e HF_HOME=/work/hf lmc-c python $H/l2_sizes.py --port "$2" --corpus $CB prompt=$p
  done > $S/e2e11/sizes_$1.txt 2>&1
  progress "e2e11 sizes ($1): $(grep -E '^(segment 0 bins|chunk 0|P-exact-15)' $S/e2e11/sizes_$1.txt | head -n 3 | paste -sd';' | cut -c1-400)"
}

# e2e11p: T-E2E-03 and T-E2E-06 for the 70B on the plain path (aerospike-ce on
# 3000, single node, no pipelining): P-exact-10..15 and P-ragged-14/18/19 cold
# (L1), restart, L2 hits; P-shared cold (later requests hit the prefix in L1),
# restart, L2 hits. The set is truncated first (reset).
sec_e2e11p() {
  local tag=e2e11p ids
  ids="$P4,P-exact-15,P-ragged-14,P-ragged-18,P-ragged-19"
  SERVER_FLAGS=$CE_L2_70
  BASES=$BASE70 VLLM_EXTRA="--gpu-memory-utilization $UTIL70" SEND_TIMEOUT=1800 L2_PORT_S=3000 \
    L1_GB_S=$L70_L1_GB session e2e11 $tag fail server reset "vllm model=$LLAMA70" \
    "send name=cold sets=P-exact,P-ragged ids=$ids stats=1" "send name=sh_cold sets=P-shared" restart \
    "send name=warm sets=P-exact,P-ragged ids=$ids stats=1" "send name=sh_warm sets=P-shared stats=1"
  BASES=$BASE70 report e2e11 $tag all "--outcomes=${tag}_warm=not_deferred" "--outcomes=${tag}_sh_warm=not_deferred" \
    cold sh_cold warm sh_warm
  e2e11_sizes $tag 3000
  progress "e2e11p: aerospike-ce traffic during warm: $(docker exec lmc-c python $H/l2_stats.py diff \
$W/e2e11/l2stats_${tag}_warm_before.json $W/e2e11/l2stats_${tag}_warm_after.json 2>&1 | tail -n 1 | cut -c1-250)"
}

sec_e2e11c() {
  local tag=e2e11c
  use_l2 cluster e2e11 $tag 1
  BASES=$BASE70 VLLM_EXTRA="--gpu-memory-utilization $UTIL70" SEND_TIMEOUT=1800 L2_PORT_S=3300 session e2e11 $tag fail \
    server "vllm model=$LLAMA70" "send name=cold sets=P-exact,P-shared ids=$P4,$(ids P-shared 0 99)" restart \
    "send name=warm sets=P-exact,P-shared ids=$P4,$(ids P-shared 0 99) stats=1"
  BASES=$BASE70 report e2e11 $tag all cold warm
  progress "e2e11c: records per node $(bash "$CL" status | grep -oE 'master_objects=[0-9]+' | paste -sd' ')"
}

sec_flt08() {
  [ "${FLT08_APPROVED:-0}" = 1 ] || { progress "flt08: refused, needs FLT08_APPROVED=1 (host change: tc netem on lo)"; return 1; }
  local tag=flt08 ids
  ids=$( { ids P-exact 4 4; ids P-exact 2 2; } | paste -sd,)
  use_l2 kvsink flt08 $tag
  bash $TREE_HOST/functional/stage6/flt08_netem.sh check > $S/flt08/netem_before.txt 2>&1
  session flt08 $tag recompute server "vllm model=$LLAMA" "send name=store sets=P-exact ids=$ids" settle restart \
    "rxe name=b" "host action=netem_on secs=${FLT08_SECS:-60}" \
    "send name=loss1 sets=P-exact ids=$ids errors=1 stats=1" restart \
    "send name=loss2 sets=P-exact ids=$ids errors=1 stats=1" "host action=netem_off" "rxe name=a" \
    "vllm_check name=afterloss" "vllm_ensure model=$LLAMA" restart \
    "send name=after sets=P-exact ids=$ids" "vllm_check name=end"
  bash $TREE_HOST/functional/stage6/flt08_netem.sh remove > $S/flt08/netem_after.txt 2>&1
  bash $TREE_HOST/functional/stage6/flt08_netem.sh check >> $S/flt08/netem_after.txt 2>&1
  report flt08 $tag all "--outcomes=${tag}_loss1=pipelined,fell_back,failed,not_deferred" \
    "--outcomes=${tag}_loss2=pipelined,fell_back,failed,not_deferred" \
    "--outcomes=${tag}_after=pipelined,fell_back,not_deferred" store loss1 loss2 after
  progress "flt08 ($FLT08_MODE): $(cat $S/flt08/HOSTDONE_* 2>/dev/null | paste -sd'|' | cut -c1-300); \
outcomes $(grep -oE 'pipelined_outcome=[a-z_]+' $S/flt08/lmcache_$tag.log | sort | uniq -c | sed 's/^ *//' | paste -sd' '); \
rxe0 during loss: $(grep -oE '(duplicate_request|out_of_seq_request|retry_exceeded_err|rcv_rnr_err|send_rnr_err|completer_retry_err) [0-9]+' $S/flt08/rxe_${tag}_a.txt | paste -sd' '); \
lo after: $(grep -c netem $S/flt08/netem_after.txt) netem lines (want 0 in the final check)"
}

sec_dry() { bash $TREE_HOST/functional/stage6/scripts/dry_s6gpu.sh; }

mkdir -p $S
FLT08_MODE=${FLT08_MODE:-rdma}; export FLT08_MODE
# shellcheck disable=SC2048
for sec in ${*:-precheck shr01 shr01c shr02 shr02c shr03 evt06 flt02 flt03 flt04 flt04k evt04 evt04l2 idle}; do
  progress "section $sec started"
  sec_$sec
  progress "section $sec finished"
done
echo "##### STAGE 6 GPU DONE $(date -u +%T) $(wait_idle)"
