# Half B — LMCache TTFT breakdown (GPU required, DEFERRED)

**Needs a GPU node. Adds $10.49–$55.04/hr on top of Half A. `gpu_count`
defaults to 0. Do not provision until Half A is complete.**

## Why this is deferred, and what it is actually for

Half B produces one number that bounds the entire RDMA project:

> **What fraction of Time To First Token is spent fetching from L2 at all?**

That fraction is the theoretical maximum benefit of making L2 fetch
infinitely fast. If L2 fetch is 15% of TTFT, then a perfect RDMA transport that
reduces fetch time to zero improves TTFT by at most 15%, and the project needs
to be justified on that basis. Getting this number is cheap relative to
building the transport, and getting it *before* building the transport is the
whole point of an M0 baseline.

Half A does not need a GPU and answers a different, also-gating question, which
is why the two are separated.

## Method

### Mode

Use **LMCache MP (multiprocess) mode**. This is the recommended path and it is
the one that emits the observability spans the breakdown depends on.

**Do not set `LMCACHE_USE_LAYERWISE`.** That selects the deprecated in-process
mode, which does not produce the MP spans. The benchmark would run to
completion and yield nothing attributable.

### Instrumentation

LMCache's MP observability subsystem (`lmcache/v1/mp_observability/`, documented
in `docs/design/v1/mp_observability/METRICS.md`) emits spans including:

- `MP_LOOKUP_PREFETCH` — the **control path**: deciding what is cached and
  initiating the prefetch.
- `MP_RETRIEVE` — the **data path**: actually moving KV cache bytes into GPU
  memory. This is the span RDMA would shrink.

The coordinator exposes `GET /metrics`. Scrape it before and after each
measurement window.

### Attribution

Decompose TTFT into three buckets at both p50 and p99:

| Bucket | Source | What RDMA affects |
|---|---|---|
| Lookup / prefetch (control) | `MP_LOOKUP_PREFETCH` | mostly not — this is metadata and round trips, though fewer segments helps |
| Retrieve (data) | `MP_RETRIEVE` | **this is the target** |
| Everything else | TTFT minus the above | not at all — prefill compute, scheduling, tokenisation, queueing |

Report each as an absolute duration and as a percentage of TTFT. The headline is
`MP_RETRIEVE / TTFT` at p50 and p99.

Percentiles must be computed over the same request population, not by combining
independently-derived percentiles — p99 TTFT and p99 retrieve are generally not
the same request, so the buckets will not sum exactly at p99. Report that
honestly rather than forcing them to reconcile.

### Conditions to sweep

The retrieve share is not one number, it varies with the thing being fetched:

- **Cache hit rate**: 0% (pure prefill, the floor) and ~100% (full reuse, the
  ceiling). The interesting number is at realistic partial hit rates.
- **Context length**: the L2 share grows with context, because prefill compute
  and fetched bytes scale differently. Sweep short/medium/long prompts.
- **Object size**: ties directly back to Half A. If the `do_single_get()`
  serial-segment chain is the dominant cost inside `MP_RETRIEVE`, that shows up
  here as retrieve latency tracking segment count rather than bytes — and it
  means part of the apparent "RDMA opportunity" is actually a client-side
  fix that costs nothing.

That last point is the one most likely to change the project's conclusion, so
run Half A first and carry its findings in.

### Placement

The GPU node joins the same subnet, AZ, security group and cluster placement
group as the Aerospike servers, so the network path is the one the RDMA design
targets. `terraform/instances.tf` already does this — set `gpu_count = 1`.

## Instance choice

See `docs/instance-selection.md` for the full table. Default is
`g6e.12xlarge` (4x L40S 48 GB, EFA, 100 Gbps, $10.49/hr): the breakdown is a
ratio, not an absolute-performance claim, so it does not require a
frontier-scale model. Move to `p4d.24xlarge` or `p5.48xlarge` only if the model
under test does not fit.

## Enabling it

```bash
cd terraform
terraform plan  -var gpu_count=1                 # confirm: 33 to add
terraform apply -var gpu_count=1
```

Then install vLLM + LMCache on the node and configure the Aerospike L2 adapter
with the seed hosts from `terraform output aerospike_seed_hosts`.

**Set `gpu_count` back to 0 the moment the measurement window closes.** An idle
GPU node is the single most expensive way for this project to waste money.
