// SPDX-License-Identifier: Apache-2.0
//
// Device-free checks for notification depth budgeting and session enforcement.

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "layer_pipeline.h"
#include "notification_depth.h"
#include "pipelined_fetch_session.h"
#include "shard_plan.h"
#include "slot_planner.h"

namespace {

using lmcache::connector::rdma::ChunkNodeBinding;
using lmcache::connector::rdma::ChunkPlacement;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::notification_depth_budget;
using lmcache::connector::rdma::NotificationDepthBudget;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::RdmaDeviceCaps;
using lmcache::connector::rdma::SlotPlanner;

constexpr size_t kWindowBytes = 1u << 20;
constexpr size_t kRecordCap = 4096;
constexpr size_t kWriteCap = 1u << 20;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

ObjectGroupLayout two_layer_layout() {
  KernelGroupLayout group;
  group.layer_indices = {0, 1};
  group.kv_size = 2;
  group.num_slots = 4;
  group.hidden_dim = 64;
  group.element_size = 2;
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(group);
  return layout;
}

void register_node(NodeRegistry& registry) {
  NodeRegistration reg;
  reg.node_name = "node-a";
  reg.region = 1;
  reg.valid = true;
  registry.set(reg);
}

void test_budget_clamps_to_device_caps() {
  std::cout << "budget clamps to device caps\n";
  RdmaDeviceCaps caps;
  caps.max_recv_wr_per_qp = 128;
  caps.max_cq_entries = 256;
  const NotificationDepthBudget budget =
      notification_depth_budget(kWindowBytes, kRecordCap, caps);
  check(budget.desired_depth > budget.effective_depth,
        "window-derived desired depth exceeds injected device recv cap");
  check(budget.effective_depth == 128,
        "effective depth equals max_recv_wr_per_qp");
  check(budget.max_slots_per_request == budget.effective_depth,
        "max slots per request follows effective depth");
  check(budget.effective_depth <= caps.max_recv_wr_per_qp,
        "effective depth never exceeds max_recv_wr_per_qp");
  check(budget.effective_depth <= caps.max_cq_entries,
        "effective depth never exceeds max_cq");
}

void test_budget_accepts_when_device_is_loose() {
  std::cout << "budget accepts when device is loose\n";
  RdmaDeviceCaps caps;
  caps.max_recv_wr_per_qp = 65536;
  caps.max_cq_entries = 65536;
  const NotificationDepthBudget budget =
      notification_depth_budget(kWindowBytes, kRecordCap, caps);
  check(budget.desired_depth == budget.effective_depth,
        "loose device leaves desired depth unchanged");
  check(budget.max_slots_per_request == budget.effective_depth,
        "max slots equals effective depth");
}

void test_begin_request_rejects_plan_above_device_slot_cap() {
  std::cout << "begin_request rejects plan above device slot cap\n";
  const SlotPlanner planner({two_layer_layout()});
  NodeRegistry registry;
  register_node(registry);
  constexpr uint32_t kDeviceSlotCap = 2;
  PipelinedFetchSession session(planner, registry, "kv", kRecordCap, kWriteCap,
                                kWindowBytes, kDeviceSlotCap);
  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  check(plan.slot_count() > kDeviceSlotCap,
        "fixture plan exceeds injected device slot cap");
  bool threw = false;
  std::string message;
  try {
    session.begin_request(placements, nodes, {});
  } catch (const std::runtime_error& e) {
    threw = true;
    message = e.what();
  }
  check(threw, "oversized plan throws runtime_error");
  check(message.find(std::to_string(plan.slot_count())) != std::string::npos,
        "error names plan slot count");
  check(message.find(std::to_string(kDeviceSlotCap)) != std::string::npos,
        "error names device slot cap");
}

void test_begin_request_accepts_plan_within_device_slot_cap() {
  std::cout << "begin_request accepts plan within device slot cap\n";
  KernelGroupLayout group;
  group.layer_indices = {0};
  group.kv_size = 1;
  group.num_slots = 4;
  group.hidden_dim = 64;
  group.element_size = 2;
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(group);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry);
  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  PipelinedFetchSession session(planner, registry, "kv", kRecordCap, kWriteCap,
                                kWindowBytes,
                                static_cast<uint32_t>(plan.slot_count()));
  lmcache::connector::rdma::SlotDigest digest;
  digest.chunk_id = 0;
  digest.layer_id = 0;
  digest.plane = 0;
  digest.piece = 0;
  digest.digest_hex = "abc";
  bool threw = false;
  try {
    session.begin_request(placements, {{0, "node-a"}}, {digest});
  } catch (const std::exception&) {
    threw = true;
  }
  check(!threw, "plan within cap begins without throwing");
  session.finish_request();
}

}  // namespace

int main() {
  try {
    test_budget_clamps_to_device_caps();
    test_budget_accepts_when_device_is_loose();
    test_begin_request_rejects_plan_above_device_slot_cap();
    test_begin_request_accepts_plan_within_device_slot_cap();
  } catch (const std::exception& e) {
    std::cerr << "EXCEPTION: " << e.what() << "\n";
    return 1;
  }

  if (failures != 0) {
    std::cout << "\n" << failures << " check(s) FAILED\n";
    return 1;
  }
  std::cout << "\nPASS\n";
  return 0;
}
