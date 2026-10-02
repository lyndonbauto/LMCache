#!/bin/bash
# Host side: run term_probe_ctr.sh for three server configurations and take Python stacks 3 s after SIGTERM.
D=/root/lmc-work/functional/stage3-newstack/d15x; WD=/work/functional/stage3-newstack/d15x; mkdir -p $D
PY314=/root/.local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14
ROOT=$(docker inspect -f '{{.State.Pid}}' lmc-c)
RDMA='{"type":"aerospike","hosts":"127.0.0.1:3700","namespace":"lmcache","set_name":"kv_chunks","rdma":{"transport":"RC","device_name":"rxe0","gid_index":1,"window_count":2,"window_bytes":134217728}}'
PLAIN='{"type":"aerospike","hosts":"127.0.0.1:3700","namespace":"lmcache","set_name":"kv_chunks"}'
run() {
  local n=$1; shift
  docker exec -d lmc-c bash /work/functional/stage3-newstack/scripts/term_probe_ctr.sh $WD $n "$@"
  for _ in $(seq 300); do [ -f $D/$n.ready ] && break; sleep 0.5; done
  pid=$(cat $D/$n.ready)
  nsenter -t $ROOT -m -p -- $PY314 -I /work/LMCache/functional/harness/pystacks.py $pid > $D/$n.stacks.txt 2>&1
  for _ in $(seq 200); do [ -f $D/$n.result ] && break; sleep 0.5; done
  cat $D/$n.result
}
run pipe_rdma --no-l1-use-lazy --pipelined-fetch --pipelined-max-chunks 4 --l2-adapter "$RDMA"
run rdma_only --no-l1-use-lazy --l2-adapter "$RDMA"
run plain --no-l1-use-lazy --l2-adapter "$PLAIN"
