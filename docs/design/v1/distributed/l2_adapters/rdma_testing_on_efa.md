# Testing the Aerospike RDMA path on AWS EFA

What has to be in place before the EFA/SRD path can be tested, and what to
run once it is. The Soft-RoCE VM
([rdma_testing_on_windows.md](rdma_testing_on_windows.md)) covers everything
except SRD, because Soft-RoCE supports only RC queue pairs.

**What this answers:** A8 over SRD --- the kv-sink client's SRD transport
against a real server, through LMCache's pipelined fetch. On the previous
protocol all 15 Aerospike integration tests passed over SRD on EFA v2
(2026-09-29). The kv-sink batch-read protocol has not been run on EFA yet.

**A7** ([track-a-acceptance.md](../../../layerwise/track-a-acceptance.md#a7-the-efa-question-is-answered-with-hardware))
asked whether an SRD write-with-immediate consumes a posted receive. It was
answered on EFA v2 (yes, as on RC), and no longer applies: on kv-sink batch
reads a row's reply is its completion, so LMCache posts no receives and the
probe that answered it has been removed.

## What is needed

### From AWS

| Item | Why |
|---|---|
| One EC2 instance with **EFA and RDMA write** (see below) | The server writes into L1 with RDMA write |
| vCPU quota for that family in the region | These sizes are large (32 to 64 vCPUs); the default quota often blocks the launch |
| A subnet in one Availability Zone | EFA traffic cannot cross AZs and is not routable |
| A security group with an inbound **and** outbound rule allowing all traffic from itself | AWS requires it for EFA traffic |
| The network interface attached as **EFA with ENA** at launch | "EFA-only" has no IP, so no SSH over it |
| SSH access: a key pair and a public IP or bastion | To build and run |

One instance is enough: the Aerospike server can run on the same host as the
client, and EFA accepts writes to its own address.

**Instance type.** RDMA write needs Nitro v4 or later, with exceptions. From
the [EFA supported-instance table](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html),
the smallest suitable sizes are:

| Instance | CPU | Note |
|---|---|---|
| `g6.8xlarge` | x86 | Smallest widely available x86 option; its L4 GPU is unused |
| `c7g.16xlarge`, `m7g.16xlarge` | Graviton (arm64) | Untested; the server and client build on arm64 |
| `hpc7a.12xlarge` | x86 | Only in a few regions |

The earlier runs used `g6.8xlarge` on demand in `us-west-2` ($2.01/h). Spot capacity
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
| The [AWS EFA installer](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-start.html) | The EFA kernel module, and an rdma-core with `efadv.h`, which both the server and the kv-sink client need to compile their verbs transport |
| `build-essential`, `git`, `python3` and `uv` | Building the server, the client and LMCache |
| No Soft-RoCE device | Server and client open the first RDMA device unless told otherwise, so an `rxe0` could be picked instead of EFA |

The installer's `--minimal` mode is enough. It installs the EFA kernel module
and rdma-core but not libfabric, so `fi_info` is absent:

```bash
curl -sSfO https://efa-installer.amazonaws.com/aws-efa-installer-latest.tar.gz
tar -xf aws-efa-installer-latest.tar.gz && cd aws-efa-installer
sudo ./efa_installer.sh -y --minimal
```

Check the device before anything else:

```bash
ibv_devices  # an EFA device, e.g. rdmap47s0
ls /usr/include/infiniband/efadv.h
lspci -n | grep 1d0f:efa  # efa1 is EFA v2, efa2 is EFA v3
```

The device name changes when the installer reloads the kernel module: it was
`efa_0` at boot and `rdmap47s0` afterwards. Take it from `ibv_devices` after
the install.

SRD does not route by GID, so the default `gid_index` works on EFA; see
[the GID index trap](aerospike_rdma.md#the-gid-index-trap) for why it matters
on the Soft-RoCE VM.

### From the repositories

- The Aerospike server, branch `sriram/kv-sink-batch-prio`. Its log must show
  the EFA device with RDMA write support at the first sink registration.
- The kv-sink C client, built by `.deps/build_aerospike_client_kvsink.sh`
  (branch `sriram/kv-sink-batch-prio` of `aerospike-client-c-kvsink`).

## What to run

Follow
[Running A8 against a real server](aerospike_rdma.md#running-a8-against-a-real-server),
with these differences:

- start the server with `KV_SINK_RDMA_DEVICE=$DEV`, where
  `DEV=$(ibv_devices | awk 'NR>2 {print $1; exit}')`;
- run the tests with `RDMA_TRANSPORT=SRD RDMA_DEVICE=$DEV`;
- raise the server's locked-memory limit with `prlimit` as on the VM;
- install the Python `aerospike` package (`uv pip install aerospike`). It is
  not in `requirements/test.txt`, and without it the integration tests skip
  rather than fail.

Pitfalls when the source is copied rather than cloned:

- A tree without `.git` fails the LMCache build in setuptools-scm. Set
  `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=0.0.0`.
- A server tree already built elsewhere keeps that host's absolute paths in
  its CMake caches, and excluding `*.a` from the copy drops the prebuilt
  `libbacktrace.a` and `libjansson.a`. Copy the source only and build it on
  the instance.

## What to bring back

- the instance type, region, EFA installer version, kernel, and the PCI ID
  from `lspci -n`;
- the pytest summary and the server's `kv-sink` log lines.

They go into the status table in
[`aerospike_rdma.md`](aerospike_rdma.md#what-is-proven-and-what-is-not).

If EFA access cannot be obtained, record that instead. Per
[track-a-acceptance.md](../../../layerwise/track-a-acceptance.md#done), it is
a project risk to escalate, not an item to drop.
