#!/usr/bin/env bash
# Gate a benchmark arm on the LIVE record cap, not on the config file.
#
# This is the check that prevents the project's most dangerous failure mode:
# collecting plausible-looking numbers from a cluster that is not in the
# configuration you think it is. LMCache's discover_record_cap() reads
# max-record-size first and falls back to write-block-size, so this reproduces
# exactly what the client will resolve.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARM="${1:?usage: verify-record-cap.sh <A|B>}"
NS="${AEROSPIKE_NAMESPACE:-lmcache}"

case "$ARM" in
  A) WANT=1048576 ;;
  B) WANT=8388608 ;;
  *) echo "error: arm must be A or B" >&2; exit 2 ;;
esac

OUT=$("$HERE/ssm-run.sh" all "asinfo -h 127.0.0.1 -v 'namespace/$NS' 2>/dev/null | tr ';' '\n' | grep -E '^max-record-size=|^write-block-size=' || echo 'UNAVAILABLE'")
echo "$OUT"
echo "---------------------------------------------"

# Replicate the client's resolution order: max-record-size first if > 0,
# otherwise write-block-size.
python3 - "$WANT" <<PY
import re, sys, subprocess
want = int(sys.argv[1])
text = """$OUT"""
fail = False
node = None
for line in text.splitlines():
    m = re.match(r'===== (\S+)', line)
    if m:
        node, mrs, wbs = m.group(1), None, None
        continue
    for key, pat in (("mrs", r'max-record-size=(\d+)'), ("wbs", r'write-block-size=(\d+)')):
        mm = re.search(pat, line)
        if mm:
            if key == "mrs": mrs = int(mm.group(1))
            else: wbs = int(mm.group(1))
    if node and 'write-block-size=' in line:
        eff = mrs if (mrs or 0) > 0 else wbs
        status = "OK " if eff == want else "BAD"
        if eff != want: fail = True
        print(f"{status} {node}: max-record-size={mrs} write-block-size={wbs} -> client resolves {eff} (want {want})")
        node = None
print()
if fail:
    print(f"GATE FAILED for Arm $ARM -- DO NOT COLLECT DATA. Fix the cluster first.")
    sys.exit(1)
print(f"GATE PASSED for Arm $ARM: client will resolve a {want}-byte record cap.")
PY
