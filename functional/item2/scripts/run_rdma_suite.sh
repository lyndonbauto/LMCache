#!/bin/bash
# E1 (rxe0, GID 1) RDMA suite and the E0 layerwise/RDMA unit files, CPU only,
# inside lmc-c, from the item-2 clone. Logs to functional/item2/logs/.
set -u
OUT=/root/lmc-work/functional/item2/logs
ENV="HIP_VISIBLE_DEVICES= CUDA_VISIBLE_DEVICES= PYTHONDONTWRITEBYTECODE=1 RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1"
docker exec -w /work/LMCache-cpu lmc-c env $ENV nice -n 19 timeout 1200 python -m pytest -p no:cacheprovider -v -rfEs \
  --junitxml=/work/functional/item2/logs/rdma_suite.junit.xml tests/v1/distributed/rdma > $OUT/rdma_suite.txt 2>&1
echo "pytest exit=$?" >> $OUT/rdma_suite.txt
docker exec -w /work/LMCache-cpu lmc-c env $ENV nice -n 19 timeout 1200 make -C tests/v1/distributed/rdma test RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 > $OUT/rdma_harness_direct.txt 2>&1
echo "make exit=$?" >> $OUT/rdma_harness_direct.txt
docker exec -w /work/LMCache-cpu lmc-c env $ENV nice -n 19 timeout 1200 python -m pytest -p no:cacheprovider -v -rfEs \
  --junitxml=/work/functional/item2/logs/e0_units.junit.xml \
  tests/v1/layerwise/test_layer_arrival_pump.py tests/v1/distributed/test_l1_rdma_windows.py \
  tests/v1/distributed/l2_adapters/test_rdma_registration.py tests/v1/distributed/test_pipelined_placer_access.py \
  tests/v1/layerwise/test_aerospike_layer_arrival_source.py tests/v1/distributed/test_rdma_window_leaser.py \
  tests/v1/layerwise/test_pipelined_retrieve.py tests/v1/layerwise/test_shared_keys.py \
  tests/v1/layerwise/test_fetch_planner.py tests/v1/distributed/test_rdma_window_placer.py \
  tests/v1/distributed/test_aerospike_l2_adapter_config.py tests/v1/layerwise/test_arrival_source_conformance.py \
  > $OUT/e0_units.txt 2>&1
echo "pytest exit=$?" >> $OUT/e0_units.txt
tail -3 $OUT/rdma_suite.txt; grep -E "FAIL|PASS$|exit=" $OUT/rdma_harness_direct.txt | tail -15; tail -3 $OUT/e0_units.txt
