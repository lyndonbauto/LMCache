# Why we are building this

This is the shared statement of intent for the Aerospike layerwise KV cache
work. It is deliberately short. If a design argument cannot be settled by
pointing at something in this document, escalate rather than guess.

## The goal in one sentence

Cut time-to-first-token for prompts whose KV cache already exists somewhere in
an Aerospike cluster, by streaming that cache back over RDMA **one layer at a
time** so the GPU starts computing layer 0 while layer 40 is still in flight.

## Why layer-at-a-time is the whole point

A conventional remote cache fetch is a barrier. The engine asks for a prompt's
KV cache, waits for all of it, then starts the forward pass:

```
  fetch all layers  ####################
  forward pass                          ####################
                    |<-- dead time -->|
  TTFT              |<------------------ total ------------->|
```

Transformer attention consumes layers strictly in order, and it only needs
layer N to compute layer N. So the wait is avoidable. If the transport can say
"layer 0 has landed" before the rest arrives, the two phases overlap:

```
  fetch layers      ####################
  forward pass        ####################
  TTFT              |<----- total ----->|
```

The saving grows with the number of layers and with how slow the fetch is
relative to compute. That overlap is the entire product rationale. Every
design decision in this project should be checked against the question *does
this preserve the overlap?* -- a change that makes the transport faster but
reintroduces a barrier is a regression.

## Why Aerospike, and why RDMA

The KV cache for a long prompt is large and is read far more often than it is
written. Aerospike holds it across a cluster; RDMA moves it into the engine's
host memory without the CPU copying it. Specifically the server issues
`RDMA_WRITE_WITH_IMM` per slot, so each completed write announces itself. That
per-write announcement is what makes "layer 0 has landed" knowable at all --
with a plain bulk transfer there would be nothing to report until the end.

## What "done" means for the exercise

This is a proof of concept. Done is **a defensible number**, not a feature
list. Concretely:

1. A real vLLM run against a real Aerospike cluster over real RDMA, serving a
   prompt whose KV cache is a remote hit.
2. A measured TTFT for that run, next to a measured TTFT for the same prompt
   with layerwise loading disabled, on the same hardware.
3. An honest account of where the remaining time goes.

If the number is disappointing, that is a result and we report it. The failure
mode to avoid is arriving at the end of the project with a large, tidy,
well-tested system and no measurement -- which is roughly where the prototype
stood before this split.

## Where the work stands today

Built and tested without hardware in the loop:

- The RDMA transport, kv-sink wire codec, slot planning, and multi-node fanout
  (`csrc/storage_backends/aerospike/`), exercised end to end against a real
  Aerospike server over Soft-RoCE.
- The multiprocess per-layer GPU load path, CUDA IPC event signalling, and the
  vLLM piecewise-cudagraph negotiation (`lmcache/v1/multiprocess/`).

Not built:

- **The junction between them.** The transport can say a layer has landed; the
  loader can copy a layer to the GPU; nothing connects the two. This is now
  `lmcache/v1/layerwise/`, and it is why the work splits three ways.

Not known:

- Whether EFA consumes a receive work request per `RDMA_WRITE_WITH_IMM`
  immediate. If it does, the wire contract needs a handshake it does not have.
  Only real EFA hardware can answer this. See
  [track-a-acceptance.md](track-a-acceptance.md).
- Any TTFT number at all.

## How the work is divided

Three tracks, described in [system-design.md](system-design.md):

| Track | Owns | Needs |
|---|---|---|
| A -- Transport | getting bytes from Aerospike into host memory, and saying when a layer is complete | RDMA fabric, no GPU |
| B -- Consumption | getting a landed layer onto the GPU and unblocking attention | GPU, no RDMA |
| C -- Planning and junction | deciding what to fetch, and driving A into B | neither |

The tracks meet only at the two interfaces in `lmcache/v1/layerwise/contract.py`.
Those interfaces are frozen: changing one is a three-person conversation, not a
commit.
