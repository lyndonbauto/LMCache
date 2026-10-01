#!/bin/bash
# Host side. Cold kv-sink server, then one probe or pytest run in aero-kvsink.
# Usage: run_probe.sh <probe|pytest2|probe_warm> <out-file> [args...]
set -u
KIND=$1; OUT=$2; shift 2
KS="bash /root/lmc-work/LMCache-cpu/functional/harness/kvsink_server.sh"
C=/root/lmc-work/functional/stage3/crash
ENVS="-e PYTHONDONTWRITEBYTECODE=1 -e PYTHONFAULTHANDLER=1 -e RUN_AEROSPIKE_INTEGRATION=1 -e AEROSPIKE_TEST_HOST=127.0.0.1 -e AEROSPIKE_TEST_PORT=3100 -e AEROSPIKE_TEST_NAMESPACE=lmcache -e RDMA_DEVICE=rxe0 -e RDMA_GID_INDEX=1 -e HIP_VISIBLE_DEVICES= -e CUDA_VISIBLE_DEVICES= ${EXTRA_ENV:-}"
if [ "${COLD:-1}" = 1 ]; then
  $KS stop >/dev/null
  KVSINK_LOG=$C/asd-kvsink.log $KS start >/dev/null || { echo "server start failed" > "$OUT"; exit 1; }
fi
case "$KIND" in
  probe)
    docker exec -w /root/lmc-work/LMCache-cpu $ENVS aero-kvsink nice -n 19 timeout 300 python $C/late_write_probe.py "$@" > "$OUT" 2>&1
    ;;
  pytest2)
    docker exec -w /root/lmc-work/LMCache-cpu $ENVS aero-kvsink nice -n 19 timeout 300 python -m pytest -p no:cacheprovider -v -rfEs \
      "tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py::test_a_pipelined_fetch_lands_every_stored_byte_in_l1" \
      "tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py::test_a_missing_record_falls_back_to_a_whole_reload" "$@" > "$OUT" 2>&1
    ;;
esac
echo "exit=$?" >> "$OUT"
