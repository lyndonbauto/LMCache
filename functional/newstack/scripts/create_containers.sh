#!/bin/bash
# Host: create the new-stack containers from lmcache-rocm:day1, with no GPU
# devices. aero-kvsink-bp builds and runs the kv-sink server 046e8558d;
# lmc-newstack builds the C client 523d51ea and LMCache (tree LMCache-1a at
# /work/LMCache) and runs the tests. Both see rxe0 through uverbs0.
set -eu
RDMA="--network host --device /dev/infiniband/uverbs0 --ulimit memlock=-1:-1 --cap-add IPC_LOCK --security-opt seccomp=unconfined"
docker inspect aero-kvsink-bp >/dev/null 2>&1 || \
  docker run -d --name aero-kvsink-bp $RDMA -v /root/lmc-work:/root/lmc-work lmcache-rocm:day1 sleep infinity
docker inspect lmc-newstack >/dev/null 2>&1 || \
  docker run -d --name lmc-newstack $RDMA -v /root/lmc-work:/work \
    -v /root/lmc-work/LMCache-1a:/work/LMCache lmcache-rocm:day1 sleep infinity
docker ps --filter name=aero-kvsink-bp --filter name=lmc-newstack --format '{{.Names}} {{.Status}}'
