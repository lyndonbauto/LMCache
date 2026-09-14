# aerospike-lmcache-bench

Infrastructure and runbook for **AIE-86** — baseline measurement of the LMCache
TTFT breakdown and the Aerospike storage ceiling, ahead of the RDMA server-push
work in epic AIE-85.

> ## NOTHING HAS BEEN APPLIED
>
> This repository contains a validated Terraform configuration and a successful
> `terraform plan` **only**. No AWS resources have been created. No billable
> action has been taken. See [Approval required](#approval-required).

---

## TEARDOWN — READ THIS FIRST

At **$58/hour**, forgetting this cluster over a weekend costs ~$2,800, which is
more than the entire planned benchmark. Teardown is documented first on purpose.

```bash
cd terraform
terraform destroy
```

Then **verify it actually worked** — a failed destroy that leaves instances
running is the expensive case:

```bash
aws ec2 describe-instances \
  --filters Name=tag:Project,Values=aerospike-lmcache-bench \
            Name=instance-state-name,Values=running,pending,stopping,stopped \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]' --output table

# Must also be empty -- NAT gateways and unattached EIPs bill after the
# instances are gone.
aws ec2 describe-nat-gateways \
  --filter Name=tag:Project,Values=aerospike-lmcache-bench \
  --query 'NatGateways[?State!=`deleted`].[NatGatewayId,State]' --output table
aws ec2 describe-addresses \
  --query 'Addresses[?!AssociationId].[PublicIp,AllocationId]' --output table
```

Every resource is tagged `Project=aerospike-lmcache-bench` and `Jira=AIE-86`, so
anything orphaned is findable.

**Between sessions, destroy rather than stop.** Stopped instances do not bill for
compute, but `i3en` local NVMe is ephemeral — stopping loses the ~10 TiB flash
dataset anyway, so there is nothing to preserve.

---

## What is here

| Path | Contents |
|---|---|
| `terraform/` | VPC, placement group, EFA security group, 5 servers + client (+ optional GPU) |
| `terraform/templates/` | `aerospike.conf` and per-role user-data |
| `docs/instance-selection.md` | Instance choice with live API evidence; the AMD question |
| `docs/aerospike-tuning.md` | `write-block-size` / `max-record-size` reasoning |
| `docs/half-a-storage-ceiling.md` | Half A method (no GPU, cheap, run first) |
| `docs/half-b-ttft.md` | Half B method (GPU, expensive, deferred) |
| `docs/cost-estimate.md` | Per-hour and per-day cost |
| `docs/findings.md` | **Things that contradict the plan's assumptions** |
| `docs/soft-roce.md` | Zero-cost local RDMA testing without EFA hardware |

The LMCache fork at `/home/lyndon/github/LMCache` is **not modified** by
anything here.

---

## Prerequisites

- Terraform >= 1.6 (validated on 1.16.2)
- AWS CLI authenticated to account `236318833413`, region `us-east-1`
- The Session Manager plugin, for node access:
  <https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html>
- An existing EC2 key pair name (only needed if you add a bastion; SSM does not
  require it)

Nodes have **no public IP addresses**. Access is via SSM Session Manager. This
is deliberate — see finding #2 in `docs/findings.md`.

---

## Applying

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars        # set operator_cidr and key_name

terraform init
terraform validate
terraform plan -out=bench.tfplan
```

Check the plan output before applying:

- `Plan: 32 to add, 0 to change, 0 to destroy`
- `estimated_hourly_cost_usd` reads `58.13 USD/hr`
- `gpu_private_ips = []` — the GPU node is **not** being created

Immediately before applying, re-check the shared account for capacity taken by
other teams:

```bash
aws ec2 describe-instances --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceType' --output text
```

Then:

```bash
terraform apply bench.tfplan
```

### If apply fails with `InsufficientInstanceCapacity`

A cluster placement group asks AWS to place five identical `i3en.24xlarge`
physically close together, which is a realistic failure. In order of preference:

1. Retry — capacity fluctuates.
2. Try another AZ that offers `i3en.24xlarge` (a, b, c, f) with
   `-var availability_zone=us-east-1b`. Note this forecloses some Half B GPU
   options; see `docs/instance-selection.md`.
3. Fall back to `i4i.32xlarge` (`-var server_instance_type=i4i.32xlarge`), at
   the cost of dropping from 100 Gbps to 75 Gbps.

Terraform creates all instances in one apply, so a capacity failure can leave a
partial cluster **that is billing**. Run `terraform destroy` before retrying.

---

## Verification — run all of these before trusting any number

### 1. Bootstrap finished

```bash
aws ssm start-session --target <instance-id>
sudo cat /var/log/bench-bootstrap-done      # exists only on success
sudo tail -50 /var/log/cloud-init-output.log
```

### 2. EFA is present and functional

The most common EFA failure is silent, so check all three layers.

**Device enumerated:**

```bash
fi_info -p efa
# Expect at least one provider entry with fabric: EFA-<...>. Empty output means
# the EFA driver did not install -- check /var/log/efa-installed-version.

ibv_devinfo
# Expect a device (rdmap*) in state PORT_ACTIVE. PORT_DOWN means the ENI is not
# an EFA interface.
```

**Loopback RDMA on a single node** — proves the local stack works before any
network is involved:

```bash
fi_pingpong -p efa &        # server on localhost
sleep 2
fi_pingpong -p efa 127.0.0.1
```

**Node-to-node RDMA** — this is the test that catches the security group
problem. Run the server on one node and the client on another:

```bash
# on 10.240.1.10
fi_pingpong -p efa -e rdm

# on 10.240.1.11
fi_pingpong -p efa -e rdm 10.240.1.10
```

> **If loopback passes but node-to-node hangs or fails, check the security
> group's self-referencing EGRESS rule first.** EFA requires the SG to allow all
> traffic both to and from itself. Without the egress half, the device still
> enumerates, `ibv_devinfo` still looks healthy, the connection appears to
> establish, and then RDMA writes fail with nothing pointing at the cause. A
> generic `0.0.0.0/0` egress rule does not substitute for it. Both rules are in
> `terraform/network.tf` with comments saying not to remove them.

```bash
aws ec2 describe-security-group-rules \
  --filters Name=group-id,Values=$(terraform output -raw security_group_id) \
  --query 'SecurityGroupRules[].[IsEgress,IpProtocol,ReferencedGroupInfo.GroupId,CidrIpv4]' \
  --output table
# There must be a row with IsEgress=True, IpProtocol=-1, and the SG's own ID.
```

### 3. Aerospike cluster formed as **one** cluster of five

```bash
asadm -h 10.240.1.10 -e info
```

`Cluster Size` must read **5 on every node**. Mesh misconfiguration produces
five single-node clusters that each individually report as healthy, so check
more than one node:

```bash
for ip in 10.240.1.{10,11,12,13,14}; do
  echo -n "$ip cluster-size: "
  asinfo -h $ip -v 'statistics' | tr ';' '\n' | grep '^cluster_size='
done
```

### 4. The record cap LMCache will actually discover

Gating step — do not record benchmark numbers until this passes.

```bash
asinfo -h 10.240.1.10 -v 'namespace/lmcache' | tr ';' '\n' \
  | grep -E 'max-record-size|write-block-size'
```

Both must read `8388608` (8 MiB). If `max-record-size` reads `1048576`, the
tuning has silently not taken effect and the sweep will produce the untuned
numbers — see finding #4 in `docs/findings.md`.

---

## Running the benchmarks

**Half A first.** It needs no GPU, costs ~$58/hr, and answers the question that
gates the whole RDMA project: can flash sustain NIC line rate? Full method in
[`docs/half-a-storage-ceiling.md`](docs/half-a-storage-ceiling.md).

```bash
# Raw cluster ceiling, LMCache not in the path
asbench --hosts $LMCACHE_AEROSPIKE_HOSTS --namespace lmcache ...

# LMCache L2 adapter size sweep, 128 KiB -> 80 MiB
lmcache bench l2 --l2-adapter "$SPEC" --data-size-kb 81920 --only load ...
```

**Half B** needs a GPU and is deferred. `gpu_count` defaults to 0. Method in
[`docs/half-b-ttft.md`](docs/half-b-ttft.md). Use LMCache **MP mode**; do not
set `LMCACHE_USE_LAYERWISE`, which is the deprecated in-process path and does
not emit the `MP_LOOKUP_PREFETCH` / `MP_RETRIEVE` spans the breakdown needs.

```bash
terraform apply -var gpu_count=1     # +$10.49/hr -- set back to 0 when done
```

---

## Soft-RoCE for local testing

RDMA functionality can be exercised with **zero AWS spend** using Soft-RoCE
(`rxe`), the kernel's software RoCE implementation. The parallel LMCache agent
is using this. It is correct for functional testing and useless for performance
numbers. See [`docs/soft-roce.md`](docs/soft-roce.md).

---

## Approval required

Before anything is applied, the human needs to approve:

1. **~$58/hour** for the Half A cluster (5 x `i3en.24xlarge` + 1 x
   `c5n.18xlarge`), ~$466 per 8-hour day.
2. **Optionally +$10.49/hour** for the Half B GPU node (`g6e.12xlarge`).
3. A realistic total M0 budget of **$2,000–$2,500** including re-runs.
4. Running the storage tier on **Intel**, because AMD + EFA + local NVMe does
   not exist outside GPU families in us-east-1 (finding #1).

No quota increase is needed — see finding #3.
