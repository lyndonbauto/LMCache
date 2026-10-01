# Stage 6, GPU half: harness and runbook

`stage6gpu.sh` runs the E4 end-to-end tests that need vLLM (functional test plan 5.6-5.9):
T-SHR-01/02/03, the GPU halves of T-FLT-02/03/04 and T-EVT-06, T-EVT-04, T-E2E-11 and
(only after approval) T-FLT-08. It reuses Stage 3's `session`/`report`/`group` helpers, so
each section is one or more `run_steps.sh` sessions in `lmc-c` with results under
`/root/lmc-work/functional/stage6/gpu/<section>/`.

**Run it after Stages 4-5, on a tree that has the D-14 fix (e9cd0689) built in `lmc-c`.**

```bash
# on the box, nothing else on the GPU or on ports 8000/8001/6555/6556/8080/8081/3100/3300-3323
cd /root/lmc-work/LMCache && git log -1 --oneline        # contains e9cd0689
setsid nohup bash functional/stage6/stage6gpu.sh > /root/lmc-work/functional/stage6/gpu/run.txt 2>&1 &
# later, separately:
bash functional/stage6/stage6gpu.sh e2e11 e2e11c          # 70B, about 1.5 h with the baseline
FLT08_APPROVED=1 bash functional/stage6/stage6gpu.sh flt08   # only after approval (see below)
```

Progress lines go to stage3.sh's progress file. Every section ends with one `progress` line
that holds the evidence: the report's verdict line, the integrity verdicts, and fault times.

## Host B: two LMCache servers and two vLLMs on one MI300X

"Host B" is a second LMCache server and a second vLLM on the same GPU. `run_steps.sh`
gained these steps (steps written for one host work as before):

| step | does |
|---|---|
| `server2`, `restart2 [downtime=]`, `kill9_2`, `server2_up` | server B on 6556 (HTTP 8081, Prometheus 9091), log `lmcache2_<tag>.log`; flags `SERVER2_FLAGS` (default: same as A) |
| `vllm2 model= lmc=2` | vLLM on 8001 attached to server B (`lmc=1` attaches it to A) |
| `send ... port=8001` | send to vLLM B; LMCache metrics come from B's HTTP port |
| `integrity name= ids= [mode=whole\|present]` | `l2_integrity.py` (below) |
| `l2stats name=` | namespace statistics, summed over every node |
| `l2evict name= prompt= chunk=`, `l2evict_go name=` | load and arm an evictor, then release it (T-EVT-04) |
| `host_wait`, `vllm_check name= port=` | wait for an armed host action; check one vLLM |

Both vLLMs use `--gpu-memory-utilization 0.3` (`TWO_VLLM_UTIL`): 2 × 57.6 GiB, each with
about 40 GiB for KV after the 15 GiB of weights. **This is untested**: Stage 4 has not yet
run two vLLMs on one GPU. If the second vLLM fails to start, set `TWO_VLLM_UTIL=0.25`.
The two servers do not collide: the L1 shm is named per PID, the layer-progress shm uses a
random instance id, and every port is separate.

New host actions in `host_actions.sh` (armed on `lookup_start` / `lookup_end` /
`retrieve_start`, `log=2` for server B): `cluster_kill node=N`, `cluster_restart node=N`,
`netem_on secs=`, `netem_off`. `kvsink_restart` honours `KVSINK_CONF_FILE`.

## Sections and pass criteria

All outputs must equal the batch-invariant baseline (`report`'s verdict line) unless the
row says otherwise. "No errors" means no Traceback or ERROR in either LMCache log or
either vLLM log.

| section | test | L2 | pass when |
|---|---|---|---|
| `shr01` / `shr01c` | T-SHR-01 | kv-sink cap 4 / cluster RF 1 | B (empty L1) gets the full prefix of A's P-exact-10..14 and P-shared from L2 (`l2diff` shows B reading); 4-chunk prompts `pipelined` on kv-sink; no errors |
| `shr02` / `shr02c` | T-SHR-02 | kv-sink / cluster RF 2 | 3 rounds of both hosts storing P-exact-00..14 at once: every `integrity` round `PASS` (each object whole, from one writer, no orphan or missing records); read-back on both hosts exact and `pipelined` on kv-sink. `client_write_error` > 0 is the evidence the race happened (a lost create-only meta write) |
| `shr03` | T-SHR-03 | kv-sink | A's server is SIGKILLed while B's retrieves are in flight: B's outputs exact and `pipelined`, both vLLMs alive, A serves again after restart |
| `evt06` (`evt06c` is a contrast run and is only recorded) | T-EVT-06 GPU half | cluster RF 2 | client LRU off on both hosts: `integrity mode=present` PASS after both stores and at the end, no L2 deletes, cross-host reads exact |
| `flt02` | T-FLT-02 | cluster RF 1 | n3 SIGKILLed at the start of a 64-chunk prefetch: the victim request completes (recompute), engine alive, later requests exact (hits not checked) |
| `flt03` | T-FLT-03 | cluster RF 2 | same with n2: afterwards every hit is served (hit check on) |
| `flt04` | T-FLT-04 CE half | cluster RF 1 | n2 restarted gracefully during the prefetch; afterwards every entry is back (hit check on) |
| `flt04k` | T-FLT-04 kv-sink half | kv-sink | kv-sink restarted under a live LMCache: the stale-registration reread falls back and is exact; after an LMCache restart `rereg` is `pipelined` |
| `evt04` | T-EVT-04, L1 pressure | kv-sink cap 4 | L1 3 GB, `--max-gpu-workers 2`: vLLM B stores P-long while vLLM A reads 4-chunk L2 hits, 3 rounds: outputs exact, no crash, 6/6 engine checks alive, each outcome in {pipelined, fell_back, refused, failed, not_deferred} |
| `evt04l2` | T-EVT-04, L2 records gone | kv-sink cap 4 | all segments of chunk 3 deleted at `MP retrieve start` (evictor pre-armed): the fetch completes or falls back, outputs exact, later fetches `pipelined` |
| `e2e11` / `e2e11c` | T-E2E-11 | 64 GiB kv-sink / cluster | below |
| `flt08` | T-FLT-08 | kv-sink | below; refused without `FLT08_APPROVED=1` |

**T-FLT-05 is not repeated here.** It needs no second host and is `stage5.sh flt05`, which
runs it under both policies.

Notes for the GPU worker:

- In `flt04k`, if `reread` shows `not_deferred`, the chunk was still in L1. Rerun with a
  smaller L1 (`L1_GB_S=3`).
- In `evt04`, whether an L1 eviction actually overlaps a pipelined fetch depends on
  timing. The progress line counts eviction lines and outcomes. If no outcome other than
  `pipelined` appears and there are no eviction lines, record it as "no overlap", not as a
  pass.
- At RF 2, `l2stats`/settle counts include replicas (twice the number of records).

## T-E2E-11: Llama-3.3-70B at TP=1

Facts, checked in the dry run from the cached `config.json`: 80 layers, 8 KV heads ×
128, bf16. **One 256-token chunk is 80 MiB = 160 slots of 512 KiB**, so D-12's 256-slot
command limit allows **1 chunk per pipelined command** (`CAP70=1`; the 8B allows 4). KV
is 320 KiB per token, so one 17,408-token sequence is 5.3 GiB.

- **GPU memory.** Weights are about 131.5 GiB. At `--gpu-memory-utilization 0.92` the
  instance gets 176.6 GiB, which leaves about 35-40 GiB for KV after activations (about
  110k tokens). Sends are batch 1, so this is enough. fp8 is only a fallback, and it would
  need its own baseline.
- **Corpus.** The 70B tokenizer gives token-identical prompts for every set (dry run:
  P-exact 20/20, P-ragged 20/20, P-shared 10/10, P-multi 50/50, P-long 10/10), so the 8B
  stage2b corpus is reused. The baseline is new (`base70`, made by `run_baseline.sh`
  if missing).
- **kv-sink: one server, not three.** P-exact plus P-ragged is 552 chunks, or 43 GiB, more
  than the 16 GiB namespace. `configs/aerospike-kvsink-70b.conf` is the same server with
  `data-size 64G`, which pins 64 GiB of host RAM while it runs, so the section restarts
  the normal server afterwards. Three kv-sink processes would form a cluster, and N1
  refuses pipelined fetches on a cluster. One adapter also cannot spread its keys over
  three separate single-node clusters. So one large node is the only layout that
  pipelines.
- **Scope.** T-E2E-04: prompts of at most `CAP70` chunks must be `pipelined`; longer
  ones take the plain path. T-E2E-06 (P-shared) is on kv-sink, and also on the cluster
  (`e2e11c`, plain path).
- **Options if `CAP70=1` is too narrow** (decision 3 in the result): (a) keep 1 and
  report it; (b) fix D-12 first (split commands at 256 slots), then rerun with
  `CAP70=4`; (c) leave out the 64-chunk prompts to save time and storage.

## T-FLT-08: 5% packet loss for 60 s (needs a host change)

The test plan sets its lowest environment at E5a. Here it can only be approximated with
`tc netem`, and rxe0 is bound to `lo`, so the loss has to go on loopback.
`flt08_netem.sh plan` prints the exact commands:

```text
# FLT08_MODE=rdma (default): only RoCEv2 (UDP dport 4791)
tc qdisc add dev lo root handle 1: prio bands 4
tc qdisc add dev lo parent 1:4 handle 40: netem loss 5%
tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip dport 4791 0xffff flowid 1:4
# FLT08_MODE=all: every loopback packet
tc qdisc add dev lo root handle 1: netem loss 5%
# remove (a detached watchdog runs it after the window; also on abort)
tc qdisc del dev lo root
```

**Blast radius.** This loads `sch_prio`, `sch_netem` and `cls_u32`, which are not loaded
now. While the rule is in place, every loopback packet on the box passes the qdisc:
Aerospike, kv-sink, LMCache ZMQ, vLLM HTTP, and the ssh port forwards that end on lo.

- In `rdma` mode only RoCE packets are dropped, but that includes **every** RDMA user on
  the box. So no other session can be running, and the GPU half has to run alone.
- In `all` mode, TCP retransmits make every local service slow for 60 s.
- If the watchdog dies, the loss stays until someone runs `flt08_netem.sh remove`.

**Options.**

1. Approve the `rdma` mode on an idle box, then run
   `FLT08_APPROVED=1 stage6gpu.sh flt08`.
2. Approve the `all` mode, which is closer to "network loss" but disturbs everything.
3. Leave T-FLT-08 N/A here, since its lowest environment is E5a: run it on real NICs.

`apply` refuses unless `FLT08_APPROVED=1` and lo's root qdisc is `noqueue`.

## Dry run (`stage6gpu.sh dry` = `scripts/dry_s6gpu.sh`)

The dry run is CPU only and uses private servers: a CE cluster `s6p-n1..3` on
3600/3610/3620, a kv-sink container `kvsink-s6p` on 3500-3503, and LMCache in `lmc-d` on
6755/6756. Before each start it checks `docker ps` and `ss` again, it never touches the
default ports, and it removes everything at the end. Results from 2026-10-01:

- **Syntax.** Every script passes `bash -n`, and the Python compiles.
- **netem.** `plan` and `check` work; `apply` is refused without approval.
- **Configs.** All five adapter specs parse: the cluster spec, the eviction contrast, and
  kv-sink windows of 4 × 32 MiB, 1 × 80 MiB and 2 × 80 MiB.
- **70B.** The model facts and the corpus identity shown above.
- **Two LMCache servers on the private cluster.** `server`, `server2`, `restart2`,
  `kill9_2`, `server2_up` and `restart` all worked, with no errors in either log.
- **Two RC servers on the private kv-sink.** Both reported `rdma=RC` at cap 4 (8B) and
  at cap 1 (70B), with no errors.
- **Synthetic D-14 objects.** `l2_integrity.py whole` PASSes on clean objects and FAILs
  with an added orphan (1 orphan reported). `evict --when` deleted 3/3 segments 0.4 ms
  after release, and `present` then FAILs with 1 partial object. Settle sums over the
  nodes.
- **Host actions.** `cluster_kill node=3` armed on `lookup_start` fired 1 ms after the
  trigger line, and the cluster re-formed at size 2. `cluster_restart` brought it back to
  size 3.

What the dry run cannot cover: vLLM, two vLLMs on one GPU, real pipelined fetches, and
the evictor racing a real retrieve.
