#!/bin/bash
# Recreate aero-kvsink-bp with /mnt/scratch/perf-aero bind-mounted (gpu-perf1
# phase 2). The old container is committed to an image (its writable layer
# holds the server's build and runtime packages) and kept, stopped, as
# aero-kvsink-bp-orig.
set -eu
C=aero-kvsink-bp
S=/root/lmc-work/functional/perf
if docker exec $C pgrep -x asd; then echo "asd is running; stop it first"; exit 1; fi
mkdir -p /mnt/scratch/perf-aero
docker inspect $C > $S/aero-kvsink-bp.orig.inspect.json
docker commit -m "aero-kvsink-bp writable layer, before recreating with /mnt/scratch/perf-aero (gpu-perf1 phase 2)" \
  $C aero-kvsink-bp:perf2-base
docker stop -t 10 $C
docker rename $C aero-kvsink-bp-orig
docker run -d --name $C --network host --ipc private --shm-size 64m \
  --device /dev/infiniband/uverbs0 --ulimit memlock=-1:-1 --cap-add IPC_LOCK \
  --security-opt seccomp=unconfined -w /app \
  -v /root/lmc-work:/root/lmc-work -v /mnt/scratch/perf-aero:/mnt/scratch/perf-aero \
  aero-kvsink-bp:perf2-base sleep infinity
docker inspect $C > $S/aero-kvsink-bp.perf2.inspect.json
docker inspect $C --format 'binds={{json .HostConfig.Binds}} devices={{json .HostConfig.Devices}} image={{.Config.Image}} net={{.HostConfig.NetworkMode}} ulimits={{json .HostConfig.Ulimits}} caps={{json .HostConfig.CapAdd}}'
docker exec $C bash -c 'ls -ld /mnt/scratch/perf-aero; A=/root/lmc-work/aerospike-server-kvsink-bp/target/Linux-x86_64/bin/asd; ls -l $A; echo "missing libs: $(ldd $A | grep -c "not found")"'
docker images aero-kvsink-bp:perf2-base --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'
docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'aero|lmc'
