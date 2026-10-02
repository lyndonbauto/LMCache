#!/bin/bash
echo "kernel: $(uname -r)"
echo "host os: $(. /etc/os-release; echo $PRETTY_NAME)"
echo "amd-smi: $(amd-smi version 2>/dev/null | head -1)"
docker exec lmc-c bash -c 'echo "hipcc: $(hipcc --version 2>/dev/null | grep -m1 HIP)"; python -c "import torch, vllm; print(\"torch:\", torch.__version__, \"hip:\", torch.version.hip); print(\"vllm:\", vllm.__version__)"; pip show lmcache 2>/dev/null | grep -i ^version'
echo "lmc-c image: $(docker inspect -f '{{.Config.Image}} {{.Image}}' lmc-c)"
echo "aero-kvsink-bp image: $(docker inspect -f '{{.Config.Image}}' aero-kvsink-bp)"
echo "aerospike-ce image: $(docker inspect -f '{{.Config.Image}} {{.Image}}' aerospike-ce)"
docker exec aerospike-ce asinfo -v build 2>/dev/null | sed 's/^/aerospike-ce build: /'
/root/lmc-work/aerospike-server-kvsink-bp/target/Linux-x86_64/bin/asd --version 2>&1 | head -2
git -C /root/lmc-work/aerospike-server-kvsink-bp log --oneline -1 2>/dev/null
cat /root/lmc-work/deps/aerospike-kvsink-install-523d51ea/*.txt 2>/dev/null | head
ls /root/lmc-work/deps/aerospike-kvsink-install-523d51ea
git -C /root/lmc-work/LMCache log --oneline -3
lsmod | grep rdma_rxe; docker exec lmc-c rdma link 2>/dev/null
