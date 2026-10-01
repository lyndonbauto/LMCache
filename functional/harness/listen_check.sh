#!/bin/bash
# listen_check.sh <label> [out-file]: fail (exit 1) if any TCP or UDP socket
# listening in this network namespace is bound to a non-loopback address
# (0.0.0.0, *, [::] or a host IP). The containers share the host's network
# namespace, so this sees every listener on the box; process names are only
# shown for processes in this PID namespace. LISTEN_ALLOW lists the allowed
# <proto>:<port> pairs (default "tcp:22 udp:4791": the host's sshd and the
# rdma_rxe kernel's RoCEv2 socket, which binds every interface). The listener
# table is appended to out-file.
set -u
LABEL=${1:-listeners}
OUTF=${2:-/dev/null}
ALLOW=" ${LISTEN_ALLOW:-tcp:22 udp:4791} "
table=$(ss -Hltunp 2>/dev/null)
{ echo "# $LABEL $(date -u +%FT%TZ)"; echo "$table"; } >> "$OUTF"
bad=""
while read -r proto _state _rq _sq local _peer proc; do
  [ -n "${local:-}" ] || continue
  port=${local##*:}
  addr=${local%:*}
  addr=${addr%%%*}
  case $addr in
    127.*|\[::1\]|\[::ffff:127.*) continue;;
  esac
  case $ALLOW in *" $proto:$port "*) continue;; esac
  bad="$bad
  $proto $local ${proc:-<process not visible from here>}"
done <<< "$table"
if [ -n "$bad" ]; then
  echo "=== listen_check $LABEL: FAIL, non-loopback listener(s):$bad"
  exit 1
fi
echo "=== listen_check $LABEL: ok (all listeners on loopback; allowed:$ALLOW)"
