# Cost estimate (AIE-86)

us-east-1, Linux, shared tenancy, **on-demand list price**, retrieved from the
AWS Pricing API on 2026-09-14.

## Half A — 5-node cluster + CPU client (no GPU)

| Component | Qty | $/hr each | $/hr |
|---|---|---|---|
| `i3en.24xlarge` Aerospike server | 5 | 10.848 | **54.24** |
| `c5n.18xlarge` load generator | 1 | 3.888 | **3.89** |
| **Instance subtotal** | | | **58.13** |
| EBS gp3 (5x100 + 1x200 GB) | | | ~0.10 |
| Elastic IPs (6, attached) | | | ~0.03 |
| **Total** | | | **~58.26** |

- **Per hour: ~$58**
- **Per 8-hour working day: ~$466**
- **Per 24-hour day: ~$1,398**
- **Per 7-day week: ~$9,790**

Cluster only, without the client: **$54.24/hr**, **$1,302/day**.

## Half B — adding the GPU node

| GPU option | $/hr | Half A + GPU $/hr | + per 24h day |
|---|---|---|---|
| `g6e.12xlarge` (4x L40S) **recommended** | 10.49 | **68.62** | **$1,647** |
| `p4d.24xlarge` (8x A100 40GB) | 21.96 | 80.09 | $1,922 |
| `g6e.48xlarge` (8x L40S) | 30.13 | 88.26 | $2,118 |
| `p5.48xlarge` (8x H100 80GB) | 55.04 | 113.17 | $2,716 |

Terraform's `estimated_hourly_cost_usd` output reports the configured total, so
it is visible in every plan before apply.

## Realistic total for the M0 baseline

Assuming Half A takes two working days including the ~10 TiB flash load, and
Half B takes one:

| Phase | Config | Hours | Cost |
|---|---|---|---|
| Half A — bring-up + EFA verification | 6 nodes | 3 | $175 |
| Half A — flash load (~10 TiB) | 6 nodes | 5 | $291 |
| Half A — ceiling + L2 sweep, both caps | 6 nodes | 8 | $466 |
| Half B — GPU bring-up + TTFT sweep | 7 nodes | 8 | $549 |
| **Total** | | **24** | **~$1,481** |

Add contingency for re-runs. **A realistic budget ask is $2,000–$2,500**,
provided instances are destroyed between sessions.

## Cost controls built in

- `gpu_count` defaults to **0**. Half B cannot be provisioned by accident.
- Everything is tagged `Project=aerospike-lmcache-bench`, `Jira=AIE-86`, so the
  whole stack is findable in Cost Explorer and destroyable by tag.
- A dedicated VPC means `terraform destroy` removes everything with no risk of
  orphans mixed into the shared account's existing VPCs.
- `estimated_hourly_cost_usd` is a Terraform output, shown at plan time.

## The main financial risk

**Idle instances.** At $58/hr, forgetting the cluster over a weekend costs
~$2,800 — more than the entire planned benchmark. The runbook puts teardown
first for this reason.

Spot instances would cut this by 60–70%, but spot capacity for five identical
`i3en.24xlarge` in one cluster placement group is unlikely to hold, and an
interruption mid-measurement invalidates the run. On-demand is the right call
for a short benchmark; revisit if the cluster needs to live for weeks.
