// SPDX-License-Identifier: Apache-2.0
//
// Test-only Python binding over the real PipelinedFetchSession, with no
// fabric, device, or cluster.
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
// Built by `make -C tests/v1/distributed/rdma pyharness`; never shipped.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "kv_sink_client.h"
#include "layer_pipeline.h"
#include "pipelined_fetch_issue.h"
#include "pipelined_fetch_session.h"
#include "slot_planner.h"

namespace py = pybind11;

namespace {

using lmcache::connector::rdma::encode_immediate;
using lmcache::connector::rdma::issue_planned_fetch;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::pipelined_command_slot_indices;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::PlannedSlot;
using lmcache::connector::rdma::SlotPlanner;

constexpr size_t kRecordCap = 1u << 20;

// The session requires a planner, which the slot path never consults, and a
// planner refuses an empty layout. One single-layer group satisfies it.
std::vector<lmcache::connector::rdma::ObjectGroupLayout> unused_layout() {
  lmcache::connector::rdma::KernelGroupLayout group;
  group.layer_indices = {0};
  group.kv_size = 2;
  group.num_slots = 1;
  group.hidden_dim = 1;
  group.element_size = 2;
  lmcache::connector::rdma::ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(group);
  return {layout};
}

// A reply accepting every sink the command carried.
std::string accept_all(const std::string& command) {
  const size_t n = pipelined_command_slot_indices(command).size();
  return "n=" + std::to_string(n) + ";accepted=" + std::to_string(n) +
         ";bytes=0";
}

class FabricFreeConnector {
 public:
  FabricFreeConnector(const std::vector<std::string>& node_names,
                      size_t window_bytes, uint32_t max_slots,
                      uint32_t max_sinks)
      : planner_(unused_layout()) {
    uint64_t region = 1;
    for (const std::string& name : node_names) {
      NodeRegistration registration;
      registration.node_name = name;
      registration.region = region++;
      registration.max_sinks_per_command = max_sinks;
      registration.valid = true;
      registry_.set(registration);
    }
    session_ = std::make_unique<PipelinedFetchSession>(
        planner_, registry_, "kv", kRecordCap, kRecordCap, window_bytes,
        max_slots);
  }

  bool pipelined_fetch_ready() const { return true; }

  std::string pipelined_fetch_init_error() const { return {}; }

  uint32_t pipelined_max_slots_per_request() const {
    return session_->max_slots_per_request();
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
        *session_,
        [](const std::string&, const std::string& command) {
          return accept_all(command);
        },
        planned);
  }

  bool is_pipelined_layer_ready(uint32_t layer_id,
                                uint16_t request_generation) const {
    return session_->is_layer_ready(layer_id, request_generation);
  }

  std::vector<uint32_t> pipelined_unservable_layers() const {
    return session_->unservable_layers();
  }

  void finish_pipelined_fetch() { session_->finish_request(); }

  void abandon_pipelined_fetch() { session_->abandon_request(); }

  // The session discards immediates whose generation is not the active one,
  // so a late write from an abandoned fetch is dropped here too.
  void land_slot(uint16_t slot_index, uint16_t generation) {
    session_->on_notifications({encode_immediate(generation, slot_index)});
  }

  // Replies quoting a generation that is not active are ignored by the
  // session; the early return only avoids looking up commands that no longer
  // exist.
  void decline_slot(uint16_t slot_index, uint16_t generation) {
    if (!session_->has_active_request() ||
        session_->active_generation() != generation) {
      return;
    }
    for (const auto& [node, command] : session_->pipelined_fetch_commands()) {
      const std::vector<uint16_t> carried =
          pipelined_command_slot_indices(command);
      for (const uint16_t slot : carried) {
        if (slot == slot_index) {
          const std::string reply =
              "n=" + std::to_string(carried.size()) +
              ";accepted=" + std::to_string(carried.size() - 1) +
              ";failed=" + std::to_string(slot_index) + ";bytes=0";
          session_->on_node_reply(node, command, reply, generation);
          return;
        }
      }
    }
    throw std::invalid_argument("slot " + std::to_string(slot_index) +
                                " is not in the active fetch");
  }

 private:
  SlotPlanner planner_;
  NodeRegistry registry_;
  std::unique_ptr<PipelinedFetchSession> session_;
};

}  // namespace

PYBIND11_MODULE(fabric_free_session, m) {
  py::register_exception<lmcache::connector::rdma::PlanTooLargeError>(
      m, "PipelinedPlanTooLargeError", PyExc_RuntimeError);

  py::class_<FabricFreeConnector>(m, "FabricFreeConnector")
      .def(py::init<const std::vector<std::string>&, size_t, uint32_t,
                    uint32_t>(),
           py::arg("node_names"), py::arg("window_bytes"), py::arg("max_slots"),
           py::arg("max_sinks") = 256)
      .def("pipelined_fetch_ready", &FabricFreeConnector::pipelined_fetch_ready)
      .def("pipelined_fetch_init_error",
           &FabricFreeConnector::pipelined_fetch_init_error)
      .def("pipelined_max_slots_per_request",
           &FabricFreeConnector::pipelined_max_slots_per_request)
      .def("issue_pipelined_fetch_by_slots",
           &FabricFreeConnector::issue_pipelined_fetch_by_slots,
           py::arg("node_names"), py::arg("slots"))
      .def("is_pipelined_layer_ready",
           &FabricFreeConnector::is_pipelined_layer_ready, py::arg("layer_id"),
           py::arg("request_generation"))
      .def("pipelined_unservable_layers",
           &FabricFreeConnector::pipelined_unservable_layers)
      .def("finish_pipelined_fetch",
           &FabricFreeConnector::finish_pipelined_fetch)
      .def("abandon_pipelined_fetch",
           &FabricFreeConnector::abandon_pipelined_fetch)
      .def("land_slot", &FabricFreeConnector::land_slot, py::arg("slot_index"),
           py::arg("generation"))
      .def("decline_slot", &FabricFreeConnector::decline_slot,
           py::arg("slot_index"), py::arg("generation"));
}
