#!/usr/bin/env bash
#
# Switch the cluster between the two sweep arms.
#
#   set-arm.sh A   # untuned: stock defaults -> connector falls back to 1 MiB
#   set-arm.sh B   # tuned:   max-record-size 8M + flush-size 8M
#
# Changing flush-size changes the on-device block format, so the devices MUST
# be wiped; a restart alone would leave Aerospike reading blocks written under
# the other arm's geometry. The wipe is also what makes the two arms
# comparable -- each starts from an empty namespace.
#
# Note on the arm definitions: Arm B sets max-record-size AND flush-size, which
# is what the connector's discover_record_cap() looks for. Arm A sets neither,
# which on Aerospike 8.x means asinfo reports no usable value and the connector
# silently uses its hardcoded 1 MiB default -- the "untuned" case a user gets
# by simply pointing LMCache at a stock cluster.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARM="${1:?usage: set-arm.sh <A|B>}"

case "$ARM" in
  A) FLUSH="1M"; MRS_LINE="" ;;
  B) FLUSH="8M"; MRS_LINE="        max-record-size 8M" ;;
  *) echo "error: arm must be A or B" >&2; exit 2 ;;
esac

SERVERS=$(aws ec2 describe-instances \
  --filters Name=tag:Role,Values=aerospike-server Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' ',')

REMOTE=$(cat <<REMOTE
set -euo pipefail
systemctl stop aerospike || true

python3 - <<'PYEOF'
import re
path = "/etc/aerospike/aerospike.conf"
txt = open(path).read()

txt = re.sub(r'^\s*flush-size\s+\S+\s*\$', '        flush-size ${FLUSH}',
             txt, flags=re.M)
txt = re.sub(r'^\s*max-record-size\s+\S+\s*\n', '', txt, flags=re.M)
mrs = """${MRS_LINE}"""
if mrs.strip():
    txt = txt.replace("        flush-size ${FLUSH}",
                      mrs + "\n        flush-size ${FLUSH}")
open(path, "w").write(txt)
PYEOF
grep -E 'flush-size|max-record-size' /etc/aerospike/aerospike.conf

# Wipe: TRIM then an 8 MiB header zero, then VERIFY. The device list is the one
# written by the positive lsblk model match in provision-enterprise.sh.
DEVS=\$(cat /tmp/bench_devices | tr '\n' ' ')
for d in \$DEVS; do
  ( blkdiscard -f "\$d" 2>/dev/null || true
    dd if=/dev/zero of="\$d" bs=1M count=8 oflag=direct status=none ) &
done
wait
for d in \$DEVS; do
  N=\$(dd if="\$d" bs=1M count=8 2>/dev/null | tr -d '\000' | wc -c)
  [ "\$N" = "0" ] || { echo "FATAL: \$d not zeroed (\$N bytes)"; exit 1; }
done
echo "wipe verified (\$(echo \$DEVS | wc -w) devices)"

systemctl start aerospike
sleep 20
echo "aerospike=\$(systemctl is-active aerospike)"
REMOTE
)

B64=$(printf '%s' "$REMOTE" | base64 -w0)
"$HERE/ssm-run.sh" "$SERVERS" "echo $B64 | base64 -d > /tmp/setarm.sh && bash /tmp/setarm.sh 2>&1 | tail -12"
