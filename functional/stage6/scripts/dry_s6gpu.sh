#!/bin/bash
# CPU-only dry run of the Stage 6 GPU harness (stage6gpu.sh), on the host,
# against PRIVATE servers only: it never touches a GPU session's ports
# (8000/8001, 6555/6556, 8080/8081, 3000, 3100-3103, 3300-3323) or its
# containers (lmc-c, aero-kvsink, aerospike-ce, aero-n1..3). Every check
# of ports and containers is repeated right before each start.
#
#   private CE cluster  s6p-n1..3, 127.0.0.1:3600/3610/3620 (cluster.sh with
#                       AERO_CLUSTER_PORT_BASE/PREFIX/BASE)
#   private kv-sink     container kvsink-s6p (image lmcache-rocm:day1, like
#                       aero-kvsink), asd on 127.0.0.1:3500-3503, 4 GiB
#   LMCache servers     in lmc-d (no GPU devices), A 6755/8280/9290,
#                       B 6756/8281/9291
#
# Usage: DRY_TREE_HOST=/root/lmc-work/LMCache-d14 dry_s6gpu.sh [keep]
# ("keep" leaves the private servers up). Output: $D (one line per check).
set -u
TH=${DRY_TREE_HOST:-/root/lmc-work/LMCache-d14}
TC=/work/LMCache
D=/root/lmc-work/s6prep/dry
DC=/work/s6prep/dry
CTR=lmc-d
KCTR=kvsink-s6p
IMAGE=lmcache-rocm:day1
ASD=/root/lmc-work/aerospike-server-kvsink/target/Linux-x86_64/bin/asd
export AERO_CLUSTER_PORT_BASE=3600 AERO_CLUSTER_PREFIX=s6p-n AERO_CLUSTER_BASE=/root/lmc-work/s6prep/aero-cluster
CL=$TH/functional/harness/cluster.sh
CB=/work/functional/corpus/corpus_llama-3.1-8b-instruct.stage2b.json
LLAMA_CHUNK=$((32 * 2 * 8 * 128 * 256 * 2)); L70_CHUNK=$((80 * 2 * 8 * 128 * 256 * 2))
mkdir -p $D
rc=0
say() { echo "$(date -u +%T) $*"; }
fail() { say "FAIL: $*"; rc=1; }
# guard <ports regex> <what>: refuse to start anything if the ports are taken
# or a GPU-session container would be affected.
guard() {
  local busy; busy=$(ss -ltn | grep -E "127.0.0.1:($1) ")
  [ -z "$busy" ] || { say "REFUSED to start $2: ports busy: $(echo "$busy" | awk '{print $4}' | paste -sd' ')"; exit 1; }
  docker ps --format '{{.Names}}' | grep -qx "$KCTR" && [ "$2" = kvsink-s6p ] && { say "REFUSED: $KCTR already running"; exit 1; }
  say "guard ok for $2: $(docker ps --format '{{.Names}}' | paste -sd' ') up; ports $1 free"
}
inctr() { docker exec -i -w $TC -e HIP_VISIBLE_DEVICES= -e CUDA_VISIBLE_DEVICES= -e PYTHONDONTWRITEBYTECODE=1 -e HF_HOME=/work/hf -e HF_HUB_OFFLINE=1 $CTR "$@"; }

say "== 0. lmc-d"
[ "$(docker inspect -f '{{.State.Running}}' $CTR)" = true ] || docker start $CTR > /dev/null
inctr python -c "import lmcache, torch; print('lmcache', lmcache.__file__, 'gpus', torch.cuda.device_count())"

say "== 1. script syntax, netem plan (read-only)"
for f in harness/run_steps.sh harness/host_actions.sh harness/cluster.sh stage3/stage3.sh stage6/stage6gpu.sh \
    stage6/flt08_netem.sh stage6/scripts/dry_s6gpu.sh; do
  bash -n $TH/functional/$f && say "syntax ok: $f" || fail "syntax $f"
done
inctr python -m py_compile functional/harness/l2_integrity.py functional/harness/l2_segments.py \
  functional/harness/l2_stats.py functional/harness/wait_l2_settle.py && say "python compiles"
bash $TH/functional/stage6/flt08_netem.sh plan
FLT08_MODE=all bash $TH/functional/stage6/flt08_netem.sh plan | sed -n 2,3p
bash $TH/functional/stage6/flt08_netem.sh check
bash $TH/functional/stage6/flt08_netem.sh apply 60; [ $? = 3 ] && say "netem apply refused without approval (expected)"

say "== 2. adapter configs (stage6gpu.sh's specs)"
cluster_json="{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:3300,127.0.0.1:3310,127.0.0.1:3320\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"}"
evict_json="{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:3300,127.0.0.1:3310,127.0.0.1:3320\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\",\"max_capacity_gb\":0.5,\"eviction\":{\"eviction_policy\":\"LRU\",\"trigger_watermark\":0.5,\"eviction_ratio\":0.5}}"
rdma_json() { echo "{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:$3\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\",\"rdma\":{\"transport\":\"RC\",\"device_name\":\"rxe0\",\"gid_index\":1,\"window_count\":2,\"window_bytes\":$(($1 * $2))}}"; }
for cfg in "$cluster_json" "$evict_json" "$(rdma_json 4 $LLAMA_CHUNK 3100)" "$(rdma_json 1 $L70_CHUNK 3100)" "$(rdma_json 2 $L70_CHUNK 3100)"; do
  inctr python - "$cfg" <<'EOF' || fail "config $cfg"
import argparse, sys
from lmcache.v1.distributed.l2_adapters.config import add_l2_adapters_args, parse_args_to_l2_adapters_config
p = add_l2_adapters_args(argparse.ArgumentParser())
c = parse_args_to_l2_adapters_config(p.parse_args(["--l2-adapter", sys.argv[1]])).adapters[0]
r = c.rdma.window_plan if c.rdma.is_enabled() else None
ev = c.eviction_config
print("config ok: hosts", c.hosts, "capacity_gb", c.max_capacity_gb,
      "rdma windows", (r.window_count, r.window_bytes) if r else None,
      "eviction", (ev.eviction_policy, ev.trigger_watermark, ev.eviction_ratio) if ev else None)
EOF
done

say "== 3. 70B model facts and corpus"
inctr python - <<'EOF'
import json, glob
cfg = json.load(open(glob.glob("/work/hf/hub/models--meta-llama--Llama-3.3-70B-Instruct/snapshots/*/config.json")[0]))
L, kv, hd = cfg["num_hidden_layers"], cfg["num_key_value_heads"], cfg.get("head_dim") or cfg["hidden_size"] // cfg["num_attention_heads"]
chunk = 2 * L * kv * hd * 256 * 2
plane = kv * hd * 256 * 2
print(f"70B: layers={L} kv_heads={kv} head_dim={hd} dtype={cfg.get('torch_dtype')} vocab={cfg['vocab_size']}")
print(f"70B chunk = {chunk} bytes ({chunk / 2**20:.0f} MiB); K or V plane per layer = {plane // 1024} KiB; "
      f"slots per chunk at 512 KiB records = {2 * L}; chunks per 256-slot command = {256 // (2 * L)}")
print(f"KV per token = {2 * L * kv * hd * 2} bytes; 17408-token max-model-len = {2 * L * kv * hd * 2 * 17408 / 2**30:.2f} GiB")
EOF
inctr bash -c "python functional/harness/build_corpus.py --model meta-llama/Llama-3.3-70B-Instruct \
  --spec functional/corpus/corpus_spec.json --out $DC/corpus_llama-3.3-70b.json > $DC/build70.txt 2>&1; echo build exit=\$?"
inctr python - $CB $DC/corpus_llama-3.3-70b.json <<'EOF'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
for s in ("P-exact", "P-ragged", "P-shared", "P-multi", "P-long"):
    pa = {p["id"]: p["token_ids"] for p in a["sets"].get(s, [])}
    pb = {p["id"]: p["token_ids"] for p in b["sets"].get(s, [])}
    same = sum(1 for k in pa if pb.get(k) == pa[k])
    print(f"{s}: {same}/{len(pa)} prompts token-identical between the Llama-3.1-8B corpus and one built with the 70B tokenizer")
EOF

say "== 4. private CE cluster s6p-n1..3 (3600/3610/3620), rf 2"
guard "3600|3601|3602|3610|3611|3612|3620|3621|3622" "private CE cluster"
bash $CL stop > /dev/null 2>&1; bash $CL wipe > /dev/null 2>&1
bash $CL start 2 || fail "cluster start"
bash $CL status

say "== 5. two LMCache servers at once in lmc-d (run_steps.sh server/server2/restart2/kill9_2/server2_up) on the cluster"
guard "6755|6756|8280|8281|9290|9291" "LMCache servers"
pjson="{\"type\":\"aerospike\",\"hosts\":\"127.0.0.1:3600,127.0.0.1:3610,127.0.0.1:3620\",\"namespace\":\"lmcache\",\"set_name\":\"kv_chunks\"}"
inctr env LMC1_PORT=6755 LMC1_HTTP=8280 LMC1_PROM=9290 LMC2_PORT=6756 LMC2_HTTP=8281 LMC2_PROM=9291 \
  L1_SIZE_GB=2 L2_PORT=3600 CORPUS=$CB LMCACHE_SERVER_EXTRA="--l2-adapter $pjson" \
  bash functional/harness/run_steps.sh $DC/steps dry2 true server server2 "l2stats name=s0" settle \
  restart2 kill9_2 "sleep secs=2" server2_up restart "l2stats name=s1" > $D/steps_dry2.txt 2>&1
say "run_steps exit=$? ; $(grep -c '^===' $D/steps_dry2.txt) step lines"
grep -E '^===|exited|did not|warning' $D/steps_dry2.txt | cut -c1-200
for l in $D/steps/lmcache_dry2.log $D/steps/lmcache2_dry2.log; do
  say "$(basename $l): adapters $(grep -c 'Created Aerospike L2 adapter' $l), starts $(grep -c 'server extra' $l), Traceback/ERROR $(grep -cE 'Traceback|ERROR' $l)"
done
grep -m1 -oE 'Created Aerospike L2 adapter: [^)]*\)' $D/steps/lmcache2_dry2.log
ls $D/steps/l2stats_dry2_s0.json > /dev/null && say "l2stats (summed over 3 nodes): objects $(python3 -c "import json; print(json.load(open('$D/steps/l2stats_dry2_s0.json')).get('objects'))")"

say "== 6. l2_integrity.py, l2_segments.py evict --when, settle/stats sums, on synthetic D-14 objects (cluster)"
inctr python - <<'EOF' > $D/synthetic.txt 2>&1
import json, sys
sys.path.insert(0, "functional/harness")
import aerospike
from l2_segments import chunk_keys
d = json.load(open("/work/functional/corpus/corpus_llama-3.1-8b-instruct.stage2b.json"))
p = {q["id"]: q for s in d["sets"].values() for q in s}
c = aerospike.client({"hosts": [("127.0.0.1", 3600)]}).connect()
c.truncate("lmcache", "kv_chunks", 0)
import time; time.sleep(2)
n = 0
for pid in ("P-exact-10", "P-exact-11"):
    for k in chunk_keys("dry-model", p[pid]["token_ids"]):
        for i in range(3):
            c.put(("lmcache", "kv_chunks", f"{k}|s|w{n}|{i}"), {"v": bytearray(b"x" * 1024)})
        c.put(("lmcache", "kv_chunks", k + "|m"), {"state": "ok", "nseg": 3, "wid": f"w{n}"})
        n += 1
print("wrote", n, "objects of 4 records")
c.close()
EOF
cat $D/synthetic.txt
I="inctr env MODEL_NAME=dry-model python functional/harness/l2_integrity.py --port 3600 --corpus $CB"
$I whole --ids P-exact-10,P-exact-11 | tail -n 1 | sed 's/^/whole, clean: /'
inctr python -c "
import aerospike; c = aerospike.client({'hosts': [('127.0.0.1', 3600)]}).connect()
c.put(('lmcache', 'kv_chunks', 'orphan|s|w99|0'), {'v': bytearray(b'y')}); c.close()"
$I whole --ids P-exact-10,P-exact-11 | tail -n 1 | sed 's/^/whole, one orphan added (want FAIL, 1 orphan): /'
rm -f $D/EVICTGO_dry
inctr env MODEL_NAME=dry-model python functional/harness/l2_segments.py --port 3600 --corpus $CB \
  --when $DC/EVICTGO_dry evict prompt=P-exact-10 chunk=3 > $D/evict_dry.txt 2>&1 &
EV=$!
for _ in $(seq 100); do grep -q '^armed' $D/evict_dry.txt && break; sleep 0.2; done
say "evictor: $(head -n 1 $D/evict_dry.txt)"; touch $D/EVICTGO_dry; t_go=$(date -u +%T.%N | cut -c1-12); wait $EV
say "released at $t_go; $(tail -n 1 $D/evict_dry.txt)"
$I present --ids P-exact-10,P-exact-11 | sed 's/^/present after evict: /'
inctr python functional/harness/wait_l2_settle.py --port 3600 --quiet-seconds 3 | sed 's/^/settle (sum of nodes): /'
for n in 1 2 3; do echo -n "n$n objects=$(docker exec s6p-n$n asinfo -h 127.0.0.1 -p $((3600 + (n - 1) * 10)) -v namespace/lmcache 2>/dev/null | tr ';' '\n' | sed -n 's/^objects=//p') "; done; echo

say "== 7. host actions: cluster_kill armed on lookup_start, cluster_restart (private cluster)"
export TREE_HOST=$TH TREE_CTR=$TC PY314=/root/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14
# shellcheck source=../../harness/host_actions.sh
source $TH/functional/harness/host_actions.sh
HA=$D/hostact; mkdir -p $HA; rm -f $HA/HOST* ; : > $HA/lmcache_dryha.log
# watch_host reads lmc-c's PID only for HANG stack captures; none are made here.
( while :; do for req in $HA/HOSTREQ_*; do [ -f "$req" ] || continue; case $req in *.tmp) continue;; esac
    run=${req##*/HOSTREQ_}; [ -f "$HA/HOSTDONE_$run" ] && continue; _host_request $HA "$req"; done; sleep 0.05; done ) &
W=$!
echo "action=cluster_kill node=3 on=lookup_start" > $HA/HOSTREQ_dryha_1
for _ in $(seq 50); do [ -f $HA/HOSTDONE_dryha_1 ] && break; sleep 0.1; done
say "answer: $(cat $HA/HOSTDONE_dryha_1)"
sleep 0.5; echo "[dry] LMCache DEBUG: MP lookup/prefetch start: session=dry-1" >> $HA/lmcache_dryha.log
t_trig=$(date -u +%T.%N | cut -c1-12)
for _ in $(seq 600); do [ -s $HA/HOSTFAULT_dryha_1.txt ] && break; sleep 0.2; done
say "trigger at $t_trig; fault: $(cat $HA/HOSTFAULT_dryha_1.txt | cut -c1-300)"
echo "action=cluster_restart node=3" > $HA/HOSTREQ_dryha_2
for _ in $(seq 1200); do [ -f $HA/HOSTDONE_dryha_2 ] && break; sleep 0.2; done
say "restart answer: $(cut -c1-300 $HA/HOSTDONE_dryha_2)"
kill $W 2>/dev/null
bash $CL status

say "== 8. private kv-sink (kvsink-s6p, 3500-3503) and two RDMA LMCache servers at once"
guard "3500|3501|3502|3503" kvsink-s6p
K=/root/lmc-work/s6prep/kvsink; mkdir -p $K/work/smd $K/work/usr/udf/lua
sed -e 's/port 3100/port 3500/; s/access-port 3100/access-port 3500/; s/port 3101/port 3501/; s/port 3102/port 3502/; s/port 3103/port 3503/' \
    -e "s|/root/lmc-work/aerospike-kvsink-data/work|$K/work|g" -e 's/cluster-name lmcache-kvsink/cluster-name s6p-kvsink/' \
    -e 's/data-size 16G/data-size 4G/' $TH/functional/configs/aerospike-kvsink.conf > $K/kvsink-s6p.conf
grep -nE 'port|work|data-size|cluster-name' $K/kvsink-s6p.conf | sed 's/^/  conf /'
docker rm -f $KCTR > /dev/null 2>&1
docker run -d --name $KCTR --network host --device /dev/infiniband/uverbs0 --cap-add IPC_LOCK \
  --ulimit memlock=-1:-1 -v /root/lmc-work:/root/lmc-work $IMAGE sleep infinity > /dev/null
docker exec -d $KCTR bash -c "ulimit -n 20000; exec $ASD --config-file $K/kvsink-s6p.conf --foreground >> $K/asd.log 2>&1"
for _ in $(seq 60); do python3 -c "import socket; socket.create_connection(('127.0.0.1', 3500), 1)" 2>/dev/null && break; sleep 1; done
say "kvsink-s6p: $(python3 $TH/functional/harness/as_info.py 3500 build 2>&1) $(ss -ltnp | grep -c '127.0.0.1:350[0-3] ') ports"
guard "6755|6756|8280|8281|9290|9291" "RDMA LMCache servers"
for spec in "llama_cap4 4 $LLAMA_CHUNK" "l70_cap1 1 $L70_CHUNK"; do
  read -r name cap chunk <<< "$spec"
  j=$(rdma_json "$cap" "$chunk" 3500)
  inctr env LMC1_PORT=6755 LMC1_HTTP=8280 LMC1_PROM=9290 LMC2_PORT=6756 LMC2_HTTP=8281 LMC2_PROM=9291 \
    L1_SIZE_GB=2 L2_PORT=3500 LMCACHE_SERVER_EXTRA="--no-l1-use-lazy --pipelined-fetch --pipelined-max-chunks $cap --l2-adapter $j" \
    bash functional/harness/run_steps.sh $DC/rdma rd_$name true server server2 restart2 > $D/steps_rd_$name.txt 2>&1
  say "$name: exit=$?; A: $(grep -m1 -oE 'Created Aerospike L2 adapter: [^)]*\)' $D/rdma/lmcache_rd_$name.log); \
B: $(grep -m1 -oE 'Created Aerospike L2 adapter: [^)]*\)' $D/rdma/lmcache2_rd_$name.log); \
errors A/B $(grep -cE 'Traceback|ERROR' $D/rdma/lmcache_rd_$name.log)/$(grep -cE 'Traceback|ERROR' $D/rdma/lmcache2_rd_$name.log); \
warnings: $(grep -hE 'WARNING' $D/rdma/lmcache*_rd_$name.log | grep -iE 'rdma|memlock|window|pipelined' | cut -c1-160 | sort -u | head -n 3 | paste -sd'|')"
done

if [ "${1:-}" != keep ]; then
  say "== 9. cleanup"
  docker exec $KCTR pkill -x asd; sleep 3; docker rm -f $KCTR > /dev/null && say "removed $KCTR"
  bash $CL stop; bash $CL wipe
  docker stop -t 5 $CTR > /dev/null && say "stopped $CTR"
  ss -ltn | grep -E '127.0.0.1:(35[0-9][0-9]|36[0-9][0-9]|675[56]|828[01]|929[01]) ' && fail "private ports still open" || say "private ports closed"
fi
say "dry run rc=$rc"
exit $rc
