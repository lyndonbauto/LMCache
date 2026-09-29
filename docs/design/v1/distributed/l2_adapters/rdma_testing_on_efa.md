# Testing the Aerospike RDMA path on AWS EFA

What has to be in place before the EFA/SRD path can be tested, and what to
run once it is. The Soft-RoCE VM
([rdma_testing_on_windows.md](rdma_testing_on_windows.md)) covers everything
except SRD, because Soft-RoCE supports only RC queue pairs.

**What this answers:**

1. **A7** ([track-a-acceptance.md](../../../layerwise/track-a-acceptance.md#a7-the-efa-question-is-answered-with-hardware)):
   does an SRD write-with-immediate consume a posted receive, and what
   happens when more immediates arrive than there are receives? This decides
   whether the wire contract needs a handshake, which would change Track C's
   plan format. It is the required part. **Done on 2026-09-29 on EFA v2:
   yes, as on RC, and no handshake is needed.** See
   [the SRD result](aerospike_rdma.md#a7-result-on-efa-srd).
2. **SRD end to end** (optional, A8 over EFA): the client's SRD queue pair
   setup (`efadv_create_qp_ex`, the qkey at INIT, the address handle) has
   compiled but never run. Not done yet.

## What is needed

### From AWS

| Item | Why |
|---|---|
| One EC2 instance with **EFA and RDMA write** (see below) | The probe exits 2 on a device without RDMA write |
| vCPU quota for that family in the region | These sizes are large (32 to 64 vCPUs); the default quota often blocks the launch |
| A subnet in one Availability Zone | EFA traffic cannot cross AZs and is not routable |
| A security group with an inbound **and** outbound rule allowing all traffic from itself | AWS requires it for EFA traffic |
| The network interface attached as **EFA with ENA** at launch | "EFA-only" has no IP, so no SSH over it |
| SSH access: a key pair and a public IP or bastion | To build and run |

One instance is enough. The probe runs its sender and receiver queue pairs
on the same device, and the Aerospike server can run on the same host as the
client. A second instance is needed only if the device refuses writes to its
own address. The probe would then have to exchange queue-pair details over
TCP, which it does not do today.

**Instance type.** RDMA write needs Nitro v4 or later, with exceptions. From
the [EFA supported-instance table](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html),
the smallest suitable sizes are:

| Instance | CPU | Note |
|---|---|---|
| `g6.8xlarge` | x86 | Smallest widely available x86 option; its L4 GPU is unused |
| `c7g.16xlarge`, `m7g.16xlarge` | Graviton (arm64) | The probe needs only libibverbs, so arm64 is fine |
| `hpc7a.12xlarge` | x86 | Only in a few regions |

The A7 run used `g6.8xlarge` on demand in `us-west-2` ($2.01/h). Spot capacity
for it was unavailable in all four zones at the time; on-demand launched at
once.

**Not usable** (EFA but no RDMA write): `c7gn`, `hpc7g`, `p4d`, `p4de`, and
every Nitro v3 type (`c5n`, `g4dn`, `g5`, ...). Check what the region offers:

```bash
aws ec2 describe-instance-types \
  --filters Name=network-info.efa-supported,Values=true \
  --query "InstanceTypes[*].[InstanceType]" --output text | sort
```

### On the instance

| Item | Why |
|---|---|
| Ubuntu 22.04 or 24.04 | Matches the VM setup |
| The [AWS EFA installer](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-start.html), not Ubuntu's rdma-core | Ubuntu 24.04's rdma-core 50 lacks the unsolicited write-receive API, so `--unsolicited` exits 2 against it |
| `build-essential`, `git`, `python3` and `uv` | Building the probe and LMCache |
| No Soft-RoCE device | The server opens the first RDMA device in the list, so an `rxe0` could be picked instead of EFA |

The installer's `--minimal` mode is enough for the probe. It installs the EFA
kernel module and rdma-core but not libfabric, so `fi_info` is absent:

```bash
curl -sSfO https://efa-installer.amazonaws.com/aws-efa-installer-latest.tar.gz
tar -xf aws-efa-installer-latest.tar.gz && cd aws-efa-installer
sudo ./efa_installer.sh -y --minimal
```

Check the device before anything else:

```bash
ibv_devices  # an EFA device, e.g. rdmap47s0
grep -c UNSOLICITED_WRITE_RECV /usr/include/infiniband/efadv.h  # > 0 for --unsolicited
lspci -n | grep 1d0f:efa  # efa1 is EFA v2, efa2 is EFA v3
```

The device name changes when the installer reloads the kernel module: it was
`efa_0` at boot and `rdmap47s0` afterwards. Take it from `ibv_devices` after
the install.

Use **GID index 0** on EFA, not 1 as on the Soft-RoCE VM; see
[the GID index trap](aerospike_rdma.md#the-gid-index-trap).

### From the repository

- Branch `track/a-transport`, with the A8 and deregistration commits.
- For the end-to-end step only: `test_aerospike_pipelined_rdma_integration.py`
  hardcodes `RdmaTransport.RC`. It needs an `RDMA_TRANSPORT` environment
  variable before it can run over SRD. This is a small change, not made yet.
- For the end-to-end step only: the Aerospike server source,
  `feat/kv-sink-fetch-pipelined` (`512b0c20`). The server needs `efadv.h`
  from rdma-core 46 or later at build time, and detects EFA at runtime; its
  log shows `kv-sink: EFA - rdma read yes, rdma write yes`.

## What to run

### 1. A7: the receive-consumption probe (required)

```bash
make -C tests/v1/distributed/rdma efa-probe EFA=1
DEV=$(ibv_devices | awk 'NR>2 {print $1; exit}')
tests/v1/distributed/rdma/build/efa_imm_probe_efa "$DEV" 0 --transport srd
tests/v1/distributed/rdma/build/efa_imm_probe_efa "$DEV" 0 --transport srd --unsolicited
```

Each run prints one `RESULT` line per scenario and a `VERDICT` line. How to
read them is in
[Receive queue depth](aerospike_rdma.md#receive-queue-depth-and-device-limits).
In short:

| VERDICT | Consequence |
|---|---|
| `consumes_recv_wr=yes` | Same as RC. The current depth logic stays binding, and nothing changes |
| `consumes_recv_wr=no` | The receive-depth clamp becomes a conservative no-op |
| `data_without_notification=yes` | Bytes land before their receive exists. EFA v2 shows this, and the client already covers it by never letting its receive queue run short |
| `unsolicited_works=yes` | With the flag on both queue pairs, immediates complete with no receive posted |

Exit code 2 means the device cannot run the probe: not EFA, no RDMA write, or
`--unsolicited` without the API.

The first A7 run found a probe bug: `--unsolicited` set the flag on the
receiver only, and SRD fails every write when the two queue pairs disagree.
The probe now sets it on both, and runs the receiver-only case as
`unsolicited_receiver_only` to record the mismatch.

### 2. SRD end to end (optional)

Once the transport variable exists, follow
[Running A8 against a real server](aerospike_rdma.md#running-a8-against-a-real-server),
with these differences:

- build LMCache with `BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_EFA=1`;
- run with `RDMA_TRANSPORT=SRD RDMA_DEVICE=$DEV RDMA_GID_INDEX=0`;
- raise the server's locked-memory limit with `prlimit` as on the VM;
- note the depth the client logs (`reports max_recv_wr=... effective=...`)
  and compare it with the probe's `max_rq_wr`. `RdmaContext` clamps to
  `ibv_query_device`'s `max_qp_wr` only, not to EFA's `max_rq_wr`.

## What to bring back

- the instance type, region, EFA installer version, kernel, and the PCI ID
  from `lspci -n`;
- all `RESULT` and `VERDICT` lines from both probe runs;
- for step 2, the pytest summary and the server's `kv-sink` log lines.

They go into
[Receive queue depth](aerospike_rdma.md#receive-queue-depth-and-device-limits)
and the status table in `aerospike_rdma.md`, and into A7 in
[track-a-questions-for-track-c.md](../../../layerwise/track-a-questions-for-track-c.md).

If EFA access cannot be obtained, record that instead. Per
[track-a-acceptance.md](../../../layerwise/track-a-acceptance.md#done), it is
a project risk to escalate, not an item to drop.
