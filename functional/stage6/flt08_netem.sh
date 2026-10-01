#!/bin/bash
# T-FLT-08 (plan 5.7, lowest env E5a): 5% packet loss for 60 s, approximated
# on Soft-RoCE with tc netem. rxe0 is bound to `lo`, so the loss has to go on
# lo's egress, which every local service shares. THIS IS A HOST CHANGE:
# `apply` refuses unless FLT08_APPROVED=1 (set it only after a human
# approved it in Slack and the box is otherwise idle). Runs on the host.
#
#   flt08_netem.sh plan            print the exact commands for FLT08_MODE (no change)
#   flt08_netem.sh check           read-only: lo's qdiscs and filters, loaded modules
#   flt08_netem.sh apply <secs>    add the loss, then remove it after <secs> seconds
#                                  (a detached watchdog, so a dead driver cannot
#                                  leave it behind); prints one line
#   flt08_netem.sh remove          remove it now (idempotent); prints the drop count
#
# FLT08_MODE:
#   rdma (default)  only RoCEv2 packets (UDP destination port 4791) are dropped:
#                   a 4-band prio root qdisc on lo, netem on band 4 (which the
#                   default priomap never selects), and a u32 filter steering
#                   UDP/4791 there. Everything else on lo (Aerospike TCP,
#                   LMCache ZMQ, vLLM HTTP) goes through bands 1-3 untouched,
#                   but every loopback packet now passes a qdisc
#   all             netem directly as lo's root qdisc: every loopback packet,
#                   TCP included, has a 5% chance of being dropped
# FLT08_LOSS (default 5%). Changes are logged to $LOG (append).
set -u
MODE=${FLT08_MODE:-rdma}
LOSS=${FLT08_LOSS:-5%}
LOG=${FLT08_LOG:-/root/lmc-work/functional/stage6/flt08_netem.log}
PIDF=/run/flt08_netem_watchdog.pid
SELF=$(readlink -f "$0")

cmds_apply() {
  case $MODE in
    rdma)
      echo "tc qdisc add dev lo root handle 1: prio bands 4"
      echo "tc qdisc add dev lo parent 1:4 handle 40: netem loss $LOSS"
      echo "tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip dport 4791 0xffff flowid 1:4";;
    all)
      echo "tc qdisc add dev lo root handle 1: netem loss $LOSS";;
    *) echo "unknown FLT08_MODE=$MODE" >&2; return 2;;
  esac
}
cmd_remove() { echo "tc qdisc del dev lo root"; }

ours_present() { tc qdisc show dev lo | grep -q netem; }

drops() { tc -s qdisc show dev lo | awk '/netem/{n=1} n && /dropped/{gsub(",","");for(i=1;i<=NF;i++) if($i=="dropped") print $(i+1); exit}'; }

case "${1:-}" in
  plan)
    echo "# FLT08_MODE=$MODE loss=$LOSS"
    echo "# apply:"; cmds_apply | sed 's/^/  /'
    echo "# remove (after the window, and on any abort):"; cmd_remove | sed 's/^/  /'
    echo "# loads modules on first use: sch_prio, sch_netem, cls_u32 (rdma) or sch_netem (all)"
    ;;
  check)
    echo "lo qdiscs:"; tc -s qdisc show dev lo | sed 's/^/  /'
    echo "lo filters:"; tc filter show dev lo 2>/dev/null | sed 's/^/  /'
    echo "modules loaded: $(lsmod | awk '$1 ~ /^(sch_netem|sch_prio|cls_u32)$/{print $1}' | paste -sd' ')"
    if [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "watchdog running: pid $(cat $PIDF)"; fi
    ;;
  apply)
    secs=${2:?apply needs the window in seconds}
    [ "${FLT08_APPROVED:-0}" = 1 ] || { echo "refused: netem on lo is a host change; set FLT08_APPROVED=1 after approval"; exit 3; }
    tc qdisc show dev lo | grep -q '^qdisc noqueue 0: root' \
      || { echo "refused: lo's root qdisc is not the default noqueue: $(tc qdisc show dev lo | head -n 1)"; exit 4; }
    t0=$(date -u +%T.%N | cut -c1-12)
    while read -r c; do
      # shellcheck disable=SC2086
      $c || { echo "apply failed at: $c"; $(cmd_remove) 2>/dev/null; exit 5; }
    done < <(cmds_apply)
    # The watchdog removes the loss even if the driver dies; remove() kills it.
    FLT08_LOG=$LOG setsid nohup bash -c "sleep $secs; FLT08_FROM_WATCHDOG=1 bash $SELF remove" \
      >> "$LOG" 2>&1 < /dev/null &
    echo $! > $PIDF
    echo "$(date -u +%FT%TZ) applied mode=$MODE loss=$LOSS for ${secs}s (watchdog pid $!): $(cmds_apply | paste -sd';')" >> "$LOG"
    echo "netem $MODE loss $LOSS on lo from $t0 for ${secs}s (watchdog pid $!)"
    ;;
  remove)
    if ours_present; then
      d=$(drops)
      by=driver; [ "${FLT08_FROM_WATCHDOG:-0}" = 1 ] && by=watchdog
      $(cmd_remove) && echo "$(date -u +%FT%TZ) removed by $by (dropped ${d:-?})" >> "$LOG"
      echo "netem removed by $by at $(date -u +%T.%N | cut -c1-12), packets dropped: ${d:-?}" | tee "${LOG%.log}_last.txt"
    else
      echo "netem not present; last removal: $(cat "${LOG%.log}_last.txt" 2>/dev/null)"
    fi
    if [ "${FLT08_FROM_WATCHDOG:-0}" != 1 ] && [ -f $PIDF ]; then kill "$(cat $PIDF)" 2>/dev/null; fi
    rm -f $PIDF
    ;;
  *) sed -n '2,23p' "$0"; exit 2 ;;
esac
