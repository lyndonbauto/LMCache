#!/bin/bash
echo "kernel: $(uname -r)"
echo "host os: $(. /etc/os-release; echo $PRETTY_NAME)"
docker exec lmc-c python -c "import torch, vllm; print('torch:', torch.__version__); print('vllm:', vllm.__version__); print('hip:', torch.version.hip)" 2>/dev/null | grep -E "^(torch|vllm|hip):"
docker exec lmc-c bash -c "cat /opt/rocm/.info/version 2>/dev/null | sed 's/^/rocm: /'"
echo "aerospike CE cluster: $(docker exec aero-n1 asd --version 2>/dev/null || docker run --rm --entrypoint asd aerospike/aerospike-server:latest --version)"
echo "aerospike image: $(docker image inspect aerospike/aerospike-server:latest --format '{{.Id}} {{index .RepoDigests 0}}')"
echo "kv-sink server: $(python3 /root/lmc-work/LMCache-cpu/functional/harness/as_info.py 3400 build 2>/dev/null || cat /work/VERSION 2>/dev/null)"
echo "aero-kvsink/lmc-c image: $(docker image inspect lmcache-rocm:day1 --format '{{.Id}}')"
echo "C client: $(ls /root/lmc-work/deps/ | tr '\n' ' ')"
docker exec lmc-c bash -c "grep -m1 -i version /work/deps/aerospike-install/usr/include/aerospike/version.h 2>/dev/null; ls /work/deps/ 2>/dev/null"
cd /root/lmc-work/LMCache-cpu && echo "LMCache clone (tests run from): $(git log --oneline -1) + uncommitted stage6 test files"
docker exec lmc-c python -c "import lmcache, os; print('lmcache imported from:', os.path.dirname(lmcache.__file__))" 2>/dev/null
docker exec -w /work/LMCache-cpu lmc-c python -c "import lmcache, os; print('lmcache imported from (cwd LMCache-cpu):', os.path.dirname(lmcache.__file__))" 2>/dev/null
