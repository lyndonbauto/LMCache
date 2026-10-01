#!/bin/bash
PIDF=/root/lmc-work/aero-cluster/ctl.pid
[ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null && echo "ctl daemon stopped"; rm -f $PIDF
