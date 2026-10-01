// SPDX-License-Identifier: Apache-2.0
//
// Test-only Python binding over the real SinkFetchTable, with no fabric,
// device, or cluster.
//
// FabricFreeConnector exposes the same pipelined methods as
// lmcache_aerospike.LMCacheAerospikeClient, so AerospikeLayerArrivalSource and
// NativePlanIssuer run against it unchanged. It adds the two ArrivalDriver
// hooks the layerwise conformance suite drives: land_slot reports a row that
// succeeded and decline_slot one that failed, as AerospikeSinkFetchDriver
// does when a batch read returns. Every accounting decision -- stale
// generations, per-layer counts, busy windows -- is the production table's,
// not this file's.
//
// It also has event_fd, drain_completions and close, so
// NativeConnectorL2Adapter can wrap it. It runs no batch operations, so the
// eventfd never fires and drain_completions is always empty.
//
// Built by `make -C tests/v1/distributed/rdma pyharness`; never shipped.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <sys/eventfd.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstring>
#include <optional>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

#include "sink_fetch_table.h"

namespace py = pybind11;

namespace {

using lmcache::connector::sink::SinkFetchTable;
using lmcache::connector::sink::SinkSlot;

// (future_id, ok, error, per-key results), as the production client's
// drain_completions returns them.
using Completion =
    std::tuple<uint64_t, bool, std::string, std::optional<std::vector<bool>>>;

class FabricFreeConnector {
 public:
  FabricFreeConnector(const std::vector<std::string>& node_names,
                      size_t window_bytes, uint32_t max_slots,
                      uint32_t window_count)
      : node_names_(node_names),
        table_(window_bytes, window_count, max_slots),
        event_fd_(eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK)) {
    if (event_fd_ < 0) {
      throw std::runtime_error(std::string("eventfd failed: ") +
                               std::strerror(errno));
    }
  }

  FabricFreeConnector(const FabricFreeConnector&) = delete;
  FabricFreeConnector& operator=(const FabricFreeConnector&) = delete;

  ~FabricFreeConnector() { close(); }

  int event_fd() const { return event_fd_; }

  std::vector<Completion> drain_completions() const { return {}; }

  void close() {
    if (event_fd_ >= 0) {
      ::close(event_fd_);
      event_fd_ = -1;
    }
  }

  bool pipelined_fetch_ready() const { return true; }

  std::string pipelined_fetch_init_error() const { return {}; }

  uint32_t pipelined_max_slots_per_request() const {
    return table_.max_slots_per_request();
  }

  // The first node, as the production driver answers; throws for an empty
  // cluster.
  std::string pipelined_fetch_node_name() const {
    if (node_names_.empty()) {
      throw std::runtime_error("the cluster has no nodes");
    }
    return node_names_.front();
  }

  uint16_t issue_pipelined_fetch_by_slots(
      const std::vector<std::string>& node_names,
      const std::vector<
          std::tuple<uint32_t, std::string, size_t, size_t, uint32_t>>& slots) {
    std::vector<SinkSlot> planned;
    planned.reserve(slots.size());
    for (const auto& [node_index, record_key, offset, length, layer_id] :
         slots) {
      if (node_index >= node_names.size()) {
        throw std::invalid_argument("node index " + std::to_string(node_index) +
                                    " is out of range");
      }
      planned.push_back({record_key, layer_id, offset, length});
    }
    return table_.begin(planned).generation;
  }

  bool is_pipelined_layer_ready(uint32_t layer_id,
                                uint16_t request_generation) const {
    return table_.is_layer_ready(layer_id, request_generation);
  }

  std::vector<uint32_t> pipelined_unservable_layers(uint16_t generation) const {
    return table_.unservable_layers(generation);
  }

  void finish_pipelined_fetch(uint16_t generation) {
    table_.finish(generation);
  }

  void abandon_pipelined_fetch(uint16_t generation) {
    table_.abandon(generation);
  }

  // The table drops a result for a generation that is not active, so a late
  // row from an abandoned fetch is dropped here too.
  void land_slot(uint32_t slot_index, uint16_t generation) {
    table_.on_slot_result(generation, slot_index, true);
  }

  void decline_slot(uint32_t slot_index, uint16_t generation) {
    table_.on_slot_result(generation, slot_index, false);
  }

 private:
  const std::vector<std::string> node_names_;
  SinkFetchTable table_;
  int event_fd_;
};

}  // namespace

PYBIND11_MODULE(fabric_free_session, m) {
  py::register_exception<lmcache::connector::sink::PlanTooLargeError>(
      m, "PipelinedPlanTooLargeError", PyExc_RuntimeError);

  py::class_<FabricFreeConnector>(m, "FabricFreeConnector")
      .def(py::init<const std::vector<std::string>&, size_t, uint32_t,
                    uint32_t>(),
           py::arg("node_names"), py::arg("window_bytes"), py::arg("max_slots"),
           py::arg("window_count") = 1)
      .def("event_fd", &FabricFreeConnector::event_fd)
      .def("drain_completions", &FabricFreeConnector::drain_completions)
      .def("close", &FabricFreeConnector::close)
      .def("pipelined_fetch_ready", &FabricFreeConnector::pipelined_fetch_ready)
      .def("pipelined_fetch_init_error",
           &FabricFreeConnector::pipelined_fetch_init_error)
      .def("pipelined_max_slots_per_request",
           &FabricFreeConnector::pipelined_max_slots_per_request)
      .def("pipelined_fetch_node_name",
           &FabricFreeConnector::pipelined_fetch_node_name)
      .def("issue_pipelined_fetch_by_slots",
           &FabricFreeConnector::issue_pipelined_fetch_by_slots,
           py::arg("node_names"), py::arg("slots"))
      .def("is_pipelined_layer_ready",
           &FabricFreeConnector::is_pipelined_layer_ready, py::arg("layer_id"),
           py::arg("request_generation"))
      .def("pipelined_unservable_layers",
           &FabricFreeConnector::pipelined_unservable_layers,
           py::arg("generation"))
      .def("finish_pipelined_fetch",
           &FabricFreeConnector::finish_pipelined_fetch, py::arg("generation"))
      .def("abandon_pipelined_fetch",
           &FabricFreeConnector::abandon_pipelined_fetch, py::arg("generation"))
      .def("land_slot", &FabricFreeConnector::land_slot, py::arg("slot_index"),
           py::arg("generation"))
      .def("decline_slot", &FabricFreeConnector::decline_slot,
           py::arg("slot_index"), py::arg("generation"));
}
