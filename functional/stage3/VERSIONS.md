# Stage 3: versions

## GPU runs (gpu-stage3b, 2026-10-01 21:47-23:39Z): the final table

| Component | Version |
|---|---|
| Host | DigitalOcean gpu-mi300x1-192gb, 1x MI300X (gfx942), Ubuntu 24.04, kernel 6.8.0-138-generic |
| ROCm / HIP | ROCm 10.0.0 / HIP 7.15.26333 (unchanged) |
| Container image | `lmcache-rocm:day1` (`sha256:50c62263906e…`), container `lmc-c`; kv-sink in `aero-kvsink` |
| torch / vLLM | 2.12.0+rocm10.0.0 / 0.27.1.dev5+gf46a9dfe2.d20260827 (installed copy, unpatched; V2 model runner, async scheduling on) |
| LMCache (product code) | box tree `/root/lmc-work/LMCache` HEAD `a3504150` at start; product code = `e9cd0689` (D-14 fix) on top of `00cd3eee`, native extensions rebuilt by gpu-d17-narrow (`BUILD_WITH_HIP=1 BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1`). No rebuild in this run |
| LMCache (harness) | fast-forwarded to `d10a376f` (functional only) for every Llama section, pipe11, e2e08p and rdma06gpu. Then `git pull` brought origin's newstack product merge (`e4701b9a`..`e6d35a0c`, not built). The tree was reset to `d10a376f` before anything ran on it; `stage3_gpt.sh` from `b6b9d76f` (e2e08pl2) and the `4c7a3df3` pipe06 change were applied on top, as uncommitted working-tree changes. Left that way: see CHANGES.md |
| kv-sink server (`asd` in `aero-kvsink`, 127.0.0.1:3100-3103) | `feat/kv-sink-fetch-pipelined` @ `512b0c2079eb`, build `8.1.3.0-112-g512b0c207` (fencing, server issue 4); namespace `lmcache` `data-size 16G` |
| Aerospike C client (LMCache's extension) | stock aerospike-client-c-libuv 7.3.0 |
| Aerospike CE (`aerospike-ce`, 127.0.0.1:3000) | 8.2.0.0; not used by Stage 3 sessions |
| RDMA | Soft-RoCE `rxe0` on `lo`, RC, GID index 1; counters count about 1 KiB packets |
| Models | Llama-3.1-8B-Instruct (`0e9e39f249a1…`); gpt-oss-120b (`b5c939de8f75…`) |
| Oracle | `VLLM_BATCH_INVARIANT=1`, temperature 0, concurrency 1. Llama: `day1/step4/bi_run1.json`, `stage2/base2b/bi_run1.json`. gpt-oss: `stage2/gptoss_ref/base_b16_all.json`, `pc256_*` (verdict), `pc16_r1_*` (reported) |

## GPU runs (gpu-stage3, 2026-10-01 20:00Z onward): first run, superseded

| Component | Version |
|---|---|
| Host | DigitalOcean gpu-mi300x1-192gb, 1x MI300X (gfx942), Ubuntu 24.04, kernel 6.8.0-138-generic |
| ROCm / HIP | ROCm 10.0.0 / HIP 7.15.26333 (unchanged since Day 1) |
| Container image | `lmcache-rocm:day1` (`sha256:50c62263906e…`), container `lmc-c` (all sessions); `aero-kvsink` (kv-sink server) from the same image |
| torch | 2.12.0+rocm10.0.0 |
| vLLM | 0.27.1.dev5+gf46a9dfe2.d20260827; async scheduling on (the default: no disabling condition applies, no warning logged) |
| LMCache | `prototype-stage1`; box tree pinned at `d872268c` at start, then fast-forwarded only for harness commits (`functional/` only, checked with `git diff --stat`). Product code (`lmcache/`, `csrc/`, `rust/`, `setup.py`) unchanged since `00cd3eee`; native extensions from Day 1 (`BUILD_WITH_HIP=1 BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1`) |
| kv-sink server (`asd` in `aero-kvsink`, 127.0.0.1:3100-3103) | `feat/kv-sink-fetch-pipelined` @ `512b0c2079eb`, build `8.1.3.0-112-g512b0c207` (fencing, server issue 4) |
| Aerospike C client (LMCache's extension) | stock aerospike-client-c-libuv 7.3.0; Sriram's kv-sink client not switched in |
| Aerospike CE (`aerospike-ce`, 127.0.0.1:3000) | 8.2.0.0, not used by Stage 3 sessions (all L2 traffic goes to kv-sink) |
| RDMA | Soft-RoCE `rxe0` on `lo`, RC, GID index 1, port active_mtu 4096; unchanged |
| Models | meta-llama/Llama-3.1-8B-Instruct (snapshot `0e9e39f249a1…`); openai/gpt-oss-120b (snapshot `b5c939de8f75…`) |
| Oracle | `VLLM_BATCH_INVARIANT=1`, temperature 0, batch size 1. Llama: `day1/step4/bi_run1.json`, `stage2/base2b/bi_run1.json`. gpt-oss: Stage 2c `stage2/gptoss_ref/base_b16_all.json` (no-hit and whole-prompt hits), `pc256_*` (proper-prefix hits, verdict) and `pc16_r1_*` (reported) |

## CPU prep (T-RDMA-06 CPU half and harness dry runs, 2026-10-01)

| Component | Version |
|---|---|
| Host | Ubuntu 24.04.4 LTS, kernel 6.8.0-138-generic, 20 vCPU, MI300X (not used by this item) |
| Container for LMCache code | `lmc-c`, image `lmcache-rocm:day1` (`sha256:50c62263906e…`); CPU only (`HIP_VISIBLE_DEVICES=` `CUDA_VISIBLE_DEVICES=`) |
| torch / HIP / vLLM (installed; vLLM not started) | 2.12.0+rocm10.0.0 / 7.15.26333 / 0.27.1.dev5+gf46a9dfe2 |
| LMCache code under test | `/work/LMCache-cpu` at `1f01fc84` (prototype-stage1) with the Day 1 native extensions, plus this item's test and harness changes, since committed as `eeb0f402` (test) and `614368d5`/`ec8ae2b1` (harness); no product code changed |
| Aerospike C client (LMCache's extension) | aerospike-client-c-libuv 7.3.0 (ubuntu24.04, x86_64), `/work/deps/aerospike-install` |
| kv-sink server (`asd` in `aero-kvsink`, 127.0.0.1:3100-3103) | `feat/kv-sink-fetch-pipelined` @ `512b0c2079eb`, build `8.1.3.0-112-g512b0c207` (fencing; see `KVSINK-SERVER-BUILD.md`) |
| RDMA | Soft-RoCE `rxe0` on `lo`, RC, GID index 1 (`::ffff:127.0.0.1`), MTU 4096; unchanged |
| Corpus (T-RDMA-06 keys) | `/work/functional/corpus/corpus_llama-3.1-8b-instruct.json`; model name = Llama-3.1-8B-Instruct snapshot `0e9e39f249a16976918f6564b8830bc894c89659` |
| Llama-3.3-70B-Instruct (for Stage 6) | downloaded to `HF_HOME=/work/hf`, snapshot `6f6073b423013f6a7d4d9f39144961bfbfbc386b`, 30/30 safetensors shards, 132 GB |
