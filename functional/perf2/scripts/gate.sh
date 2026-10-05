#!/bin/bash
# perf2 step 3, the correctness gate (new server + new client, rxe0, GID 1).
# Runs on the host. Logs: /root/lmc-work/functional/perf2/gate/.
#   1. RDMA logic suites (tests/v1/distributed/rdma, make logic-test) and the
#      queue_pairs config tests, in lmc-c.
#   2. One kv-sink server (perf.sh exp_start: device namespace, 8k sizing);
#      for queue_pairs 1 and 8: the pipelined RDMA integration test (the data
#      path checks T-RDMA-01..04 moved there), with the live RC QP count on
#      rxe0 sampled, then kvlayers --qps (every page verified, late writes;
#      512 KiB blocks like LMCache's K/V planes, under the 1 MiB record cap).
#      The QP count includes the server's side of each pair.
#   3. perf.sh smoke at QUEUE_PAIRS 1 and 8 (token-exact outputs through vLLM).
#   4. kv-sink log scan.
set -u
G=/root/lmc-work/functional/perf2/gate
GC=/work/functional/perf2/gate
T=/root/lmc-work/LMCache
PERF="bash $T/functional/perf/perf.sh"
ENV="HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= PYTHONDONTWRITEBYTECODE=1 RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1"
export DATA_DIR=/mnt/scratch/perf-aero
mkdir -p $G
say() { echo "$(date -u +%FT%TZ) $*" | tee -a $G/gate.log; }
lmc() { docker exec -w /work/LMCache lmc-c env $ENV "$@"; }
# qp_sampler <out>: max RC QPs on rxe0 seen every 0.2 s until killed.
qp_sampler() {
  local max=0 n
  while true; do
    n=$(rdma res show qp link rxe0/1 2>/dev/null | grep -c 'type RC')
    [ "$n" -gt "$max" ] && { max=$n; echo "$max" > "$1"; }
    sleep 0.2
  done
}

say "1. logic suites"
lmc timeout 1200 python -m pytest -p no:cacheprovider -q -rfEs tests/v1/distributed/rdma \
  tests/v1/distributed/l2_adapters/test_rdma_registration.py > $G/logic_pytest.txt 2>&1
say "pytest rdma + registration exit=$?: $(tail -n 1 $G/logic_pytest.txt)"
lmc timeout 1200 make -C tests/v1/distributed/rdma logic-test > $G/logic_make.txt 2>&1
say "make logic-test exit=$?: $(grep -ciE 'fail' $G/logic_make.txt) lines mentioning fail"

say "2. kv-sink server"
PERF_OUT=$G/server $PERF exp_start > $G/server_start.txt 2>&1
CLONE=$T . $T/functional/newstack/kvsink_bp_env.sh
say "server: $(grep -m1 -E 'started on|refusing' $G/server/progress.log | cut -c1-200)"
grep -q 'started on' $G/server/progress.log || { say "server did not start; GATE FAILED"; exit 1; }

say "kvlayers build"
docker exec lmc-c bash -c "command -v pkg-config >/dev/null; dpkg -s libuv1-dev >/dev/null 2>&1 || \
  (apt-get update -qq && apt-get install -y -qq libuv1-dev >/dev/null)
  rm -rf /work/kvlayers-build && mkdir -p /work/kvlayers-build && cp -a /work/aerospike-client-c-kvsink-bp /work/kvlayers-build/client
  cd /work/kvlayers-build/client && make clean >/dev/null && make -j16 EVENT_LIB=libuv > make_client.txt 2>&1 && \
  cd examples/kv_sink && make kvlayers" \
  > $G/kvlayers_build.txt 2>&1
say "kvlayers build exit=$?"

for qp in 1 8; do
  qp_sampler $G/it_qp${qp}_max_rc_qps.txt & sp=$!
  # Not RUN_AEROSPIKE_SLOW_INTEGRATION: its region-release test did not finish in
  # 15 min on rxe0 (873 adapter lifetimes, gate_attempt3).
  lmc env RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 \
    AEROSPIKE_TEST_PORT=$KVSINK_PORT AEROSPIKE_TEST_NAMESPACE=lmcache RDMA_QUEUE_PAIRS=$qp \
    timeout 900 python -m pytest -p no:cacheprovider -v -rfEs \
    --junitxml=$GC/it_qp$qp.junit.xml tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py \
    > $G/it_qp$qp.txt 2>&1
  rc=$?; kill $sp 2>/dev/null; wait $sp 2>/dev/null
  say "integration qp=$qp exit=$rc: $(tail -n 1 $G/it_qp$qp.txt); max RC QPs on rxe0: $(cat $G/it_qp${qp}_max_rc_qps.txt 2>/dev/null)"

  qp_sampler $G/kvl_qp${qp}_max_rc_qps.txt & sp=$!
  docker exec -w /work/kvlayers-build/client/examples/kv_sink lmc-c timeout 600 ./kvlayers --host 127.0.0.1 \
    --port $KVSINK_PORT --transport rc --ns lmcache --set kvl$qp --blocksz 524288 --fill --reps 10 --qps $qp > $G/kvlayers_qp$qp.txt 2>&1
  rc=$?; kill $sp 2>/dev/null; wait $sp 2>/dev/null
  say "kvlayers qp=$qp exit=$rc: $(grep -E '^row results|^late writes' $G/kvlayers_qp$qp.txt | paste -sd' '); max RC QPs: $(cat $G/kvl_qp${qp}_max_rc_qps.txt 2>/dev/null)"
done
cp $G/server/asd-kvsink-bp-perf.log $G/asd_it_kvlayers.log 2>/dev/null
PERF_OUT=$G/server $PERF exp_stop > $G/server_stop.txt 2>&1

say "3. perf.sh smoke"
for qp in 1 8; do
  QUEUE_PAIRS=$qp PERF_OUT=$G/smoke_qp$qp $PERF smoke > $G/smoke_qp$qp.txt 2>&1
  say "smoke qp=$qp: $(grep -E 'session smoke_(aon|lw) rc=' $G/smoke_qp$qp/progress.log | sed -E 's/.*session /session /' | paste -sd' ')"
  grep -h -E '^point |pipelined_outcome|INVALID|valid' $G/smoke_qp$qp/smoke_*/session_*.txt 2>/dev/null | cut -c1-200 >> $G/gate.log
done

say "4. kv-sink log scan"
for f in $G/asd_it_kvlayers.log $G/smoke_qp1/asd-kvsink-bp-perf.log $G/smoke_qp8/asd-kvsink-bp-perf.log; do
  say "$f: WARNING $(grep -c WARNING "$f" 2>/dev/null) ERROR $(grep -c -E 'ERROR|CRITICAL' "$f" 2>/dev/null) \
late $(grep -ciE 'late' "$f" 2>/dev/null) region-err $(grep -ciE 'region.*(err|fail|unknown)' "$f" 2>/dev/null) \
fail $(grep -ciE 'fail' "$f" 2>/dev/null) drop $(grep -ciE 'drop' "$f" 2>/dev/null)"
done
say "GATE DONE"
