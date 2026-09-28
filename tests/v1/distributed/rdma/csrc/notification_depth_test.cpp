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
#include "slot_planner.h"

namespace {

using lmcache::connector::rdma::ChunkPlacement;
using lmcache::connector::rdma::FetchSlot;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::kMaxSlotsPerRequest;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::notification_depth_budget;
using lmcache::connector::rdma::NotificationDepthBudget;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::PlannedSlot;
using lmcache::connector::rdma::PlanTooLargeError;
using lmcache::connector::rdma::RdmaDeviceCaps;
using lmcache::connector::rdma::RequestPlan;
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
      notification_depth_budget(kWindowBytes, caps);
  check(budget.desired_depth > budget.effective_depth,
        "window-derived desired depth exceeds injected device recv cap");
  check(budget.effective_depth == 128,
        "effective depth equals max_recv_wr_per_qp");
  check(budget.max_slots_per_request == budget.effective_depth,
        "max slots per request follows effective depth");
  check(budget.effective_depth <= caps.max_recv_wr_per_qp,
        "effective depth never exceeds max_recv_wr_per_qp");
  check(budget.effective_depth <= caps.max_cq_entries,
        "effective depth never exceeds max_cqe");
}

void test_budget_accepts_when_device_is_loose() {
  std::cout << "budget accepts when device is loose\n";
  RdmaDeviceCaps caps;
  caps.max_recv_wr_per_qp = 65536;
  caps.max_cq_entries = 65536;
  const NotificationDepthBudget budget =
      notification_depth_budget(kWindowBytes, caps);
  check(budget.desired_depth == budget.effective_depth,
        "loose device leaves desired depth unchanged");
  check(budget.max_slots_per_request == budget.effective_depth,
        "max slots equals effective depth");
}

void test_budget_covers_slots_smaller_than_the_record_cap() {
  std::cout << "budget covers slots smaller than the record cap\n";
  RdmaDeviceCaps loose;
  loose.max_recv_wr_per_qp = 1u << 20;
  loose.max_cq_entries = 1u << 20;
  // A 64 KiB window against a ~1 MiB record cap: a plan of 2 KiB plane
  // pieces still has dozens of slots.
  const NotificationDepthBudget small_window =
      notification_depth_budget(64u << 10, loose);
  check(small_window.max_slots_per_request == kMaxSlotsPerRequest,
        "a window smaller than one record keeps the full slot-index range");
  const NotificationDepthBudget tiny_window =
      notification_depth_budget(100, loose);
  check(tiny_window.max_slots_per_request == 100,
        "a window below the slot-index range gets one slot per byte");
}

void test_budget_splits_depth_across_windows() {
  std::cout << "budget splits the depth evenly across windows\n";
  RdmaDeviceCaps loose;
  loose.max_recv_wr_per_qp = 1u << 20;
  loose.max_cq_entries = 1u << 20;
  const NotificationDepthBudget one =
      notification_depth_budget(kWindowBytes, loose);
  const NotificationDepthBudget four =
      notification_depth_budget(kWindowBytes, loose, 4);
  check(four.desired_depth == 4 * one.desired_depth,
        "four windows ask for four windows' worth of receives");
  check(four.max_slots_per_request == one.max_slots_per_request,
        "on a loose device each window keeps a full window's slots");

  RdmaDeviceCaps tight;
  tight.max_recv_wr_per_qp = 100;
  tight.max_cq_entries = 1u << 20;
  const NotificationDepthBudget clamped =
      notification_depth_budget(kWindowBytes, tight, 4);
  check(clamped.effective_depth == 100, "the device still clamps the total");
  check(clamped.max_slots_per_request == 25,
        "each of four windows gets a quarter of the clamped depth");
}

// Slots of `layout` for one chunk at offset 0, all on node-a.
std::vector<PlannedSlot> one_chunk_slots(const ObjectGroupLayout& layout) {
  const SlotPlanner planner({layout});
  const RequestPlan plan =
      planner.plan_request({ChunkPlacement{0, 0, 0}}, kRecordCap, kWriteCap, 0);
  std::vector<PlannedSlot> slots;
  for (size_t i = 0; i < plan.slot_count(); ++i) {
    const FetchSlot& fetch = plan.slot(static_cast<uint16_t>(i));
    PlannedSlot slot;
    slot.node_name = "node-a";
    slot.digest_hex = "d" + std::to_string(i);
    slot.layer_id = fetch.layer_id;
    slot.offset = fetch.offset;
    slot.length = fetch.length;
    slots.push_back(slot);
  }
  return slots;
}

void test_begin_request_rejects_plan_above_device_slot_cap() {
  std::cout << "begin_request_from_slots rejects plan above device slot cap\n";
  NodeRegistry registry;
  register_node(registry);
  constexpr uint32_t kDeviceSlotCap = 2;
  PipelinedFetchSession session(registry, "kv", kWindowBytes, kDeviceSlotCap);
  const std::vector<PlannedSlot> slots = one_chunk_slots(two_layer_layout());
  check(slots.size() > kDeviceSlotCap,
        "fixture plan exceeds injected device slot cap");
  bool threw = false;
  std::string message;
  try {
    session.begin_request_from_slots(slots);
  } catch (const PlanTooLargeError& e) {
    threw = true;
    message = e.what();
  }
  check(threw, "oversized plan throws PlanTooLargeError");
  check(message.find(std::to_string(slots.size())) != std::string::npos,
        "error names plan slot count");
  check(message.find(std::to_string(kDeviceSlotCap)) != std::string::npos,
        "error names device slot cap");
  check(!session.has_active_request(), "nothing was begun");
}

void test_begin_request_accepts_plan_within_device_slot_cap() {
  std::cout << "begin_request_from_slots accepts plan within device slot cap\n";
  NodeRegistry registry;
  register_node(registry);
  const std::vector<PlannedSlot> slots = one_chunk_slots(two_layer_layout());
  PipelinedFetchSession session(registry, "kv", kWindowBytes,
                                static_cast<uint32_t>(slots.size()));
  bool threw = false;
  try {
    session.begin_request_from_slots(slots);
  } catch (const std::exception&) {
    threw = true;
  }
  check(!threw, "plan exactly at the cap begins without throwing");
  session.finish_request();
}

}  // namespace

int main() {
  try {
    test_budget_clamps_to_device_caps();
    test_budget_accepts_when_device_is_loose();
    test_budget_covers_slots_smaller_than_the_record_cap();
    test_budget_splits_depth_across_windows();
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
