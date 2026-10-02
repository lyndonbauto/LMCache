#!/bin/bash
# One run_steps session (LMCache L1 only + vLLM Llama + one prompt) to check listeners.
cd /root/lmc-work/functional/stage3-newstack
docker exec -e VLLM_BATCH_INVARIANT=1 -e CORPUS=/work/functional/corpus/corpus_llama-3.1-8b-instruct.stage2b.json lmc-c \
  bash /work/LMCache/functional/harness/run_steps.sh /work/functional/stage3-newstack/listen/rs1 rs1 true server \
  "vllm model=meta-llama/Llama-3.1-8B-Instruct" "send name=one sets=P-exact ids=P-exact-00" > listen/rs1.session.txt 2>&1
echo "rc=$?" >> listen/rs1.session.txt
