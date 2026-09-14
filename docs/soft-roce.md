# Soft-RoCE (`rxe`) — zero-cost local RDMA testing

Soft-RoCE is the Linux kernel's software implementation of RoCE v2. It presents
a real `ibverbs` device over an ordinary Ethernet NIC, so RDMA code paths —
`ibv_reg_mr`, queue pairs, completion queues, RDMA WRITE — execute for real
without any RDMA hardware.

**Use it for:** does the code work, are memory regions registered correctly, do
the queue pairs connect, does the RDMA WRITE land at the right offset.

**Do not use it for:** any number. It is entirely software, copies through the
kernel network stack, and its latency and bandwidth bear no relationship to
EFA/SRD. A performance figure from Soft-RoCE is not a slower version of the real
answer, it is a different measurement.

The parallel LMCache agent is developing against this, which is the right call —
the expensive EFA cluster should be reserved for measurement, not for finding
null-pointer bugs.

## Setup

```bash
sudo apt-get install -y rdma-core iproute2 ibverbs-utils perftest   # Debian/Ubuntu
# sudo dnf install -y rdma-core libibverbs-utils perftest           # RHEL/AL2023

sudo modprobe rdma_rxe

# Bind to a real NIC (not loopback -- rxe on lo behaves oddly).
IFACE=$(ip -o -4 route show default | awk '{print $5}')
sudo rdma link add rxe0 type rxe netdev "$IFACE"

rdma link show          # expect rxe0/1 state ACTIVE physical_state LINK_UP
ibv_devinfo -d rxe0     # expect PORT_ACTIVE
```

## Verify it works

```bash
ibv_rc_pingpong -d rxe0 &      # server
sleep 1
ibv_rc_pingpong -d rxe0 localhost
```

RDMA WRITE specifically, which is the operation the Aerospike server-push design
uses:

```bash
ib_write_bw -d rxe0 &          # server
sleep 1
ib_write_bw -d rxe0 localhost -s 1048576
```

Again: `ib_write_bw` will report a bandwidth number. Ignore it.

## Two-machine testing

Soft-RoCE works across real machines on the same L2 segment. Run
`rdma link add` on both, then point the client at the server's IP instead of
localhost. Useful for testing the connection-establishment path, which loopback
does not exercise properly.

## Teardown

```bash
sudo rdma link delete rxe0
sudo modprobe -r rdma_rxe
```

## How this differs from EFA

| | Soft-RoCE (`rxe`) | EFA |
|---|---|---|
| Transport | RoCE v2 over UDP/IP, software | SRD, hardware |
| Verbs | full RC/UC/UD | **no RC**; UD + SRD via `efadv_create_qp_ex` |
| Cost | free | $10.85/hr per `i3en.24xlarge` |
| Performance | meaningless | the thing being measured |

The verbs difference matters and is the main porting risk. **EFA does not
support Reliable Connected queue pairs.** Code that works on Soft-RoCE using RC
queue pairs will not work on EFA — it has to use SRD, created through the
`efadv_create_qp_ex` extension rather than plain `ibv_create_qp`.

So Soft-RoCE can validate the memory-registration and data-placement logic, but
it cannot validate the queue-pair setup path that EFA actually requires. Budget
for that difference when the code moves from local testing to the cluster.
