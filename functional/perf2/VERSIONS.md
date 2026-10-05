# Perf rerun with the kv-sink sink-path fixes: versions and run definitions

2026-10-05, on a new MI300X droplet (129.212.177.62). The baseline is the committed
`functional/perf/` (phase 1 and the lw experiments, old droplet). Only the new kv-sink
server and client were built; the old builds were not rerun. LMCache's only product change
for this rerun is `rdma.queue_pairs` (`81288120`); everything else here is `functional/`.

| Component | Version |
|---|---|
| Host | DigitalOcean MI300X droplet (new), 1x MI300X (gfx942, 192 GB), 20 vCPU Xeon Platinum 8568Y+ (1 NUMA node), 235 GiB RAM, Ubuntu 24.04, kernel `6.8.0-138-generic`, amdgpu `6.19.14`, MLNX OFED `24.10-3.2.5` IB core. Same size and kernel/driver as the old droplet |
| Aerospike data | `/dev/vdc1` (5 TB scratch volume) at `/mnt/scratch`, data file `/mnt/scratch/perf-aero/lmcache.dat` |
| Host ROCm | 7.14 (`/opt/rocm/core-7.14`). Its `amd-smi metric --mem-usage` raises `AttributeError`, so `perf.sh` reads VRAM from sysfs |
| Container `lmc-c` | `vllm/vllm-openai-rocm:v0.27.1` (`sha256:bb44b39aea26…`): ROCm 7.2.3 (HIP `7.2.53211`), torch `2.11.0+gitd0c8b1f`, vLLM `0.27.1`, cupy-rocm-7-0 `14.2.0` (added, CHANGES.md) |
| Old box (for comparison) | ROCm 10.0.0, torch `2.12.0+rocm10.0.0`, vLLM `0.27.1.dev5`, image `lmcache-rocm:day1`, which was not saved and cannot be rebuilt. Valentyn chose this image and rebaselining on this box (Slack, "Agent: option 1") |
| LMCache | `prototype-stage-1b` `81288120` ("Aerospike RDMA: rdma.queue_pairs"), on `284f31b5`; version `0.4.6.dev1039`; native extensions built with `BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1` against the client below |
| Aerospike C client | `sriram588/aerospike-client-c-kvsink` `sriram/kv-sink-batch-prio` `5a24afdbb6` ("examples/kv_sink: kvlayers --qps"), on `b852be2eac` (`as_sink_config.queue_pairs`). Old run: `523d51eaa6` |
| kv-sink server | `citrusleaf/aerospike-server` `sriram/kv-sink-batch-prio` `9c16972132` (placement pool, several RC queue pairs per region), on `5af46adaea`; build string `Aerospike Community Edition build 8.1.3.0-113-g9c1697213`; in `aero-kvsink-bp` (ubuntu:24.04, `sha256:534baea6a22c…`), 127.0.0.1:3700-3703. Old run: `046e8558d1` |
| RDMA | Soft-RoCE `rxe0` on `lo` (upstream v6.11 `rdma_rxe` built out of tree for OFED, `functional/HOST-CHANGES.md`), RC, GID index 1, active MTU 4096 |
| Model | `meta-llama/Llama-3.1-8B-Instruct` snapshot `0e9e39f249a16976918f6564b8830bc894c89659`, bf16, TP=1 (token placed on the box by Valentyn, Slack "Agent: option 2") |

## Run definitions

As `functional/perf/VERSIONS.md` (prompts, output, load, metrics, hit check, warm-up, LMCache
server flags, connector, window sizing, L1 sizes, cached point procedure), except:

| Item | Value |
|---|---|
| KV cache capacity (vLLM log) | 1,266,672 tokens (nocache); 1,275,776 tokens (aon, lw) |
| CUDA graphs | `FULL_AND_PIECEWISE` in nocache and aon; `PIECEWISE` with layerwise on (lw), as before |
| `queue_pairs` | `rdma.queue_pairs` in the lw L2 adapter JSON (`perf.sh` `QUEUE_PAIRS`): RC queue pairs per kv-sink node. Step 4 scans 1 / 4 / 8 / 16; step 6 runs at 16 |
| Data file | `filesize` = 32 prompts x KV x 2.0 (`FS_PCT=200`): 64G at 8k, 128G at 16k; every store used about 50% of it |
| Queue-pair scan (step 4) | Per length, one data file: aon store of prompts 0-31 (8 in flight) and aon points c = 1, 2, 4; then lw points c = 1, 2, 4 per `queue_pairs` (LMCache restarted before every point, as before). Sessions `L<len>_aon`, `L<len>_lw_qp<n>` |
| E3 timeline | lw 8k c=1, n=4, per `queue_pairs`: a box-only print patch in `lmcache/v1/layerwise/pump.py` (time of `begin_fetch` and of each layer becoming resident), applied before and reverted after with `git checkout --`; `git status` of `lmcache/` and `csrc/` checked clean after. asd threads sampled with `top -H -d 0.2`. Sessions `E_timeline_qp<n>` |
| Memory namespace (step 5) | `functional/configs/aerospike-kvsink-bp-perf-mem.conf.in`: the same server stanza with `storage-engine memory { data-size 16G }`; prompts 0-3 stored; aon c=1, lw c=1 and E3 at `queue_pairs` 1 and 8. Results in `memns/` |
| Full sweeps (step 6) | `perf.sh cached:<L> lwwait:<L>` for 8k and 16k, c = 1..32, and the lw experiments' `exp_*` sections (E4 partial hits, E2 tcp-lw), all at `queue_pairs` 16. Results in `sweep/` |
