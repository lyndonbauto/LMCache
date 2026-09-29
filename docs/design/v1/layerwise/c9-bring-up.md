# C9 bring-up: the first pipelined retrieve on a GPU over Soft-RoCE

A runbook for the first real run of the pipelined retrieve
([c9-wiring.md](c9-wiring.md)). It needs one Linux box with a GPU, Soft-RoCE
and an Aerospike `kv-sink` server that writes by RDMA. The goal is one
request whose L2 hit is served layer by layer (`pipelined_outcome=pipelined`)
with output identical to a run without LMCache. TTFT on EFA comes later.

Each stage adds one moving part, so a failure points at that part.

## Build

```bash
# CUDA
BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1 \
  pip install -e . --no-build-isolation
# ROCm (for example the MI300X functional test box)
BUILD_WITH_HIP=1 CXX=hipcc BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1 \
  pip install -e . --no-build-isolation
```

`BUILD_WITH_AEROSPIKE_RDMA` needs `rdma-core` (`libibverbs`). On ROCm, the
HIP build together with the Aerospike RDMA flags has not been tried before,
so a build failure here is a finding, not a setup mistake.

## Stage 0: CPU checks on the box

Run these before touching the GPU. They cover all three tracks without a
fabric:

```bash
pytest -q tests/v1/layerwise/test_three_track_retrieve.py \
  tests/v1/layerwise/test_storage_manager_placer.py \
  tests/v1/multiprocess/test_lmcache_driven_deferred_retrieve.py \
  tests/v1/multiprocess/test_pipelined_loading.py
```

`test_three_track_retrieve.py` runs Track A's storage manager, placer and
source over the fabric-free client, Track C's `fetch_deferred_objects`, and
Track B's sink staging real bytes. It skips if the fabric-free module cannot
be built (it needs `make`, a C++ compiler and `pybind11`).

Then, with the GPU visible, check that a layer event recorded in one process
orders reads in another. The worker's per-layer wait depends on it, and
nothing else orders the daemon's copies against vLLM's attention:

```bash
pytest -q tests/v1/platform/test_event_ipc.py \
  tests/v1/platform/test_event_ipc_ordering.py \
  tests/v1/multiprocess/test_object_group_layerwise_transfer.py
```

This is the first run of the layerwise path across two processes on any
GPU. Track B's GPU runs were single-process under WSL2, which cannot share
GPU events between processes. ROCm reuses the CUDA backend (PyTorch
interprocess events, which become HIP IPC events) and compiles the native
range copy as `hipMemcpyAsync`; none of that has run either. If the ordering
test fails or skips, stop. Every layerwise stage after this one would read
KV blocks before they are written, which shows up as wrong output, not an
error.

## Stage 1: Soft-RoCE

```bash
sudo modprobe rdma_rxe
sudo rdma link add rxe0 type rxe netdev lo
ibv_devinfo -d rxe0
export RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
pytest -q tests/v1/distributed/rdma/
```

The RDMA suite runs Track A's native pipeline against `rxe0`, against its
own mock writer.

Then build and start the Aerospike server from the aerospike-server branch
`feat/kv-sink-fetch-pipelined`, following
["Running A8 against a real server"](../distributed/l2_adapters/aerospike_rdma.md#running-a8-against-a-real-server)
(stock CE has no `kv-sink-*` commands). Run A8 against it:

```bash
RUN_AEROSPIKE_INTEGRATION=1 AEROSPIKE_TEST_HOST=127.0.0.1 \
  AEROSPIKE_TEST_PORT=3000 AEROSPIKE_TEST_NAMESPACE=lmcache \
  RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1 \
  pytest -q tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py
```

A8 registers the windows with the real server, runs a clean pipelined fetch
and a missing-record fallback, and checks the bytes of both. It must pass,
not skip, before Stage 3. Record the server commit.

## Stage 2: GPU, layerwise, no RDMA

Start with the pipelined flags on but no RDMA adapter. This proves the flags
and the sink factory do not break today's layerwise path:

```bash
lmcache server --port 6555 --http-port 8080 \
  --l1-size-gb 8 --eviction-policy LRU --chunk-size 256 \
  --use-layerwise --pipelined-fetch \
  --l2-adapter '{"type":"aerospike","hosts":"127.0.0.1:3000"}'
```

```bash
vllm serve <model> --port 8000 --no-enable-prefix-caching \
  --kv-transfer-config '{"kv_connector":"LMCacheMPConnector","kv_role":"kv_both","kv_connector_extra_config":{"lmcache.mp.host":"tcp://localhost","lmcache.mp.port":6555,"lmcache.mp.use_layerwise":true}}'
```

Expect a `Cannot fetch <model> layer by layer` warning at registration (no
RDMA adapter), and `pipelined_outcome=not_deferred` on every retrieve.
`--no-enable-prefix-caching` makes every repeat of a prompt go to LMCache.

## Stage 3: the pipelined path

Add the RDMA block. `window_bytes` must hold `--pipelined-max-chunks`
chunks. For Llama-3-8B at 256-token chunks, one chunk is 32 MiB
(32 layers × K and V × 8 KV heads × 128 × 2 bytes × 256 tokens), so start
small:

```bash
lmcache server --port 6555 --http-port 8080 \
  --l1-size-gb 8 --eviction-policy LRU --chunk-size 256 \
  --use-layerwise --pipelined-fetch --pipelined-max-chunks 4 \
  --l2-adapter '{"type":"aerospike","hosts":"127.0.0.1:3000",
    "rdma":{"transport":"RC","device_name":"rxe0","gid_index":1,
            "window_count":2,"window_bytes":134217728}}'
```

`fetch_timeout_seconds` (default in `L1RdmaConfig`) must stay below
`--l1-write-ttl-seconds`; startup checks it.

**Confirm registration.** The log must show:

```text
<model> fetches layer by layer from L2 adapter 0, reading records of at most <N> bytes
```

Any warning instead (`No pipelined sink is installed`, `serves world size 1
only`, `Cannot fetch ... layer by layer` with a traceback) means every
lookup takes today's path; the traceback names the check that refused.

**Get a pure L2 hit.** Send a prompt of at most 4 full chunks (so under
1024 tokens here), wait for the store, then restart `lmcache server` (L1 is
lost, L2 keeps the records) and vLLM, and send the same prompt again.

**Confirm the path.** With `LMCACHE_LOG_LEVEL=DEBUG` the daemon logs

```text
MP retrieve end: session=... retrieved_count=... pipelined_outcome=pipelined
```

and `http://localhost:8080/metrics` shows
`lmcache_mp_num_deferred_retrieves_total{outcome="pipelined"}`.

**Confirm correctness.** With greedy sampling (`temperature=0`), the
completion must match the same prompt against vLLM without LMCache.
`fell_back` must also match; it is served, only slower.

## Triage

| `pipelined_outcome` | Look at | Usual cause |
|---|---|---|
| `not_deferred` on an L2 hit | Registration log | Model not registered (see above); prompt over `--pipelined-max-chunks`; or the hit was in L1, not L2 |
| `no_source` | `No pipelined fetch` warning | The RDMA adapter's pipelined path is not ready (native client built without RDMA, or windows not registered) |
| `refused` | `Pipelined fetch refused` warning | Window busy (all `window_count` leased, or quarantined after a failure for `fetch_timeout_seconds`); plan over the slot cap |
| `fell_back` | `Pipelined fetch failed on layer N` warning | The server declined a slot or a layer timed out; records written under another `max_record_bytes` decline every slot |
| `reused` | – | Another request's fetch left the keys in L1 (`retain`); fine |
| `shared_keys_busy` | `--pipelined-shared-keys` | Two requests with one prefix; `wait` trades latency for a recompute |
| `failed` | ERROR log with traceback | Anything else; vLLM recomputes the request |
| `loaded_whole` | `--use-layerwise`, `lmcache.mp.use_layerwise` | Retrieve was not layerwise, or the model has no `PipelinedModel` |

**Wrong output with `pipelined` is the one failure that must stop the run.**
Check in this order:

1. The worker's layer wait timed out silently (see "A generation timeout is
   silent" in [c9-wiring.md](c9-wiring.md), Risks). Look for
   `LayerProgressRetrieveGenerationTimeoutError` in the vLLM log.
2. The record layout the server writes disagrees with the plan's slot
   offsets. Track A's `test_slot_plan_parity.py` pins the layout; rerun it
   against the server's build.
3. Staging copied a layer before it landed. Track B's per-layer staging
   reads the table at launch; a whole-object staging path would read early.
