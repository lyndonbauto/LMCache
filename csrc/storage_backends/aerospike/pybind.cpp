// SPDX-License-Identifier: Apache-2.0

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <utility>
#include <vector>

#include "../connector_pybind_utils.h"
#include "connector.h"
#include "l1_rdma_registration.h"
#ifdef LMCACHE_AEROSPIKE_RDMA
  #include "aerospike_pipelined_pybind.h"
#endif

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

  auto aerospike_client =
      py::class_<lmcache::connector::AerospikeNativeConnector>(
          m, "LMCacheAerospikeClient")
          .def(py::init<std::string, std::string, std::string, int, uint32_t,
                        uint32_t, uint32_t, size_t, size_t, std::string,
                        std::string, lmcache::connector::L1RdmaRegistration,
                        size_t>(),
               py::arg("hosts"), py::arg("namespace"), py::arg("set_name"),
               py::arg("num_workers"), py::arg("read_timeout_ms") = 1000,
               py::arg("write_timeout_ms") = 2000,
               py::arg("default_ttl_seconds") = 86400,
               py::arg("target_segment_bytes") = 0,
               py::arg("max_record_bytes") = 0, py::arg("username") = "",
               py::arg("password") = "",
               py::arg("l1_rdma_registration") =
                   lmcache::connector::L1RdmaRegistration(),
               py::arg("plane_bytes") = 0)
          .def("set_plane_bytes",
               &lmcache::connector::AerospikeNativeConnector::set_plane_bytes,
               py::arg("plane_bytes"))
          // One list per object group of (plane_bytes, planes) pairs, one
          // pair per kernel group in payload order.
          .def(
              "set_record_layouts",
              [](lmcache::connector::AerospikeNativeConnector& self,
                 const std::vector<std::vector<std::pair<size_t, uint32_t>>>&
                     object_groups) {
                std::vector<std::vector<lmcache::connector::PlaneRun>> runs;
                for (const auto& group : object_groups) {
                  std::vector<lmcache::connector::PlaneRun> group_runs;
                  for (const auto& [plane_bytes, planes] : group) {
                    group_runs.push_back({plane_bytes, planes});
                  }
                  runs.push_back(std::move(group_runs));
                }
                self.set_record_layouts(runs);
              },
              py::arg("object_groups"));
#ifdef LMCACHE_AEROSPIKE_RDMA
  aerospike_client
      .def("pipelined_fetch_ready",
           &lmcache::connector::AerospikeNativeConnector::pipelined_fetch_ready)
      .def("is_pipelined_layer_ready",
           &lmcache::connector::AerospikeNativeConnector::
               is_pipelined_layer_ready,
           py::arg("layer_id"), py::arg("request_generation") = 0)
      .def("poll_pipelined_fetch_notifications",
           &lmcache::connector::AerospikeNativeConnector::
               poll_pipelined_fetch_notifications);
  lmcache::connector::aerospike_pipelined_pybind::bind_pipelined_fetch(
      m, aerospike_client);
#endif
  aerospike_client LMCACHE_BIND_CONNECTOR_METHODS(
      lmcache::connector::AerospikeNativeConnector);
}
