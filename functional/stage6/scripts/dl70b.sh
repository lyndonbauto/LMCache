#!/bin/bash
# Download meta-llama/Llama-3.3-70B-Instruct (safetensors, tokenizer, config;
# not original/*.pth) into HF_HOME=/work/hf, for T-E2E-11. Runs inside lmc-c.
# The token comes only from the HF_TOKEN environment variable; this script
# never prints or writes it. It first fetches config.json alone, so a gated
# repo without access fails fast (status "no-access") before the big pull.
# Usage: HF_TOKEN=... dl70b.sh   (normally via docker exec -e HF_TOKEN -d)
# Output: $LOGDIR/dl70b.log (progress), $LOGDIR/dl70b.status (one word:
# checking | no-access | downloading | done | failed).
set -u
set +x
REPO=meta-llama/Llama-3.3-70B-Instruct
LOGDIR=${LOGDIR:-/work/functional/stage6/logs}
mkdir -p "$LOGDIR"
LOG=$LOGDIR/dl70b.log
STATUS=$LOGDIR/dl70b.status
export HF_HOME=/work/hf HF_HUB_OFFLINE=0 HF_HUB_DISABLE_TELEMETRY=1
unset HF_HUB_ENABLE_HF_TRANSFER
[ -n "${HF_TOKEN:-}" ] || { echo "HF_TOKEN is not set" >> "$LOG"; echo failed > "$STATUS"; exit 1; }

echo checking > "$STATUS"
echo "$(date -u +%FT%TZ) access check: config.json" >> "$LOG"
if ! nice -n 19 hf download "$REPO" config.json --format quiet >> "$LOG" 2>&1; then
  if grep -qiE '401|403|gated|access|Unauthorized|forbidden' "$LOG"; then
    echo no-access > "$STATUS"
  else
    echo failed > "$STATUS"
  fi
  echo "$(date -u +%FT%TZ) access check failed" >> "$LOG"
  exit 1
fi
echo "$(date -u +%FT%TZ) access ok; downloading" >> "$LOG"
echo downloading > "$STATUS"
IONICE=""; command -v ionice >/dev/null && IONICE="ionice -c3"
# Four workers keep the box responsive (the default is 8).
if nice -n 19 $IONICE hf download "$REPO" --max-workers 4 --format quiet \
    --include '*.safetensors' --include '*.json' --include 'tokenizer*' \
    --include 'generation_config.json' --exclude 'original/*' >> "$LOG" 2>&1; then
  echo done > "$STATUS"
  echo "$(date -u +%FT%TZ) download finished" >> "$LOG"
else
  echo failed > "$STATUS"
  echo "$(date -u +%FT%TZ) download failed" >> "$LOG"
  exit 1
fi
