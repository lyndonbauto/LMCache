#!/bin/bash
# D-14: rebuild LMCache in lmc-d (no GPU) with the Day 1 flags.
cd /work/LMCache || exit 1
export BUILD_WITH_HIP=1 PYTORCH_ROCM_ARCH=gfx942 CXX=hipcc TORCH_DONT_CHECK_COMPILER_ABI=1 MAX_JOBS=8
export BUILD_WITH_AEROSPIKE=1 BUILD_WITH_AEROSPIKE_RDMA=1
export AEROSPIKE_INCLUDE_DIR=/work/deps/aerospike-install/usr/include
export AEROSPIKE_LIBRARY_DIR=/work/deps/aerospike-install/usr/lib
export SETUPTOOLS_SCM_PRETEND_VERSION=0.4.6.dev940
time nice -n 19 pip install -e . --no-build-isolation --no-deps --ignore-requires-python -v
echo "build exit=$?"
