#!/bin/bash
# Start the cluster control daemon (from the D-14 tree) on the host if it is not running.
PIDF=/root/lmc-work/aero-cluster/ctl.pid
if [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "ctl daemon running pid $(cat $PIDF)"; exit 0; fi
mkdir -p /root/lmc-work/aero-cluster/ctl
nohup setsid /root/lmc-work/LMCache-d14/functional/harness/cluster_ctl_daemon.sh >> /root/lmc-work/functional/d14/logs/ctl_daemon.log 2>&1 < /dev/null &
echo $! > $PIDF; echo "ctl daemon started pid $!"
