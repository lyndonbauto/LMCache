#!/usr/bin/env bash
#
# Extend (or shorten) the auto-teardown deadline for the benchmark cluster.
#
#   ./bin/extend-teardown.sh 6      # 6 more hours from NOW
#   ./bin/extend-teardown.sh        # show the current deadline and exit
#
# The new deadline is measured from the moment you run this, not from the
# existing deadline -- so running it with 6 always means "I need six more
# hours from now", regardless of how much time was left.
#
# This is a standalone command rather than a `terraform apply -var ...` on
# purpose: the schedule has `ignore_changes = [schedule_expression]` so that a
# routine apply for an unrelated reason cannot silently extend the deadline.
# Moving the deadline should always be a deliberate act.

set -euo pipefail

PROJECT="${PROJECT_NAME:-aerospike-lmcache-bench}"
SCHEDULE="${PROJECT}-auto-teardown"
REGION="${AWS_REGION:-us-east-1}"

current() {
  aws scheduler get-schedule --name "$SCHEDULE" --region "$REGION" \
    --query 'ScheduleExpression' --output text
}

if [ $# -eq 0 ]; then
  echo "Schedule : $SCHEDULE"
  echo "Deadline : $(current)  (UTC)"
  echo "Now      : $(date -u +%Y-%m-%dT%H:%M:%S)  (UTC)"
  echo
  echo "To extend: $0 <hours-from-now>"
  exit 0
fi

HOURS="$1"
case "$HOURS" in
  ''|*[!0-9]*) echo "error: hours must be a positive integer" >&2; exit 2 ;;
esac

NEW_AT=$(date -u -d "+${HOURS} hours" +%Y-%m-%dT%H:%M:%S)

echo "Current deadline : $(current)"
echo "New deadline     : at(${NEW_AT})"

# get-schedule returns the full definition; reuse the existing target and role
# rather than reconstructing them, so this script cannot drift from Terraform.
DEF=$(aws scheduler get-schedule --name "$SCHEDULE" --region "$REGION")
TARGET_ARN=$(echo "$DEF" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Target"]["Arn"])')
ROLE_ARN=$(echo "$DEF" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Target"]["RoleArn"])')

aws scheduler update-schedule \
  --name "$SCHEDULE" \
  --region "$REGION" \
  --schedule-expression "at(${NEW_AT})" \
  --schedule-expression-timezone UTC \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"${TARGET_ARN}\",\"RoleArn\":\"${ROLE_ARN}\"}" \
  --output text --query 'ScheduleArn'

echo "Extended. Confirmed deadline: $(current)"
