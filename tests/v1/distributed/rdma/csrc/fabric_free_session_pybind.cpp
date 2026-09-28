// SPDX-License-Identifier: Apache-2.0
//
// Test-only Python binding over the real PipelinedFetchPool (one
// PipelinedFetchSession per window), with no fabric, device, or cluster.
//
// FabricFreeConnector exposes the same pipelined methods as
// lmcache_aerospike.LMCacheAerospikeClient, so AerospikeLayerArrivalSource and
// NativePlanIssuer run against it unchanged. It adds the two ArrivalDriver
// hooks the layerwise conformance suite drives: land_slot feeds an encoded
// immediate, as RdmaContext::poll_notifications would; decline_slot feeds a
// node's reply naming the slot as failed, as aerospike_info_node would. Every
// accounting decision -- stale generations, per-layer counts, which node owns
// a slot -- is the production session's, not this file's.
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
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "kv_sink_client.h"
#include "layer_pipeline.h"
#include "pipelined_fetch_issue.h"
#include "pipelined_fetch_pool.h"

namespace py = pybind11;

namespace {

using lmcache::connector::rdma::encode_immediate;
using lmcache::connector::rdma::issue_planned_fetch;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::pipelined_command_slot_indices;
using lmcache::connector::rdma::PipelinedFetchPool;
using lmcache::connector::rdma::PlannedSlot;

// A reply accepting every sink the command carried.
std::string accept_all(const std::string& command) {
  const size_t n = pipelined_command_slot_indices(command).size();
  return "n=" + std::to_string(n) + ";accepted=" + std::to_string(n) +
         ";bytes=0";
}

// (future_id, ok, error, per-key results), as the production client's
// drain_completions returns them.
using Completion =
    std::tuple<uint64_t, bool, std::string, std::optional<std::vector<bool>>>;

class FabricFreeConnector {
 public:
  // `max_slots` is one window's share, so the pool's depth is
  // max_slots * window_count.
  FabricFreeConnector(const std::vector<std::string>& node_names,
                      size_t window_bytes, uint32_t max_slots,
                      uint32_t max_sinks, uint32_t window_count)
      : event_fd_(eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK)) {
    if (event_fd_ < 0) {
      throw std::runtime_error(std::string("eventfd failed: ") +
                               std::strerror(errno));
    }
    uint64_t region = 1;
    for (const std::string& name : node_names) {
      NodeRegistration registration;
      registration.node_name = name;
      registration.region = region++;
      registration.max_sinks_per_command = max_sinks;
      registration.valid = true;
      registry_.set(registration);
    }
    pool_ = std::make_unique<PipelinedFetchPool>(
        registry_, "kv", window_bytes, window_count, max_slots * window_count);
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
    return pool_->max_slots_per_request();
  }

  // Throws unless exactly one node is registered, as the production driver
  // does: pipelined fetches run on single-node clusters only.
  std::string pipelined_fetch_node_name() const {
    const std::vector<std::string> names = registry_.node_names();
    if (names.size() != 1) {
      throw std::runtime_error(
          "pipelined fetches need one registered node, but there are " +
          std::to_string(names.size()));
    }
    return names.front();
  }

  // Record keys stand in for digests: the session only needs them non-empty
  // and carries them opaquely into the command.
  uint16_t issue_pipelined_fetch_by_slots(
      const std::vector<std::string>& node_names,
      const std::vector<
          std::tuple<uint32_t, std::string, size_t, size_t, uint32_t>>& slots) {
    std::vector<PlannedSlot> planned;
    planned.reserve(slots.size());
    for (const auto& [node_index, record_key, offset, length, layer_id] :
         slots) {
      if (node_index >= node_names.size()) {
        throw std::invalid_argument("node index " + std::to_string(node_index) +
                                    " is out of range");
      }
      planned.push_back(
          {node_names[node_index], record_key, layer_id, offset, length});
    }
    return issue_planned_fetch(
        *pool_,
        [](const std::string&, const std::string& command) {
          return accept_all(command);
        },
        planned);
  }

  bool is_pipelined_layer_ready(uint32_t layer_id,
                                uint16_t request_generation) const {
    return pool_->is_layer_ready(layer_id, request_generation);
  }

  std::vector<uint32_t> pipelined_unservable_layers(uint16_t generation) const {
    return pool_->unservable_layers(generation);
  }

  void finish_pipelined_fetch(uint16_t generation) {
    pool_->finish_request(generation);
  }

  void abandon_pipelined_fetch(uint16_t generation) {
    pool_->abandon_request(generation);
  }

  // The pool routes the immediate to its generation's window, whose session
  // discards it unless that generation is active, so a late write from an
  // abandoned fetch is dropped here too.
  void land_slot(uint16_t slot_index, uint16_t generation) {
    pool_->on_notifications({encode_immediate(generation, slot_index)});
  }

  // Replies quoting a generation that is not active are ignored by the pool;
  // the early return only avoids looking up commands that no longer exist.
  void decline_slot(uint16_t slot_index, uint16_t generation) {
    if (!pool_->is_active(generation)) {
      return;
    }
    for (const auto& [node, command] :
         pool_->pipelined_fetch_commands(generation)) {
      const std::vector<uint16_t> carried =
          pipelined_command_slot_indices(command);
      for (const uint16_t slot : carried) {
        if (slot == slot_index) {
          const std::string reply =
              "n=" + std::to_string(carried.size()) +
              ";accepted=" + std::to_string(carried.size() - 1) +
              ";failed=" + std::to_string(slot_index) + ";bytes=0";
          pool_->on_node_reply(node, command, reply, generation);
          return;
        }
      }
    }
    throw std::invalid_argument("slot " + std::to_string(slot_index) +
                                " is not in the active fetch");
  }

 private:
  int event_fd_;
  NodeRegistry registry_;
  std::unique_ptr<PipelinedFetchPool> pool_;
};

}  // namespace

PYBIND11_MODULE(fabric_free_session, m) {
  py::register_exception<lmcache::connector::rdma::PlanTooLargeError>(
      m, "PipelinedPlanTooLargeError", PyExc_RuntimeError);

  py::class_<FabricFreeConnector>(m, "FabricFreeConnector")
      .def(py::init<const std::vector<std::string>&, size_t, uint32_t, uint32_t,
                    uint32_t>(),
           py::arg("node_names"), py::arg("window_bytes"), py::arg("max_slots"),
           py::arg("max_sinks") = 256, py::arg("window_count") = 1)
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
