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
#include "shard_plan.h"

namespace {

using lmcache::connector::rdma::ChunkNodeBinding;
using lmcache::connector::rdma::ChunkPlacement;
using lmcache::connector::rdma::issue_pipelined_fetch;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::KernelGroupLayoutInput;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::object_group_bytes;
using lmcache::connector::rdma::object_group_layouts_from_inputs;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::ObjectGroupLayoutInput;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::PipelinedNodeInfoSender;
using lmcache::connector::rdma::RequestPlan;
using lmcache::connector::rdma::SlotDigest;
using lmcache::connector::rdma::SlotPlanner;

constexpr size_t kRecordCap = 1u << 20;
constexpr size_t kWriteCap = 1u << 20;
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

KernelGroupLayout attention_group(uint32_t first_layer, uint32_t layers) {
  KernelGroupLayout group;
  for (uint32_t i = 0; i < layers; ++i) {
    group.layer_indices.push_back(first_layer + i);
  }
  group.kv_size = 2;
  group.num_slots = 256;
  group.hidden_dim = 8 * 128;
  group.element_size = 2;
  return group;
}

ObjectGroupLayout single_group_layout(uint32_t layers) {
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(attention_group(0, layers));
  return layout;
}

void register_node(NodeRegistry& registry, const std::string& name,
                   uint64_t region) {
  NodeRegistration registration;
  registration.node_name = name;
  registration.region = region;
  registration.valid = true;
  registry.set(registration);
}

PipelinedFetchSession make_session(const SlotPlanner& planner,
                                   const NodeRegistry& registry) {
  return PipelinedFetchSession(planner, registry, "kv", kRecordCap, kWriteCap,
                               kWindowBytes, 4096);
}

std::vector<SlotDigest> digests_for_plan(
    const SlotPlanner& planner, const RequestPlan& plan,
    const std::vector<ChunkPlacement>& placements, size_t max_record_bytes,
    const std::string& prefix) {
  std::vector<SlotDigest> out;
  std::map<uint32_t, std::vector<const ChunkPlacement*>> by_group;
  for (const ChunkPlacement& placement : placements) {
    by_group[placement.object_group_id].push_back(&placement);
  }
  size_t slot_index = 0;
  for (const uint32_t layer_id : planner.layer_ids()) {
    const uint32_t group_id = planner.object_group_of_layer(layer_id);
    const auto group_placements = by_group.find(group_id);
    if (group_placements == by_group.end()) {
      continue;
    }
    const std::vector<lmcache::connector::rdma::ByteRange> planes =
        planner.layer_plane_ranges(layer_id);
    for (const ChunkPlacement* placement : group_placements->second) {
      for (uint32_t plane = 0; plane < planes.size(); ++plane) {
        const size_t record_bytes = lmcache::connector::plane_segment_bytes(
            planes[plane].length, max_record_bytes);
        const size_t pieces =
            (planes[plane].length + record_bytes - 1) / record_bytes;
        for (size_t piece = 0; piece < pieces; ++piece) {
          SlotDigest entry;
          entry.chunk_id = placement->chunk_id;
          entry.layer_id = layer_id;
          entry.plane = plane;
          entry.piece = static_cast<uint32_t>(piece);
          entry.digest_hex = prefix + std::to_string(slot_index);
          out.push_back(entry);
          ++slot_index;
        }
      }
    }
  }
  if (out.size() != plan.slot_count()) {
    throw std::runtime_error("digests_for_plan: slot count mismatch");
  }
  return out;
}

void test_transport_failure_marks_slots_unservable() {
  std::cout << "transport failure marks node slots unservable\n";

  const ObjectGroupLayout layout = single_group_layout(1);
  const SlotPlanner planner({layout});
  const size_t object_bytes = object_group_bytes(layout);
  NodeRegistry registry;
  register_node(registry, "node-a", 10);
  register_node(registry, "node-b", 20);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {
      ChunkPlacement{0, 0, 0},
      ChunkPlacement{1, 0, object_bytes},
  };
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}, {1, "node-b"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  const std::vector<SlotDigest> digests =
      digests_for_plan(planner, plan, placements, kRecordCap, "aa");

  const PipelinedNodeInfoSender send_info = [](const std::string& node_name,
                                               const std::string& /*command*/) {
    if (node_name == "node-b") {
      throw std::runtime_error("transport down");
    }
    return std::string("n=2;accepted=2;failed=;bytes=1");
  };

  issue_pipelined_fetch(session, send_info, placements, nodes, digests);
  check(session.unservable_layers() == std::vector<uint32_t>({0}),
        "node-b transport failure marks its layer slots unservable");
  check(!session.is_layer_ready(0),
        "layer 0 cannot complete when a chunk's node is unreachable");
  session.finish_request();
}

void test_throw_mid_issue_clears_active_request() {
  std::cout << "throw mid issue clears active request\n";

  const ObjectGroupLayout layout = single_group_layout(1);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  const std::vector<SlotDigest> digests =
      digests_for_plan(planner, plan, placements, kRecordCap, "bb");

  const PipelinedNodeInfoSender send_info = [](const std::string& /*node*/,
                                               const std::string& /*command*/) {
    return std::string("not-a-valid-reply");
  };

  bool threw = false;
  try {
    issue_pipelined_fetch(session, send_info, placements, nodes, digests);
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

}  // namespace

int main() {
  try {
    test_transport_failure_marks_slots_unservable();
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
