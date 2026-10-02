#!/bin/bash
# s6a_totals.sh [section...]: per send, n / ok / exact / errors / outcomes from
# each report's Totals line.
cd /root/lmc-work/functional/stage6/gpu || exit 1
for d in ${*:-shr01 shr02 shr03 evt06 flt02 flt03 flt04 flt04k evt04 evt04l2}; do
  for f in "$d"/report_*_all.md; do
    [ -f "$f" ] || continue
    echo "## $f"
    tail -n 1 "$f" | sed 's/^Totals: //' | tr ',' '\n' | sed 's/^ *//' | awk -F: '
      { k=$1; v=$2; for (i=3;i<=NF;i++) v=v":"$i; a[k]=a[k]" "v; if (!(k in seen)) {seen[k]=1; order[++n]=k} }
      END { for (i=1;i<=n;i++) print "  " order[i] ":" a[order[i]] }'
  done
done
