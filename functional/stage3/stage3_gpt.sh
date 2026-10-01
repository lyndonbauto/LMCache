#!/bin/bash
# Stage 3, gpt-oss-120b: T-E2E-08 pipelined half and T-PIPE-11.
# Sources stage3.sh for its helpers (kv-sink groups, sessions, reports).
#
# Usage: stage3_gpt.sh [section ...]   (default: precheck e2e08p pipe11 idle)
#   e2e08p   T-E2E-08 pipelined half: Stage 2c's T-E2E-02..07 send sequence
#            (gptoss_e2e, layerwise on) with --pipelined-fetch at cap 4 and the
#            LMCache flags Stage 2c used (no --separate-object-groups), then a
#            restart and P-shared / P-multi again as pure L2 hits. L2 sends of
#            prompts within the cap must be `pipelined`. rxe0 counters are
#            snapshotted around the P-exact/P-ragged L2 send (one object group:
#            every layer fetches every chunk, the control for T-PIPE-11).
#   pipe11   stage3.sh pipe11 (--separate-object-groups by default)
# Oracles (Stage 2c, batch size 1, VLLM_BATCH_INVARIANT=1): the no-cache
# baseline at block 16 for no-hit and whole-prompt hits; vLLM's own prefix
# cache for proper-prefix hits, at block 256 (split-matched to LMCache, the
# verdict) and at block 16 (reported).
# Environment: E2E08_SERVER_FLAGS (default empty, as Stage 2c), GPT_BASE.
set -u
# shellcheck source=stage3.sh
source "$(dirname "$0")/stage3.sh"
REF=/work/functional/stage2/gptoss_ref
GPT_BASE=${GPT_BASE:-$REF/base_b16_all.json}
E2E08_SERVER_FLAGS=${E2E08_SERVER_FLAGS:-}

# e2e08p_report <tag> <ref>: one report against prefix-cache reference <ref>.
e2e08p_report() {
  local tag=$1 ref=$2 elig_args=()
  [ -n "$ELIG" ] && elig_args=("--outcomes=${tag}_l03elig=pipelined" "--require=${tag}_l03elig=pipelined"
    "--oracle=${tag}_l03elig=$REF/${ref}_rw.json")
  CORPUS_S=$CG BASES=$GPT_BASE report gpt_e2e08p "$tag" "$ref" \
    --oracle="${tag}_w02=$REF/${ref}_rw.json" --oracle="${tag}_l03over=$REF/${ref}_rw.json" \
    --oracle="${tag}_shc=$REF/${ref}_sc.json" --oracle="${tag}_shw=$REF/${ref}_sw.json" \
    --oracle="${tag}_muc=$REF/${ref}_mc.json" --oracle="${tag}_muw=$REF/${ref}_mw.json" \
    --oracle="${tag}_l2sh=$REF/${ref}_sw.json" --oracle="${tag}_l2mu=$REF/${ref}_mw.json" \
    "${elig_args[@]}" \
    --outcomes="${tag}_l2sh=pipelined,not_deferred" --outcomes="${tag}_l2mu=pipelined,not_deferred" \
    shortc shortw c02 w02 l03elig l03over longc longw shc shw muc muw l2sh l2mu
}

sec_e2e08p() {
  SERVER_FLAGS="$E2E08_SERVER_FLAGS $(server_flags 4 $GPT_CHUNK)"
  tag=e2e08p
  ELIG=$( { ids P-exact 1 4 $CG; ids P-ragged 1 4 $CG; } | paste -sd, | sed 's/^,//; s/,$//')
  OVER=$( { ids P-exact 5 999 $CG; ids P-ragged 5 999 $CG; } | paste -sd, | sed 's/^,//; s/,$//')
  progress "e2e08p: L2 send within cap 4: $ELIG; over: $OVER"
  group gpt_e2e08p $tag
  CORPUS_S=$CG VLLM_EXTRA="${GPT_VLLM_EXTRA:-}" session gpt_e2e08p $tag fail server "vllm model=$GPT" \
    "send name=shortc sets=P-short-v2 stats=1" "send name=shortw sets=P-short-v2 stats=1" \
    "send name=c02 sets=P-exact,P-ragged" "send name=w02 sets=P-exact,P-ragged" \
    restart "rxe name=b" "send name=l03elig sets=P-exact,P-ragged ids=$ELIG stats=1" "rxe name=a" \
    "send name=l03over sets=P-exact,P-ragged ids=$OVER stats=1" \
    "send name=longc sets=P-long" "send name=longw sets=P-long" \
    "send name=shc sets=P-shared" "send name=shw sets=P-shared" \
    "send name=muc sets=P-multi" "send name=muw sets=P-multi" \
    restart "send name=l2sh sets=P-shared stats=1" "send name=l2mu sets=P-multi stats=1"
  e2e08p_report $tag pc256
  e2e08p_report $tag pc16_r1
  progress "e2e08p: rxe0 packets during l03elig: $(rxe_delta gpt_e2e08p $tag b a) \
(one object group, so every layer reads every chunk)"
}

mkdir -p $S
# shellcheck disable=SC2048
for sec in ${*:-precheck e2e08p pipe11 idle}; do
  progress "section $sec started"
  sec_$sec
  progress "section $sec finished"
done
echo "##### STAGE 3 gpt-oss DONE $(date -u +%T) $(wait_idle)"
