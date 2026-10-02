#!/bin/bash
# probe_listen.sh <with-env 0|1> <out>: start plain vLLM (Llama), list listeners, stop it.
OUT=$2; mkdir -p $(dirname $OUT)
export HF_HOME=/work/hf HF_HUB_OFFLINE=1
[ "$1" = 1 ] && source /work/LMCache/functional/harness/loopback_env.sh
env | grep -E 'VLLM_HOST_IP|LOOPBACK|MASTER_ADDR|IFNAME'
vllm serve meta-llama/Llama-3.1-8B-Instruct --host 127.0.0.1 --port 8000 --seed 0 --max-model-len 4096 --gpu-memory-utilization 0.5 > $OUT.vllm.log 2>&1 &
P=$!
for _ in $(seq 600); do curl -sf localhost:8000/health >/dev/null && break; sleep 1; done
ss -Hltunp > $OUT
bash /work/LMCache/functional/harness/listen_check.sh probe$1
kill $P; sleep 15; kill -9 $P 2>/dev/null
for p in $(pgrep -f "VLLM::"); do kill -9 $p; done
