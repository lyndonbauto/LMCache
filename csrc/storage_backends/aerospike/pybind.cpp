// SPDX-License-Identifier: Apache-2.0

#include <pybind11/pybind11.h>
#include "../connector_pybind_utils.h"
#include "connector.h"
#include "l1_rdma_registration.h"

namespace py = pybind11;

PYBIND11_MODULE(lmcache_aerospike, m) {
  m.doc() = "Native Aerospike connector for LMCache";

  // Always exposed so the Python factory can import it unconditionally. On a
  // build without RDMA support the fields below are not bound, so the factory
  // sees a field-less type and reports that RDMA was compiled out rather than
  // failing with an opaque ImportError.
  auto registration = py::class_<lmcache::connector::L1RdmaRegistration>(
                          m, "L1RdmaRegistration")
                          .def(py::init<>());
#ifdef LMCACHE_AEROSPIKE_RDMA
  registration
      .def_readwrite("transport",
                     &lmcache::connector::L1RdmaRegistration::transport)
      .def_readwrite("device_name",
                     &lmcache::connector::L1RdmaRegistration::device_name)
      .def_readwrite("gid_index",
                     &lmcache::connector::L1RdmaRegistration::gid_index)
      .def_readwrite("base", &lmcache::connector::L1RdmaRegistration::base)
      .def_readwrite("size", &lmcache::connector::L1RdmaRegistration::size)
      .def_readwrite("window_count",
                     &lmcache::connector::L1RdmaRegistration::window_count)
      .def_readwrite("window_bytes",
                     &lmcache::connector::L1RdmaRegistration::window_bytes)
      .def_readwrite("fetch_timeout_ms",
                     &lmcache::connector::L1RdmaRegistration::fetch_timeout_ms)
      .def("is_enabled", &lmcache::connector::L1RdmaRegistration::is_enabled);
#endif

  py::class_<lmcache::connector::AerospikeNativeConnector>(
      m, "LMCacheAerospikeClient")
      .def(py::init<std::string, std::string, std::string, int, uint32_t,
                    uint32_t, uint32_t, size_t, size_t, std::string,
                    std::string, lmcache::connector::L1RdmaRegistration>(),
           py::arg("hosts"), py::arg("namespace"), py::arg("set_name"),
           py::arg("num_workers"), py::arg("read_timeout_ms") = 1000,
           py::arg("write_timeout_ms") = 2000,
           py::arg("default_ttl_seconds") = 86400,
           py::arg("target_segment_bytes") = 0, py::arg("max_record_bytes") = 0,
           py::arg("username") = "", py::arg("password") = "",
           py::arg("l1_rdma_registration") =
               lmcache::connector::L1RdmaRegistration())
          LMCACHE_BIND_CONNECTOR_METHODS(
              lmcache::connector::AerospikeNativeConnector);
}
