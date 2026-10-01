#!/bin/bash
# D-14 round 2 (store-side heal): build, storage ITs on :3200, the cluster file, then the unit batch.
set -u
D=/root/lmc-work/functional/d14
docker exec lmc-d /work/functional/d14/scripts/build.sh > $D/logs/build_r2.txt 2>&1 || { echo BUILD_FAILED > $D/logs/round2.done; exit 1; }
$D/scripts/run_storage_it.sh storage_it2 tests/v1/distributed/test_aerospike_l2_integration.py tests/v1/distributed/test_aerospike_record_layouts_integration.py tests/v1/distributed/test_aerospike_storage_integrity_integration.py
$D/scripts/run_cluster_it.sh cluster_it2 "" 1000
docker exec lmc-d /work/functional/d14/scripts/units.sh units2 > $D/logs/units2.out 2>&1
echo DONE > $D/logs/round2.done
