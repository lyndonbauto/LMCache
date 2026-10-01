# Stage 2a versions

| Component | Version |
| --- | --- |
| Host | DigitalOcean gpu-mi300x1-192gb, 1x MI300X (gfx942), Ubuntu 24.04 |
| Kernel | 6.8.0-138-generic |
| ROCm / amdgpu | ROCm 10.0.0 (unchanged since Day 1) |
| Container image | `lmcache-rocm:day1` (sha256:50c62263906e…), container `lmc-c` |
| torch | 2.12.0+rocm10.0.0 |
| HIP | 7.15.26333 |
| vLLM | 0.27.1.dev5+gf46a9dfe2.d20260827 |
| LMCache | `prototype-stage1`, product code at `00cd3eee` (HEAD when the runs started; later Stage 2a commits change `functional/` only). Native extensions built 2026-09-30 20:34 UTC at `a3456e2c`; no `csrc/`, `rust/` or `setup.py` change since. Built with `BUILD_WITH_HIP=1 BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1` |
| Aerospike C client | 7.3.0 (libuv, ubuntu24.04), the stock client inside `lmc-c`; not the kv-sink client |
| Aerospike Python client (harness only) | 18.1.0 |
| Aerospike server | Aerospike Community Edition 8.2.0.0 (`aerospike/aerospike-server:latest`, sha256:583e882d540f…), one node on 127.0.0.1:3000, namespace `lmcache`, config `functional/configs/aerospike.conf` |
| Model | meta-llama/Llama-3.1-8B-Instruct (snapshot 0e9e39f249a1…) |
| Oracle | `VLLM_BATCH_INVARIANT=1`, temperature 0, batch size 1; baseline `functional/day1/step4/bi_run1.json` (deterministic against `bi_run2.json`, 130/130) |

## Stage 2b

Same host, container, torch, HIP, vLLM, Aerospike server and C client as
above. LMCache: box tree fast-forwarded to `49f18d12` after the runs; the
product code (`lmcache/`, `csrc/`, `rust/`, `setup.py`) is unchanged since
`00cd3eee`, and the harness that ran is `f1a8c56e` + `9879ac44` (reports
rebuilt with `c5a4ff99`). Corpus: spec version 2 (`edc901fc`),
`corpus_llama-3.1-8b-instruct.v2.json`, plus P-prefix in
`corpus_llama-3.1-8b-instruct.stage2b.json` (sha256 `c26fd7f2…`). Second
model: openai/gpt-oss-120b (T-LKP-05). New baseline `stage2/base2b/bi_run1.json`.
