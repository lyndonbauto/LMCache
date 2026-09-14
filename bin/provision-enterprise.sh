#!/usr/bin/env bash
#
# Provision the Aerospike ENTERPRISE server on every storage node.
#
# Enterprise rather than Community because Community 8.x caps usable device
# size at 2 TiB per device ("usable device size must be <= 2199023255552,
# trimming original size 7499994365952"), which would silently discard 5.5 TB
# of every 7.5 TB i3en device. The feature key removes the cap.
#
# The feature key is read from a local path and pushed to the nodes. It is a
# license file and is NOT in this repository -- see .gitignore.
#
#   FEATURES_CONF=/path/to/features.conf ./bin/provision-enterprise.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEATURES_CONF="${FEATURES_CONF:-/home/lyndon/Downloads/features.conf}"
EE_URL="https://download.aerospike.com/artifacts/aerospike-server-enterprise/8.1.2.5/aerospike-server-enterprise_8.1.2.5_tools-13.0.3_amzn2023_x86_64.tgz"

[ -s "$FEATURES_CONF" ] || { echo "error: feature key not found at $FEATURES_CONF" >&2; exit 1; }

SERVERS=$(aws ec2 describe-instances \
  --filters Name=tag:Role,Values=aerospike-server Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' ',')
[ -n "$SERVERS" ] || { echo "error: no running server nodes" >&2; exit 1; }

FEATURES_B64=$(base64 -w0 "$FEATURES_CONF")

REMOTE_SCRIPT=$(cat <<REMOTE
set -euo pipefail

systemctl stop aerospike 2>/dev/null || true

# Preserve the benchmark config; the package install ships its own default.
cp -f /etc/aerospike/aerospike.conf /root/aerospike.conf.bench 2>/dev/null || true

# ---------------------------------------------------------------------------
# Enterprise package
# ---------------------------------------------------------------------------
rpm -e --nodeps aerospike-server-enterprise 2>/dev/null || true
if ! rpm -q aerospike-server-enterprise >/dev/null 2>&1; then
  rpm -e --nodeps aerospike-server-community 2>/dev/null || true

  # Use the amzn2023 build, NOT el9.
  #
  # The el9 Enterprise package is not ABI-compatible with Amazon Linux 2023 and
  # fails in two separate ways: it needs OpenLDAP 2.6 (AL2023 ships 2.4), and
  # even once that is satisfied, asd core-dumps during startup inside
  # cf_tls_init -> OpenSSL default-context init with
  # "CRITICAL (alloc): aligned allocation during startup" -- Aerospike's
  # allocator guard tripping on AL2023's OpenSSL build.
  #
  # Aerospike publishes a dedicated amzn2023 artifact. Use it.
  rm -f /usr/lib64/libldap.so.2* /usr/lib64/liblber.so.2* 2>/dev/null || true
  ldconfig

  rm -rf /opt/aerospike-ee && mkdir -p /opt/aerospike-ee && cd /opt/aerospike-ee
  curl -fsSL -o ee.tgz "$EE_URL"
  tar -xf ee.tgz
  cd aerospike-server-enterprise_*/
  ./asinstall || true
fi
rpm -q aerospike-server-enterprise

# Prove the binary actually resolves every shared library before relying on it.
if ldd /usr/bin/asd | grep -q 'not found'; then
  echo "FATAL: asd has unresolved shared libraries:"; ldd /usr/bin/asd | grep 'not found'; exit 1
fi

mkdir -p /etc/aerospike /var/log/aerospike

# ---------------------------------------------------------------------------
# Feature key: 0600, owned by aerospike.
# ---------------------------------------------------------------------------
echo "$FEATURES_B64" | base64 -d > /etc/aerospike/features.conf
chown aerospike:aerospike /etc/aerospike/features.conf
chmod 0600 /etc/aerospike/features.conf
grep -q 'valid-until-date' /etc/aerospike/features.conf || { echo "FATAL: feature key looks wrong"; exit 1; }

# Restore the benchmark config and register the key.
cp -f /root/aerospike.conf.bench /etc/aerospike/aerospike.conf
if ! grep -q 'feature-key-file' /etc/aerospike/aerospike.conf; then
  sed -i 's|^    cluster-name lmcache-bench\$|    cluster-name lmcache-bench\n    feature-key-file /etc/aerospike/features.conf|' /etc/aerospike/aerospike.conf
fi
grep -n 'feature-key-file' /etc/aerospike/aerospike.conf

# ---------------------------------------------------------------------------
# POSITIVE instance-store identification.
#
# Numbering is NOT stable: across these five nodes the EBS root has been
# observed as nvme0n1, nvme2n1 AND nvme3n1. Selecting by index would eventually
# hand the root volume to Aerospike's raw-device engine and destroy the node.
#
# A device qualifies only if ALL of the following hold:
#   - lsblk MODEL is exactly "Amazon EC2 NVMe Instance Storage"
#   - TYPE is "disk"
#   - neither it nor any child partition has a mountpoint
#   - it is not the disk backing /
# The expected count is asserted; a mismatch aborts rather than proceeding,
# because a wrong count means the identification logic is wrong and the cost of
# guessing is a destroyed node.
# ---------------------------------------------------------------------------
python3 - <<'PYEOF'
import json, subprocess, sys

EXPECTED = 8
MODEL = "Amazon EC2 NVMe Instance Storage"

root_src = subprocess.run(["findmnt", "-no", "SOURCE", "/"],
                          capture_output=True, text=True).stdout.strip()
lsblk = json.loads(subprocess.run(
    ["lsblk", "-J", "-o", "NAME,MODEL,SIZE,TYPE,MOUNTPOINT"],
    capture_output=True, text=True).stdout)

def mounted(node):
    if node.get("mountpoint"):
        return True
    return any(mounted(c) for c in node.get("children", []))

root_disk = None
for d in lsblk["blockdevices"]:
    names = [d["name"]] + [c["name"] for c in d.get("children", [])]
    if any(root_src.endswith(n) for n in names):
        root_disk = d["name"]

chosen, rejected = [], []
for d in lsblk["blockdevices"]:
    name, model = d["name"], (d.get("model") or "").strip()
    why = None
    if d.get("type") != "disk":
        why = "not a disk"
    elif model != MODEL:
        why = f"model={model!r}"
    elif mounted(d):
        why = "has mountpoint"
    elif name == root_disk:
        why = "backs /"
    if why:
        rejected.append(f"/dev/{name} ({why})")
    else:
        chosen.append(f"/dev/{name}")

print("root filesystem source :", root_src, "-> disk", root_disk)
print("rejected               :", ", ".join(rejected) or "none")
print("selected               :", " ".join(sorted(chosen)))

if len(chosen) != EXPECTED:
    print(f"FATAL: expected {EXPECTED} instance-store devices, found {len(chosen)}. "
          "Refusing to continue -- identification logic is wrong.")
    sys.exit(1)
if root_disk and f"/dev/{root_disk}" in chosen:
    print("FATAL: root disk is in the selected set. Refusing to continue.")
    sys.exit(1)

with open("/tmp/bench_devices", "w") as fh:
    fh.write("\n".join(sorted(chosen)) + "\n")

# Rewrite the device stanza with exactly the verified set.
path = "/etc/aerospike/aerospike.conf"
lines, out, inserted = open(path).read().splitlines(), [], False
for line in lines:
    if line.strip().startswith("device /dev/"):
        if not inserted:
            out.extend(f"        device {d}" for d in sorted(chosen))
            inserted = True
        continue
    out.append(line)
if not inserted:
    out2 = []
    for line in out:
        out2.append(line)
        if "DEVICE_LIST_PLACEHOLDER" in line:
            out2.extend(f"        device {d}" for d in sorted(chosen))
    out = out2
open(path, "w").write("\n".join(out) + "\n")
print("device lines written  :", sum(1 for l in out if l.strip().startswith("device /dev/")))
PYEOF

# ---------------------------------------------------------------------------
# Wipe: TRIM (near-instant, and these NVMe read back as zeros after discard)
# followed by an 8 MiB header zero. blkdiscard WITHOUT -z, because -z rewrites
# all 7.5 TB. Verified afterwards rather than assumed -- a bare blkdiscard
# returning success while leaving data is what caused the first failed start.
# ---------------------------------------------------------------------------
DEVS=\$(cat /tmp/bench_devices | tr '\n' ' ')
for d in \$DEVS; do
  ( blkdiscard -f "\$d" 2>/dev/null || true
    dd if=/dev/zero of="\$d" bs=1M count=8 oflag=direct status=none ) &
done
wait

BAD=0
for d in \$DEVS; do
  N=\$(dd if="\$d" bs=1M count=8 2>/dev/null | tr -d '\000' | wc -c)
  [ "\$N" = "0" ] || { echo "WIPE VERIFY FAILED: \$d has \$N non-zero bytes"; BAD=1; }
done
[ "\$BAD" = "0" ] || { echo "FATAL: wipe verification failed"; exit 1; }
echo "wipe verified: all \$(echo \$DEVS | wc -w) devices read as zero"

systemctl start aerospike
sleep 20
echo "aerospike=\$(systemctl is-active aerospike)"
grep -E 'CRITICAL|FAILED' /var/log/aerospike/aerospike.log | tail -3 || true
REMOTE
)

B64=$(printf '%s' "$REMOTE_SCRIPT" | base64 -w0)
SSM_TIMEOUT=900 "$HERE/ssm-run.sh" "$SERVERS" \
  "echo $B64 | base64 -d > /tmp/prov.sh && bash /tmp/prov.sh 2>&1 | tail -40"
