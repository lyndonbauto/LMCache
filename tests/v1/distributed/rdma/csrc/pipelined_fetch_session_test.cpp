// SPDX-License-Identifier: Apache-2.0
//
// Driver tests for the pipelined fetch session.

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "kv_sink_client.h"
#include "layer_pipeline.h"
#include "pipelined_fetch_session.h"
#include "shard_plan.h"
#include "slot_planner.h"

namespace {

using lmcache::connector::plane_segment_bytes;
using lmcache::connector::rdma::ChunkNodeBinding;
using lmcache::connector::rdma::ChunkPlacement;
using lmcache::connector::rdma::encode_immediate;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::object_group_bytes;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::SlotDigest;
using lmcache::connector::rdma::SlotPlanner;

constexpr size_t kRecordCap = 1u << 20;
constexpr size_t kWriteCap = 1u << 20;
constexpr size_t kWindowBytes = 1u << 24;
constexpr uint32_t kNotifyCap = 65536;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

KernelGroupLayout attention_group(uint32_t layers, uint32_t kv_size = 1,
                                  size_t num_slots = 8) {
  KernelGroupLayout group;
  for (uint32_t i = 0; i < layers; ++i) {
    group.layer_indices.push_back(i);
  }
  group.kv_size = kv_size;
  group.num_slots = num_slots;
  group.hidden_dim = 64;
  group.element_size = 2;
  return group;
}

ObjectGroupLayout single_group_layout(uint32_t layers, uint32_t kv_size = 1,
                                      size_t num_slots = 8) {
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(attention_group(layers, kv_size, num_slots));
  return layout;
}

void register_node(NodeRegistry& registry, const std::string& name,
                   uint64_t region) {
  NodeRegistration reg;
  reg.node_name = name;
  reg.region = region;
  reg.valid = true;
  registry.set(reg);
}

PipelinedFetchSession make_session(const SlotPlanner& planner,
                                   const NodeRegistry& registry) {
  return PipelinedFetchSession(planner, registry, "kv", kRecordCap, kWriteCap,
                               kWindowBytes, kNotifyCap);
}

std::vector<SlotDigest> digests_for_plan(
    const SlotPlanner& planner,
    const lmcache::connector::rdma::RequestPlan& plan,
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
        const size_t record_bytes =
            plane_segment_bytes(planes[plane].length, max_record_bytes);
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
  if (slot_index != plan.slot_count()) {
    throw std::runtime_error("digests_for_plan: slot count mismatch");
  }
  return out;
}

std::vector<uint16_t> slot_indices_in_command(const std::string& command) {
  std::vector<uint16_t> slots;
  const std::string marker = "sinks=";
  const size_t start = command.find(marker);
  if (start == std::string::npos) {
    return slots;
  }
  std::string sinks = command.substr(start + marker.size());
  size_t pos = 0;
  while (pos < sinks.size()) {
    const size_t comma = sinks.find(',', pos);
    const std::string token = sinks.substr(
        pos, comma == std::string::npos ? std::string::npos : comma - pos);
    const size_t hash = token.rfind('#');
    if (hash != std::string::npos) {
      slots.push_back(
          static_cast<uint16_t>(std::stoul(token.substr(hash + 1))));
    }
    if (comma == std::string::npos) {
      break;
    }
    pos = comma + 1;
  }
  return slots;
}

void test_multi_node_request_shares_one_slot_index_space() {
  std::cout << "multi-node request shares one slot index space\n";

  const ObjectGroupLayout layout = single_group_layout(2);
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
  const std::vector<ChunkNodeBinding> nodes = {
      {0, "node-a"},
      {1, "node-b"},
  };

  const auto expected_plan =
      planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  const uint16_t gen2 = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, expected_plan, placements, kRecordCap, "aa"));

  const std::map<std::string, std::string> commands =
      session.pipelined_fetch_commands();
  check(commands.size() == 2, "two nodes each receive one command");

  const std::vector<uint16_t> slots_a =
      slot_indices_in_command(commands.at("node-a"));
  const std::vector<uint16_t> slots_b =
      slot_indices_in_command(commands.at("node-b"));
  check(slots_a == std::vector<uint16_t>({0, 2}),
        "node-a carries slots 0 and 2 from the shared numbering");
  check(slots_b == std::vector<uint16_t>({1, 3}),
        "node-b carries slots 1 and 3");

  check(session.active_generation() == gen2,
        "generation is returned to caller");
  session.finish_request();
}

void test_one_node_two_chunks_orders_sinks_by_slot() {
  std::cout << "one node two chunks orders sinks by slot\n";

  const ObjectGroupLayout layout = single_group_layout(2);
  const SlotPlanner planner({layout});
  const size_t object_bytes = object_group_bytes(layout);
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {
      ChunkPlacement{0, 0, 0},
      ChunkPlacement{5, 0, object_bytes},
  };
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}, {5, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "bb"));

  const std::vector<uint16_t> slots =
      slot_indices_in_command(session.pipelined_fetch_commands().at("node-a"));
  check(std::is_sorted(slots.begin(), slots.end()),
        "sink slot suffixes are ascending");
  session.finish_request();
}

void test_failed_slot_makes_one_layer_recompute() {
  std::cout << "failed slot makes one layer recompute\n";

  const ObjectGroupLayout layout = single_group_layout(2);
  const SlotPlanner planner({layout});

  NodeRegistry registry;
  register_node(registry, "node-a", 1);

  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {
      ChunkPlacement{0, 0, 0},
  };
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan =
      planner.plan_request(placements, kRecordCap, kWriteCap, 0x11);
  const uint16_t gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "dd"));

  session.on_node_reply("node-a", "n=2;accepted=1;failed=0;bytes=1", gen);

  check(!session.is_layer_ready(0),
        "layer 0 is not ready when one of its slots was declined");
  check(session.unservable_layers() == std::vector<uint32_t>({0}),
        "layer 0 is listed as unservable");

  session.on_notifications({encode_immediate(session.active_generation(), 1)});

  check(session.is_layer_ready(1),
        "layer 1 still completes when its slots land");
  check(!session.is_layer_ready(0),
        "layer 0 stays unready after a partial arrival");

  session.finish_request();
}

void test_stale_generation_notifications_are_ignored() {
  std::cout << "stale generation notifications are ignored\n";

  const ObjectGroupLayout layout = single_group_layout(1);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto first_plan =
      planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  const uint16_t first_gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, first_plan, placements, kRecordCap, "a"));

  session.finish_request();

  const uint16_t second_gen = session.begin_request(
      placements, nodes,
      digests_for_plan(
          planner, planner.plan_request(placements, kRecordCap, kWriteCap, 0),
          placements, kRecordCap, "b"));

  session.on_notifications({encode_immediate(first_gen, 0)});
  check(!session.is_layer_ready(0),
        "an immediate from a finished request does not advance the new one");

  session.on_notifications({encode_immediate(second_gen, 0)});
  check(session.is_layer_ready(0),
        "an immediate matching the active generation completes the layer");

  session.finish_request();
}

void test_abandon_makes_late_reply_harmless() {
  std::cout << "abandon makes late reply harmless\n";

  const ObjectGroupLayout layout = single_group_layout(1);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  const uint16_t abandoned_gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "c"));
  session.abandon_request();

  const uint16_t gen = session.begin_request(
      placements, nodes,
      digests_for_plan(
          planner, planner.plan_request(placements, kRecordCap, kWriteCap, 0),
          placements, kRecordCap, "d"));
  check(session.has_active_request(), "a new request can start after abandon");

  session.on_node_reply("node-a", "n=1;accepted=0;failed=0;bytes=0",
                        abandoned_gen);
  check(session.unservable_layers().empty(),
        "a late reply after the next begin_request does not poison readiness");

  check(session.active_generation() == gen, "new generation is allocated");
  session.finish_request();
}

void test_kv_size_two_requires_both_planes() {
  std::cout << "kv_size two requires both planes before layer ready\n";

  const ObjectGroupLayout layout = single_group_layout(1, 2, 4);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, 256, kWriteCap, 0);
  const uint16_t gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, 256, "kv"));

  check(plan.slot_count() >= 2, "small record cap splits a plane into pieces");

  session.on_notifications({encode_immediate(gen, 0)});
  check(!session.is_layer_ready(0),
        "one plane landing does not complete the layer when kv_size is 2");
  session.on_notifications({encode_immediate(gen, 1)});
  check(session.is_layer_ready(0), "layer 0 completes once both planes land");
  session.finish_request();
}

void test_begin_request_rejects_missing_digest() {
  std::cout << "begin_request rejects missing digest\n";
  const ObjectGroupLayout layout = single_group_layout(1);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);
  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  bool threw = false;
  try {
    session.begin_request(placements, nodes, {});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "missing digest throws invalid_argument");
}

void test_begin_request_rejects_second_active() {
  std::cout << "begin_request rejects second active request\n";
  const ObjectGroupLayout layout = single_group_layout(1);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);
  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "x"));
  bool threw = false;
  try {
    session.begin_request(
        placements, nodes,
        digests_for_plan(planner, plan, placements, kRecordCap, "y"));
  } catch (const std::runtime_error&) {
    threw = true;
  }
  check(threw, "second begin_request throws runtime_error");
  session.finish_request();
}

}  // namespace

int main() {
  try {
    test_multi_node_request_shares_one_slot_index_space();
    test_one_node_two_chunks_orders_sinks_by_slot();
    test_failed_slot_makes_one_layer_recompute();
    test_stale_generation_notifications_are_ignored();
    test_abandon_makes_late_reply_harmless();
    test_kv_size_two_requires_both_planes();
    test_begin_request_rejects_missing_digest();
    test_begin_request_rejects_second_active();
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
