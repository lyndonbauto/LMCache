// SPDX-License-Identifier: Apache-2.0
#pragma once

#ifdef LMCACHE_AEROSPIKE_RDMA

  #include "connector.h"
  #include "memory_layout_conversion.h"
  #include "pipelined_fetch_session.h"

  #include <pybind11/pybind11.h>

namespace py = pybind11;

namespace lmcache {
namespace connector {
namespace aerospike_pipelined_pybind {

// Register pipelined-fetch types and AerospikeNativeConnector methods.
void bind_pipelined_fetch(py::module& module,
                          py::class_<AerospikeNativeConnector>& connector);

}  // namespace aerospike_pipelined_pybind
}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
