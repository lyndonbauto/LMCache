#!/bin/bash
V=$(python -c "import vllm,os;print(os.path.dirname(vllm.__file__))" 2>/dev/null)
echo "vllm at $V"; python -c "import vllm;print(vllm.__version__)"
grep -n "distributed_init_method\|get_ip()\|get_open_port" $V/v1/executor/uniproc_executor.py | head -20
grep -n "def get_ip\|VLLM_HOST_IP\|VLLM_LOOPBACK_IP" $V/utils/network_utils.py $V/envs.py 2>/dev/null | head -20
grep -rn "def get_distributed_init_method" $V | head
grep -rn "loopback" $V/distributed/parallel_state.py | head
which ss; python -c "import torch;print(torch.__version__)"
