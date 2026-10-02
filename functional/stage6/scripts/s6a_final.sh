#!/bin/bash
# s6a_final.sh: cross-session checks for gpu-stage6a, then stop the CE cluster
# and the kv-sink server.
cd /root/lmc-work/functional/stage6/gpu || exit 1
echo "listener checks: ok $(cat */session_*.txt | grep -c 'listen_check.*: ok') FAIL $(cat */session_*.txt | grep -c 'listen_check.*FAIL')"
echo "section-7 lines in vLLM logs: $(cat */vllm*_*.log | grep -cE 'LayerProgress(RetrieveGenerationTimeout|RetrieveProgressTimeout|StaleGeneration)Error')"
echo "engine dead lines: $(cat */vllm*_*.log | grep -E 'EngineDeadError' | grep -vcE 'LMCache (DEBUG|INFO|WARNING|ERROR)')"
echo "LMCache tracebacks: $(cat */lmcache*_*.log | grep -c Traceback)"
echo "server stops: $(cat */session_*.txt | grep -oE 'stopped \((TERM|kill -9)\) exit=[0-9]+' | sort | uniq -c | paste -sd' ')"
echo "kv-sink late completions/region errors/failed writes: $(cat */kvsink_*/asd-kvsink.log 2>/dev/null | grep -cE 'late completion|in error state|kv-sink: region [0-9]+ (write|post) failed|dropped after a failed write')"
echo "D-17 recompute warnings (expected under recompute): $(cat */vllm*_*.log | grep -c 'needs a vLLM that rewinds')"
echo "HANG files: $(ls */HANG_* 2>/dev/null | wc -l)"
cd /root/lmc-work/LMCache || exit 1
bash functional/harness/cluster.sh stop
(CLONE=/root/lmc-work/LMCache . functional/newstack/kvsink_bp_env.sh; bash functional/harness/kvsink_server.sh stop)
sleep 2
docker ps --format '{{.Names}} {{.Status}}'
ss -ltn | grep -E '127.0.0.1:(8000|8001|6555|6556|8080|8081|3700|3300|3310|3320|3000) '
docker exec lmc-c pgrep -fa 'lmcache server|vllm serve' || echo "no LMCache or vLLM processes"
amd-smi metric --mem-usage | grep USED_VRAM
