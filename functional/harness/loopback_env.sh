#!/bin/bash
# Source before starting vLLM or LMCache: keep every listener they open on
# loopback. ufw's default policy on the box allows incoming connections, so a
# listener on 0.0.0.0, [::] or the public IP is reachable from the internet.
# Single-engine vLLM uses ipc:// for its ZMQ sockets. Its TCP listeners are
# gloo's (interface from GLOO_SOCKET_IFNAME) and torch.distributed's
# rendezvous store (init_process_group tcp://<VLLM_HOST_IP>:<port>), whose
# master listens on every interface whatever the host; loopback_shim/
# sitecustomize.py hands it a socket bound to 127.0.0.1 instead.
export VLLM_HOST_IP=127.0.0.1 VLLM_LOOPBACK_IP=127.0.0.1 MASTER_ADDR=127.0.0.1
export GLOO_SOCKET_IFNAME=lo NCCL_SOCKET_IFNAME=lo
LOOPBACK_SHIM=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/loopback_shim
case ":${PYTHONPATH:-}:" in
  *":$LOOPBACK_SHIM:"*) ;;
  *) export PYTHONPATH=$LOOPBACK_SHIM${PYTHONPATH:+:$PYTHONPATH};;
esac
