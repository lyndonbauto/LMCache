// SPDX-License-Identifier: Apache-2.0
//
// Driver tests for the pipelined fetch session.

#include <algorithm>
#include <cstdint>
#include <functional>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "kv_sink_client.h"
#include "layer_pipeline.h"
#include "pipelined_fetch_issue.h"
#include "pipelined_fetch_session.h"
#include "shard_plan.h"
#include "slot_planner.h"

namespace {

using lmcache::connector::plane_segment_bytes;
using lmcache::connector::rdma::ChunkNodeBinding;
using lmcache::connector::rdma::ChunkPlacement;
using lmcache::connector::rdma::encode_immediate;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::kNoGeneration;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::object_group_bytes;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::PlannedSlot;
using lmcache::connector::rdma::PlanTooLargeError;
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
                   uint64_t region, uint32_t max_sinks_per_command = 256) {
  NodeRegistration reg;
  reg.node_name = name;
  reg.region = region;
  reg.max_sinks_per_command = max_sinks_per_command;
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

std::vector<std::string> commands_for_node(
    const std::vector<std::pair<std::string, std::string>>& commands,
    const std::string& node_name) {
  std::vector<std::string> out;
  for (const auto& entry : commands) {
    if (entry.first == node_name) {
      out.push_back(entry.second);
    }
  }
  return out;
}

std::set<uint16_t> all_slots_in_commands(
    const std::vector<std::string>& commands) {
  std::set<uint16_t> slots;
  for (const std::string& command : commands) {
    for (const uint16_t slot : slot_indices_in_command(command)) {
      slots.insert(slot);
    }
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

  const std::vector<std::pair<std::string, std::string>> commands =
      session.pipelined_fetch_commands();
  check(commands.size() == 2, "two nodes each receive one command");

  const std::vector<uint16_t> slots_a =
      slot_indices_in_command(commands_for_node(commands, "node-a").at(0));
  const std::vector<uint16_t> slots_b =
      slot_indices_in_command(commands_for_node(commands, "node-b").at(0));
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

  const std::vector<uint16_t> slots = slot_indices_in_command(
      commands_for_node(session.pipelined_fetch_commands(), "node-a").at(0));
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

  const std::string command =
      commands_for_node(session.pipelined_fetch_commands(), "node-a").at(0);
  session.on_node_reply("node-a", command, "n=2;accepted=1;failed=0;bytes=1",
                        gen);

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

  session.on_node_reply("node-a", "ignored", "n=1;accepted=0;failed=0;bytes=0",
                        abandoned_gen);
  check(session.unservable_layers().empty(),
        "a late reply after the next begin_request does not poison readiness");

  check(session.active_generation() == gen, "new generation is allocated");
  session.finish_request();
}

void test_allocated_generations_are_never_the_reserved_value() {
  std::cout << "allocated generations are never the reserved value\n";

  const ObjectGroupLayout layout = single_group_layout(1);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);

  const uint16_t first_gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "g0"));
  check(first_gen != kNoGeneration,
        "the first request of a fresh session does not get the reserved "
        "generation");
  session.finish_request();

  // 65535 + 1 wraps to the reserved value, which is the only way a live
  // request could otherwise be handed it.
  session.restore_generation_counter(65535);
  const uint16_t last_gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "g1"));
  check(last_gen == 65535, "the counter reaches the top of the 16-bit space");
  session.finish_request();

  const uint16_t wrapped_gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "g2"));
  check(wrapped_gen != kNoGeneration,
        "the generation after a wrap skips the reserved value");
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

void test_chunking_splits_at_exact_multiple_of_limit() {
  std::cout << "chunking splits at exact multiple of limit\n";

  constexpr uint32_t kLimit = 64;
  const ObjectGroupLayout layout = single_group_layout(128);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1, kLimit);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "em"));

  const std::vector<std::string> node_commands =
      commands_for_node(session.pipelined_fetch_commands(), "node-a");
  check(node_commands.size() == 2,
        "128 sinks at limit 64 produce exactly two commands");
  check(slot_indices_in_command(node_commands[0]).size() == kLimit,
        "first command is full");
  check(slot_indices_in_command(node_commands[1]).size() == kLimit,
        "second command is full");
  session.finish_request();
}

void test_chunking_splits_with_remainder() {
  std::cout << "chunking splits with remainder\n";

  constexpr uint32_t kLimit = 64;
  const ObjectGroupLayout layout = single_group_layout(100);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1, kLimit);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "rm"));

  const std::vector<std::string> node_commands =
      commands_for_node(session.pipelined_fetch_commands(), "node-a");
  check(node_commands.size() == 2,
        "100 sinks at limit 64 produce two commands");
  check(slot_indices_in_command(node_commands[0]).size() == kLimit,
        "first command carries a full chunk");
  check(slot_indices_in_command(node_commands[1]).size() == 36,
        "second command carries the remainder");
  session.finish_request();
}

void test_chunking_union_covers_every_node_sink_once() {
  std::cout << "chunking union covers every node sink once\n";

  constexpr uint32_t kLimit = 30;
  const ObjectGroupLayout layout = single_group_layout(95);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1, kLimit);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "un"));

  std::set<uint16_t> expected;
  for (const uint16_t slot : plan.slots_for_chunk(0)) {
    expected.insert(slot);
  }
  const std::set<uint16_t> got = all_slots_in_commands(
      commands_for_node(session.pipelined_fetch_commands(), "node-a"));
  check(got == expected, "command slots equal the unchunked set");
  check(got.size() == plan.slot_count(), "no duplicate slot indices");
  session.finish_request();
}

void test_chunking_preserves_ascending_slot_order_across_commands() {
  std::cout << "chunking preserves ascending slot order across commands\n";

  constexpr uint32_t kLimit = 40;
  const ObjectGroupLayout layout = single_group_layout(125);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1, kLimit);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "ord"));

  std::vector<uint16_t> sequence;
  for (const std::string& command :
       commands_for_node(session.pipelined_fetch_commands(), "node-a")) {
    const std::vector<uint16_t> slots = slot_indices_in_command(command);
    check(std::is_sorted(slots.begin(), slots.end()),
          "each command keeps sinks sorted by slot");
    sequence.insert(sequence.end(), slots.begin(), slots.end());
  }
  check(std::is_sorted(sequence.begin(), sequence.end()),
        "concatenated command order is globally ascending");
  check(!sequence.empty() && sequence.front() == 0,
        "first command still holds the earliest slots");
  session.finish_request();
}

void test_two_nodes_chunk_with_independent_limits() {
  std::cout << "two nodes chunk with independent limits\n";

  const ObjectGroupLayout layout = single_group_layout(35);
  const SlotPlanner planner({layout});
  const size_t object_bytes = object_group_bytes(layout);
  NodeRegistry registry;
  register_node(registry, "node-a", 10, 30);
  register_node(registry, "node-b", 20, 10);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {
      ChunkPlacement{0, 0, 0},
      ChunkPlacement{1, 0, object_bytes},
  };
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}, {1, "node-b"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "ind"));

  const auto commands = session.pipelined_fetch_commands();
  check(commands_for_node(commands, "node-a").size() == 2,
        "node-a limit 30 splits 35 sinks into two commands");
  check(commands_for_node(commands, "node-b").size() == 4,
        "node-b limit 10 splits 35 sinks into four commands");
  session.finish_request();
}

void test_rejected_second_command_marks_unservable() {
  std::cout << "rejected second command marks unservable\n";

  constexpr uint32_t kLimit = 256;
  const ObjectGroupLayout layout = single_group_layout(300);
  const SlotPlanner planner({layout});
  NodeRegistry registry;
  register_node(registry, "node-a", 1, kLimit);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  const uint16_t gen = session.begin_request(
      placements, nodes,
      digests_for_plan(planner, plan, placements, kRecordCap, "rej"));

  const std::vector<std::string> node_commands =
      commands_for_node(session.pipelined_fetch_commands(), "node-a");
  check(node_commands.size() == 2, "300 sinks split at 256");

  session.on_node_reply("node-a", node_commands[0],
                        "n=256;accepted=256;bytes=1", gen);
  session.on_node_reply(
      "node-a", node_commands[1],
      lmcache::connector::rdma::declined_reply_for_command(node_commands[1]),
      gen);

  check(session.unservable_layers().size() == 44,
        "declined second command marks its layers unservable");
  check(!session.is_layer_ready(299),
        "a layer whose slots were declined is not ready");
  check(std::find(session.unservable_layers().begin(),
                  session.unservable_layers().end(),
                  256) != session.unservable_layers().end(),
        "a slot from the rejected command appears among unservable layers");
  session.finish_request();
}

PlannedSlot planned(const std::string& node, uint32_t layer, size_t offset,
                    const std::string& digest, size_t length = 64) {
  PlannedSlot slot;
  slot.node_name = node;
  slot.digest_hex = digest;
  slot.layer_id = layer;
  slot.offset = offset;
  slot.length = length;
  return slot;
}

// Slots of one chunk whose records hash to different nodes, as Aerospike
// places them: layer 0 on node-a then node-b, layer 1 on node-b then node-a.
std::vector<PlannedSlot> one_chunk_across_two_nodes() {
  return {
      planned("node-a", 0, 0, "d0"),
      planned("node-b", 0, 64, "d1"),
      planned("node-b", 1, 128, "d2"),
      planned("node-a", 1, 192, "d3"),
  };
}

void test_planned_slots_go_to_their_own_node() {
  std::cout << "planned slots go to their own node, not their chunk's\n";

  const SlotPlanner planner({single_group_layout(1)});
  NodeRegistry registry;
  register_node(registry, "node-a", 10);
  register_node(registry, "node-b", 20);
  PipelinedFetchSession session = make_session(planner, registry);

  session.begin_request_from_slots(one_chunk_across_two_nodes());
  const auto commands = session.pipelined_fetch_commands();

  const auto a = commands_for_node(commands, "node-a");
  const auto b = commands_for_node(commands, "node-b");
  check(a.size() == 1 && b.size() == 1, "each node gets one command");
  check(slot_indices_in_command(a.at(0)) == std::vector<uint16_t>({0, 3}),
        "node-a is asked for exactly the slots whose records it holds");
  check(slot_indices_in_command(b.at(0)) == std::vector<uint16_t>({1, 2}),
        "node-b is asked for the rest, even though they share a chunk");
  check(a.at(0).find("d0@0:64#0") != std::string::npos,
        "a sink carries the slot's digest, offset, length and position");
  check(b.at(0).find("region=20") != std::string::npos,
        "each command uses its own node's region");
  session.finish_request();
}

void test_planned_readiness_counts_every_slot_of_a_layer() {
  std::cout << "planned readiness counts every slot of a layer\n";

  const SlotPlanner planner({single_group_layout(1)});
  NodeRegistry registry;
  register_node(registry, "node-a", 10);
  register_node(registry, "node-b", 20);
  PipelinedFetchSession session = make_session(planner, registry);

  const uint16_t gen =
      session.begin_request_from_slots(one_chunk_across_two_nodes());

  session.on_notifications({encode_immediate(gen, 3)});
  check(!session.is_layer_ready(1, gen),
        "layer 1 is not ready when one of its two slots has landed");
  check(!session.is_layer_ready(0, gen), "layer 0 has nothing yet");

  session.on_notifications({encode_immediate(gen, 2)});
  check(session.is_layer_ready(1, gen),
        "layer 1 is ready once its slots on both nodes landed, out of order");
  check(!session.is_layer_ready(0, gen),
        "layer 1 finishing first does not make layer 0 ready");

  session.on_notifications(
      {encode_immediate(gen, 1), encode_immediate(gen, 0)});
  check(session.is_layer_ready(0, gen), "layer 0 completes");
  session.finish_request();
}

void test_planned_decline_marks_only_its_layer() {
  std::cout << "planned decline marks only its layer\n";

  const SlotPlanner planner({single_group_layout(1)});
  NodeRegistry registry;
  register_node(registry, "node-a", 10);
  register_node(registry, "node-b", 20);
  PipelinedFetchSession session = make_session(planner, registry);

  const uint16_t gen =
      session.begin_request_from_slots(one_chunk_across_two_nodes());
  const auto commands = session.pipelined_fetch_commands();
  const std::string command_b = commands_for_node(commands, "node-b").at(0);

  session.on_node_reply("node-b", command_b, "n=2;accepted=1;failed=1;bytes=64",
                        gen);
  check(session.unservable_layers() == std::vector<uint32_t>({0}),
        "node-b declining slot 1 makes layer 0 unservable");

  bool threw = false;
  const std::string command_a = commands_for_node(commands, "node-a").at(0);
  try {
    session.on_node_reply("node-a", command_a,
                          "n=2;accepted=1;failed=1;bytes=64", gen);
  } catch (const std::runtime_error&) {
    threw = true;
  }
  check(threw, "a node cannot decline a slot another node owns");
  session.abandon_request();
}

void test_planned_request_splits_to_max_sinks() {
  std::cout << "planned request splits to max_sinks\n";

  const SlotPlanner planner({single_group_layout(1)});
  NodeRegistry registry;
  register_node(registry, "node-a", 10, 2);
  PipelinedFetchSession session = make_session(planner, registry);

  std::vector<PlannedSlot> slots;
  for (uint32_t i = 0; i < 5; ++i) {
    slots.push_back(planned("node-a", i, i * 64, "d" + std::to_string(i)));
  }
  session.begin_request_from_slots(slots);
  const auto commands =
      commands_for_node(session.pipelined_fetch_commands(), "node-a");
  check(commands.size() == 3, "5 sinks at max_sinks 2 become 3 commands");
  check(all_slots_in_commands(commands).size() == 5,
        "every slot is sent exactly once");
  session.finish_request();
}

void test_planned_late_write_from_abandoned_request_is_ignored() {
  std::cout << "planned late write from an abandoned request is ignored\n";

  const SlotPlanner planner({single_group_layout(1)});
  NodeRegistry registry;
  register_node(registry, "node-a", 10);
  PipelinedFetchSession session = make_session(planner, registry);

  const std::vector<PlannedSlot> slots = {planned("node-a", 0, 0, "d0")};
  const uint16_t old_gen = session.begin_request_from_slots(slots);
  session.abandon_request();
  const uint16_t new_gen = session.begin_request_from_slots(slots);

  session.on_notifications({encode_immediate(old_gen, 0)});
  check(!session.is_layer_ready(0, new_gen),
        "the old generation's write does not complete the new request");
  session.on_notifications({encode_immediate(new_gen, 0)});
  check(session.is_layer_ready(0, new_gen), "the new request's write does");
  session.finish_request();
}

template <typename Exception>
bool throws(const std::function<void()>& action) {
  try {
    action();
  } catch (const Exception&) {
    return true;
  } catch (...) {
    return false;
  }
  return false;
}

void test_planned_request_rejects_bad_input() {
  std::cout << "planned request rejects bad input\n";

  const SlotPlanner planner({single_group_layout(1)});
  NodeRegistry registry;
  register_node(registry, "node-a", 10);
  PipelinedFetchSession session = make_session(planner, registry);

  check(throws<std::invalid_argument>(
            [&] { session.begin_request_from_slots({}); }),
        "an empty plan is refused");
  check(throws<std::invalid_argument>([&] {
          session.begin_request_from_slots({planned("node-z", 0, 0, "d")});
        }),
        "a node with no kv-sink registration is refused");
  check(throws<std::invalid_argument>([&] {
          session.begin_request_from_slots({planned("node-a", 0, 0, "")});
        }),
        "an empty digest is refused");
  check(throws<std::invalid_argument>([&] {
          session.begin_request_from_slots(
              {planned("node-a", 0, kWindowBytes - 8, "d", 64)});
        }),
        "a slot outside the registered window is refused");
  check(!session.has_active_request(),
        "no rejected plan leaves a request active");

  session.begin_request_from_slots({planned("node-a", 0, 0, "d")});
  check(throws<std::runtime_error>([&] {
          session.begin_request_from_slots({planned("node-a", 0, 0, "d")});
        }),
        "a second active request is refused");
  session.finish_request();

  PipelinedFetchSession small(planner, registry, "kv", kRecordCap, kWriteCap,
                              kWindowBytes, 2);
  check(small.max_slots_per_request() == 2, "the device slot cap is reported");
  check(throws<PlanTooLargeError>([&] {
          small.begin_request_from_slots({planned("node-a", 0, 0, "d0"),
                                          planned("node-a", 0, 64, "d1"),
                                          planned("node-a", 0, 128, "d2")});
        }),
        "a plan above the device slot cap raises PlanTooLargeError");
}

}  // namespace

int main() {
  try {
    test_multi_node_request_shares_one_slot_index_space();
    test_one_node_two_chunks_orders_sinks_by_slot();
    test_failed_slot_makes_one_layer_recompute();
    test_stale_generation_notifications_are_ignored();
    test_abandon_makes_late_reply_harmless();
    test_allocated_generations_are_never_the_reserved_value();
    test_kv_size_two_requires_both_planes();
    test_begin_request_rejects_missing_digest();
    test_begin_request_rejects_second_active();
    test_chunking_splits_at_exact_multiple_of_limit();
    test_chunking_splits_with_remainder();
    test_chunking_union_covers_every_node_sink_once();
    test_chunking_preserves_ascending_slot_order_across_commands();
    test_two_nodes_chunk_with_independent_limits();
    test_rejected_second_command_marks_unservable();
    test_planned_slots_go_to_their_own_node();
    test_planned_readiness_counts_every_slot_of_a_layer();
    test_planned_decline_marks_only_its_layer();
    test_planned_request_splits_to_max_sinks();
    test_planned_late_write_from_abandoned_request_is_ignored();
    test_planned_request_rejects_bad_input();
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
