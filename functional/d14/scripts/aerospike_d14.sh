#!/bin/bash
# Start/stop the private Aerospike CE node for D-14 (127.0.0.1:3200-3202).
# Usage: aerospike_t2.sh start|stop|status
set -u
CTR=aerospike-ce-d14
CONF=/root/lmc-work/functional/d14/scripts/aerospike-d14.conf
case "${1:-}" in
  start)
    if docker ps --format '{{.Names}}' | grep -qx "$CTR"; then echo "already running"; exit 0; fi
    docker rm -f "$CTR" >/dev/null 2>&1
    docker run -d --name "$CTR" --network host --ulimit nofile=65536:65536 \
      -v "$CONF":/etc/aerospike/aerospike.template.conf:ro \
      aerospike/aerospike-server:latest >/dev/null || exit 1
    for _ in $(seq 1 60); do
      if docker exec "$CTR" asinfo -h 127.0.0.1 -p 3200 -v status 2>/dev/null | grep -q ok; then
        echo "started $CTR on 127.0.0.1:3200"; docker exec "$CTR" asinfo -h 127.0.0.1 -p 3200 -v build; exit 0
      fi
      sleep 1
    done
    echo "did not come up"; docker logs --tail 30 "$CTR"; exit 1
    ;;
  stop) docker rm -f "$CTR" && echo "removed $CTR" ;;
  status) docker ps -a --filter name="$CTR" --format '{{.Names}} {{.Status}}' ;;
  *) echo "usage: $0 start|stop|status"; exit 2 ;;
esac
