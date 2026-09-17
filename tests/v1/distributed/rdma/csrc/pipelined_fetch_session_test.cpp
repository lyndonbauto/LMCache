// SPDX-License-Identifier: Apache-2.0
//
// Driver tests for the pipelined fetch session.
//
// Needs no RDMA device and no Aerospike cluster: the session consumes reply
// strings and notification integers, so every multi-node and abandon case
// lives here rather than in the fabric harness.
//
// **One slot index space across nodes.** If each node's command numbered slots
// from zero, notifications would collide and a layer would never complete or
// would complete on partial data. The session must partition by node while
// keeping the request's slot indices in every command.
//
// **A declined slot is not a slow slot.** The reply names failed slots; the
// session feeds them to note_unservable so one missing record becomes a
// recompute of one layer while the rest of the request still pipelines.
//
// **Abandon is a hard stop.** A reply that arrives after abandon must not
// touch readiness for a later request, and stale generations must be ignored
// once a new request has started.
//
// Usage: pipelined_fetch_session_test    (takes no arguments)

#include <cstdint>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "kv_sink_client.h"
#include "layer_pipeline.h"
#include "pipelined_fetch_session.h"
#include "slot_planner.h"

namespace {

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

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

KernelGroupLayout attention_group(uint32_t layers) {
  KernelGroupLayout group;
  for (uint32_t i = 0; i < layers; ++i) {
    group.layer_indices.push_back(i);
  }
  group.kv_size = 1;
  group.num_slots = 8;
  group.hidden_dim = 64;
  group.element_size = 2;
  return group;
}

ObjectGroupLayout single_group_layout(uint32_t layers) {
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(attention_group(layers));
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

std::vector<SlotDigest> digests_for_plan(
    const lmcache::connector::rdma::RequestPlan& plan,
    const std::string& prefix) {
  std::vector<SlotDigest> out;
  for (uint16_t i = 0; i < static_cast<uint16_t>(plan.slot_count()); ++i) {
    SlotDigest entry;
    entry.slot_index = i;
    entry.digest_hex = prefix + std::to_string(i);
    out.push_back(entry);
  }
  return out;
}

void test_multi_node_request_shares_one_slot_index_space() {
  std::cout << "multi-node request shares one slot index space\n";

  const ObjectGroupLayout layout = single_group_layout(2);
  const SlotPlanner planner({layout});
  const size_t object_bytes = object_group_bytes(layout);

  NodeRegistry registry;
  register_node(registry, "node-a", 10);
  register_node(registry, "node-b", 20);

  PipelinedFetchSession session(planner, registry, "kv", kRecordCap, kWriteCap);

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
      placements, nodes, digests_for_plan(expected_plan, "aa"));

  const std::map<std::string, std::string> commands =
      session.pipelined_fetch_commands();
  check(commands.size() == 2, "two nodes each receive one command");

  const std::string& cmd_a = commands.at("node-a");
  const std::string& cmd_b = commands.at("node-b");
  check(cmd_a.find("#0") != std::string::npos &&
            cmd_a.find("#2") != std::string::npos,
        "node-a carries slots 0 and 2 from the shared numbering");
  check(cmd_b.find("#1") != std::string::npos &&
            cmd_b.find("#3") != std::string::npos,
        "node-b carries slots 1 and 3");
  check(cmd_a.find("#1") == std::string::npos &&
            cmd_b.find("#0") == std::string::npos,
        "each command omits the other node's slots");

  check(session.active_generation() == gen2,
        "generation is returned to caller");
  session.finish_request();
}

void test_failed_slot_makes_one_layer_recompute() {
  std::cout << "failed slot makes one layer recompute\n";

  const ObjectGroupLayout layout = single_group_layout(2);
  const SlotPlanner planner({layout});

  NodeRegistry registry;
  register_node(registry, "node-a", 1);

  PipelinedFetchSession session(planner, registry, "kv", kRecordCap, kWriteCap);

  const std::vector<ChunkPlacement> placements = {
      ChunkPlacement{0, 0, 0},
  };
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan =
      planner.plan_request(placements, kRecordCap, kWriteCap, 0x11);
  session.begin_request(placements, nodes, digests_for_plan(plan, "dd"));

  session.on_node_reply("node-a", "n=2;accepted=1;failed=0;bytes=1");

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
  PipelinedFetchSession session(planner, registry, "kv", kRecordCap, kWriteCap);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto first_plan =
      planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  const uint16_t first_gen = session.begin_request(
      placements, nodes, digests_for_plan(first_plan, "a"));

  session.finish_request();

  const uint16_t second_gen = session.begin_request(
      placements, nodes,
      digests_for_plan(
          planner.plan_request(placements, kRecordCap, kWriteCap, 0), "b"));

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
  PipelinedFetchSession session(planner, registry, "kv", kRecordCap, kWriteCap);

  const std::vector<ChunkPlacement> placements = {ChunkPlacement{0, 0, 0}};
  const std::vector<ChunkNodeBinding> nodes = {{0, "node-a"}};
  const auto plan = planner.plan_request(placements, kRecordCap, kWriteCap, 0);
  session.begin_request(placements, nodes, digests_for_plan(plan, "c"));
  session.abandon_request();

  session.on_node_reply("node-a", "n=1;accepted=0;failed=0;bytes=0");
  check(session.unservable_layers().empty(),
        "a late reply after abandon does not mark layers unservable");

  const uint16_t gen = session.begin_request(
      placements, nodes,
      digests_for_plan(
          planner.plan_request(placements, kRecordCap, kWriteCap, 0), "d"));
  check(session.has_active_request(), "a new request can start after abandon");
  check(session.active_generation() == gen, "new generation is allocated");
  session.finish_request();
}

}  // namespace

int main() {
  try {
    test_multi_node_request_shares_one_slot_index_space();
    test_failed_slot_makes_one_layer_recompute();
    test_stale_generation_notifications_are_ignored();
    test_abandon_makes_late_reply_harmless();
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
