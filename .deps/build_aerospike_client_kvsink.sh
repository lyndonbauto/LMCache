#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the kv-sink fork of the Aerospike C client into .deps/ and write
# .deps/aerospike-client-c.env pointing the LMCache build at it.
#
# The pipelined RDMA fetch (BUILD_WITH_AEROSPIKE_RDMA=1) calls
# aerospike_sink_create() and sets the sink fields of batch-read rows, which
# only this fork has. Its verbs transport is compiled only when
# infiniband/efadv.h is installed (rdma-core's libibverbs-dev / rdma-core-devel);
# without it the client falls back to a same-host transport LMCache refuses.
#
# Usage, from the repository root:
#   .deps/build_aerospike_client_kvsink.sh [SOURCE_DIR]
#   source .deps/aerospike-client-c.env
#   BUILD_WITH_AEROSPIKE_RDMA=1 pip install -e . --no-build-isolation
#
# SOURCE_DIR is an existing checkout or a copied tree with its submodules
# (modules/common, lua, mod-lua); without it the fork is cloned into
# .deps/aerospike-client-c-kvsink. Override the clone with
# AEROSPIKE_CLIENT_REPO and AEROSPIKE_CLIENT_REF.

set -euo pipefail

DEPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${AEROSPIKE_CLIENT_REPO:-https://github.com/sriram588/aerospike-client-c-kvsink.git}"
REF="${AEROSPIKE_CLIENT_REF:-sriram/kv-sink-batch-prio}"
SRC="${1:-${DEPS}/aerospike-client-c-kvsink}"
INSTALL="${DEPS}/aerospike-kvsink-install"
ENV_FILE="${DEPS}/aerospike-client-c.env"

if [[ ! -f /usr/include/infiniband/efadv.h ]]; then
  echo "error: /usr/include/infiniband/efadv.h not found." >&2
  echo "Install rdma-core development headers (libibverbs-dev on Debian/Ubuntu," >&2
  echo "rdma-core-devel on RHEL/Amazon Linux); the client's verbs transport" >&2
  echo "is compiled only when they are present." >&2
  exit 1
fi

if [[ -e "${SRC}/.git" ]]; then
  git -C "${SRC}" submodule update --init --recursive
elif [[ $# -ge 1 ]]; then
  # A copied source tree: its submodules must already be in place.
  if [[ ! -f "${SRC}/modules/common/Makefile" ]]; then
    echo "error: ${SRC} has no modules/common; copy the submodules too" >&2
    exit 1
  fi
else
  git clone --branch "${REF}" --recurse-submodules "${REPO}" "${SRC}"
fi

# No EVENT_LIB: the connector uses only the synchronous API, so the build
# needs no libuv/libev/libevent.
#
# The fork links libaerospike.so without libibverbs, so its ibv_* references
# carry no symbol version and resolve at runtime to libibverbs' IBVERBS_1.0
# compatibility entry points, whose ibv_mr is a different struct: the sink
# then advertises a garbage rkey and every server write fails with a remote
# access error. --no-as-needed is required because the Makefile places
# LDFLAGS before the objects. The rm forces a relink with these flags.
rm -f "${SRC}"/target/*/lib/libaerospike.so
make -C "${SRC}" -j"$(nproc)" EVENT_LIB= \
  LDFLAGS="-Wl,--no-as-needed -libverbs -lefa"

PLATFORM_DIR="$(find "${SRC}/target" -mindepth 1 -maxdepth 1 -type d -name '*-*' | head -n 1)"
if [[ -z "${PLATFORM_DIR}" || ! -f "${PLATFORM_DIR}/include/aerospike/as_sink.h" ]]; then
  echo "error: build finished but no target/*/include/aerospike/as_sink.h was produced" >&2
  exit 1
fi

DYNSYMS="$(objdump -T "${PLATFORM_DIR}/lib/libaerospike.so")"
if ! grep -q 'IBVERBS_1\.1.*ibv_reg_mr$' <<<"${DYNSYMS}"; then
  echo "error: libaerospike.so does not bind ibv_reg_mr@IBVERBS_1.1" >&2
  exit 1
fi

rm -rf "${INSTALL}"
mkdir -p "${INSTALL}"
cp -r "${PLATFORM_DIR}/include" "${INSTALL}/include"
cp -r "${PLATFORM_DIR}/lib" "${INSTALL}/lib"

cat > "${ENV_FILE}" <<EOF
# Written by .deps/build_aerospike_client_kvsink.sh from ${SRC}.
export AEROSPIKE_INCLUDE_DIR="${INSTALL}/include"
export AEROSPIKE_LIBRARY_DIR="${INSTALL}/lib"
# Built without an event library; see the script.
export AEROSPIKE_EVENT_LIB=none
export LD_LIBRARY_PATH="${INSTALL}/lib:\${LD_LIBRARY_PATH:-}"
EOF

echo "Installed the kv-sink client into ${INSTALL}"
echo "Next: source ${ENV_FILE} && BUILD_WITH_AEROSPIKE_RDMA=1 pip install -e . --no-build-isolation"
