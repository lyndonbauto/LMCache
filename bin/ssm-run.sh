#!/usr/bin/env bash
#
# Run a shell command on one or more benchmark nodes via SSM and print the
# output. Nodes have no public IPs, so this is the access path.
#
#   ./bin/ssm-run.sh <instance-id>[,<instance-id>...] '<command>'
#   ./bin/ssm-run.sh all 'uptime'
#
# Exists because `aws ssm send-command` + `get-command-invocation` polling is
# several steps, and every verification step in the runbook needs it.

set -euo pipefail

TARGETS="${1:?usage: ssm-run.sh <instance-ids|all> <command>}"
COMMAND="${2:?usage: ssm-run.sh <instance-ids|all> <command>}"
REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT_NAME:-aerospike-lmcache-bench}"
TIMEOUT="${SSM_TIMEOUT:-300}"

if [ "$TARGETS" = "all" ]; then
  TARGETS=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Project,Values=$PROJECT" \
              "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' ',')
fi

IFS=',' read -r -a IDS <<< "$TARGETS"

CMD_ID=$(aws ssm send-command --region "$REGION" \
  --instance-ids "${IDS[@]}" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"$(printf '%s' "$COMMAND" | sed 's/\\/\\\\/g; s/"/\\"/g')\"],executionTimeout=[\"$TIMEOUT\"]" \
  --query 'Command.CommandId' --output text)

# Poll until every invocation leaves the in-flight states.
for _ in $(seq 1 "$TIMEOUT"); do
  sleep 2
  PENDING=$(aws ssm list-command-invocations --region "$REGION" \
    --command-id "$CMD_ID" \
    --query "length(CommandInvocations[?Status=='Pending'||Status=='InProgress'||Status=='Delayed'])" \
    --output text)
  [ "$PENDING" = "0" ] && break
done

for id in "${IDS[@]}"; do
  NAME=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$id" \
    --query 'Reservations[0].Instances[0].Tags[?Key==`Name`]|[0].Value' --output text 2>/dev/null || echo "$id")
  echo "===== $NAME ($id) ====="
  aws ssm get-command-invocation --region "$REGION" \
    --command-id "$CMD_ID" --instance-id "$id" \
    --query '[Status,StandardOutputContent,StandardErrorContent]' --output text 2>/dev/null \
    || echo "  (no invocation result)"
  echo
done
