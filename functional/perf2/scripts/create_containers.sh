#!/bin/bash
# Host: create the perf2 containers. Both use host networking and see rxe0
# through uverbs0.
#   aero-kvsink-bp  ubuntu:24.04, builds and runs the kv-sink server; sees the
#                   scratch disk's /mnt/scratch/perf-aero (device namespace file)
#   lmc-c           vllm/vllm-openai-rocm:v0.27.1 (ROCm 7.2.3, torch 2.11,
#                   vLLM 0.27.1) with the GPU; builds the C client and LMCache
#                   (/work/LMCache) and runs vLLM, LMCache and the harness
set -eu
RDMA="--network host --device /dev/infiniband/uverbs0 --ulimit memlock=-1:-1 --cap-add IPC_LOCK --security-opt seccomp=unconfined"
docker inspect aero-kvsink-bp >/dev/null 2>&1 || \
  docker run -d --name aero-kvsink-bp $RDMA -v /root/lmc-work:/root/lmc-work \
    -v /mnt/scratch/perf-aero:/mnt/scratch/perf-aero ubuntu:24.04 sleep infinity
docker inspect lmc-c >/dev/null 2>&1 || \
  docker run -d --name lmc-c $RDMA --device /dev/kfd --device /dev/dri --group-add video \
    --ipc host --shm-size 32g -v /root/lmc-work:/work --entrypoint sleep \
    vllm/vllm-openai-rocm:v0.27.1 infinity
docker ps --filter name=aero-kvsink-bp --filter name=lmc-c --format '{{.Names}} {{.Image}} {{.Status}}'
