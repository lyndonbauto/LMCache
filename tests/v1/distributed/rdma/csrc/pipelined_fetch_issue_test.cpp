// SPDX-License-Identifier: Apache-2.0
//
// Device-free tests for pipelined fetch issue and layout conversion.

#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "memory_layout_conversion.h"
#include "pipelined_fetch_issue.h"
#include "pipelined_fetch_session.h"

namespace {

using lmcache::connector::rdma::issue_planned_fetch;
using lmcache::connector::rdma::KernelGroupLayoutInput;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::object_group_layouts_from_inputs;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::ObjectGroupLayoutInput;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::PipelinedNodeInfoSender;
using lmcache::connector::rdma::PlannedSlot;

constexpr size_t kWindowBytes = 1u << 22;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

void register_node(NodeRegistry& registry, const std::string& name,
                   uint64_t region) {
  NodeRegistration registration;
  registration.node_name = name;
  registration.region = region;
  registration.valid = true;
  registry.set(registration);
}

PipelinedFetchSession make_session(const NodeRegistry& registry) {
  return PipelinedFetchSession(registry, "kv", kWindowBytes, 4096);
}

PlannedSlot slot(const std::string& node, uint32_t layer, size_t offset) {
  PlannedSlot s;
  s.node_name = node;
  s.digest_hex = "d" + std::to_string(offset);
  s.layer_id = layer;
  s.offset = offset;
  s.length = 64;
  return s;
}

void test_throw_mid_issue_clears_active_request() {
  std::cout << "throw mid issue clears active request\n";

  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(registry);

  const PipelinedNodeInfoSender send_info = [](const std::string& /*node*/,
                                               const std::string& /*command*/) {
    return std::string("not-a-valid-reply");
  };

  bool threw = false;
  try {
    issue_planned_fetch(session, send_info, {slot("node-a", 0, 0)});
  } catch (const std::exception&) {
    threw = true;
  }
  check(threw, "malformed reply throws");
  check(!session.has_active_request(),
        "active request is abandoned after issue failure");
}

void test_memory_layout_conversion_reads_shapes() {
  std::cout << "memory layout conversion reads shapes\n";

  std::map<uint32_t, ObjectGroupLayoutInput> inputs;
  ObjectGroupLayoutInput group;
  KernelGroupLayoutInput kernel;
  kernel.shape = {2, 4, 256, 1024};
  kernel.dtype = "torch.float16";
  kernel.layer_indices = {0, 1, 2, 3};
  group.kernel_groups.push_back(kernel);
  inputs.emplace(0, group);

  const std::vector<ObjectGroupLayout> layouts =
      object_group_layouts_from_inputs(inputs);
  check(layouts.size() == 1, "one object group converted");
  check(layouts[0].kernel_groups[0].kv_size == 2, "kv_size from 4D shape");
  check(layouts[0].kernel_groups[0].layer_indices.size() == 4,
        "layer indices preserved");
}

void test_planned_transport_failure_marks_only_that_nodes_slots() {
  std::cout << "planned transport failure marks only that node's slots\n";

  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  register_node(registry, "node-b", 2);
  PipelinedFetchSession session = make_session(registry);

  // Layer 0's records sit on both nodes; layer 1's only on node-a.
  const std::vector<PlannedSlot> slots = {
      slot("node-a", 0, 0), slot("node-b", 0, 64), slot("node-a", 1, 128)};

  const PipelinedNodeInfoSender send_info = [](const std::string& node,
                                               const std::string& command) {
    if (node == "node-b") {
      throw std::runtime_error("connection refused");
    }
    const size_t n =
        lmcache::connector::rdma::pipelined_command_slot_indices(command)
            .size();
    return "n=" + std::to_string(n) + ";accepted=" + std::to_string(n) +
           ";bytes=0";
  };

  issue_planned_fetch(session, send_info, slots);
  check(session.has_active_request(), "a node failure does not abandon");
  check(session.unservable_layers() == std::vector<uint32_t>({0}),
        "only the layer with a slot on the unreachable node is unservable");
  check(!session.is_layer_ready(0),
        "layer 0 cannot complete when one of its nodes is unreachable");
  session.finish_request();
}

}  // namespace

int main() {
  try {
    test_planned_transport_failure_marks_only_that_nodes_slots();
    test_throw_mid_issue_clears_active_request();
    test_memory_layout_conversion_reads_shapes();
  } catch (const std::exception& e) {
    std::cout << "UNHANDLED: " << e.what() << "\n";
    return 1;
  }

  if (failures == 0) {
    std::cout << "PASS pipelined_fetch_issue_test\n";
    return 0;
  }
  std::cout << failures << " failure(s)\n";
  return 1;
}
