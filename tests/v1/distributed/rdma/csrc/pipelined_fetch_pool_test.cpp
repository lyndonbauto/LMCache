// SPDX-License-Identifier: Apache-2.0
//
// Device-free checks for PipelinedFetchPool: one pipelined fetch per window
// at the same time, routed by generation.

#include <cstdint>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "kv_sink_client.h"
#include "layer_pipeline.h"
#include "pipelined_fetch_issue.h"
#include "pipelined_fetch_pool.h"
#include "pipelined_fetch_session.h"
#include "slot_planner.h"

namespace {

using lmcache::connector::rdma::encode_immediate;
using lmcache::connector::rdma::issue_planned_fetch;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::kMaxFetchWindows;
using lmcache::connector::rdma::kNoGeneration;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::pipelined_command_slot_indices;
using lmcache::connector::rdma::PipelinedFetchPool;
using lmcache::connector::rdma::PipelinedFetchSession;
using lmcache::connector::rdma::PlannedSlot;
using lmcache::connector::rdma::PlanTooLargeError;
using lmcache::connector::rdma::SlotPlanner;

constexpr size_t kRecordCap = 1u << 20;
constexpr size_t kWindowBytes = 1u << 16;
constexpr uint32_t kWindows = 4;
constexpr uint32_t kDepth = 64;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

template <typename Exception, typename Fn>
bool throws(Fn&& fn) {
  try {
    fn();
  } catch (const Exception&) {
    return true;
  } catch (...) {
    return false;
  }
  return false;
}

ObjectGroupLayout two_layer_layout() {
  KernelGroupLayout group;
  group.layer_indices = {0, 1};
  group.kv_size = 1;
  group.num_slots = 8;
  group.hidden_dim = 64;
  group.element_size = 2;
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(group);
  return layout;
}

NodeRegistry two_nodes() {
  NodeRegistry registry;
  uint64_t region = 1;
  for (const char* name : {"node-a", "node-b"}) {
    NodeRegistration reg;
    reg.node_name = name;
    reg.region = region++;
    reg.max_sinks_per_command = 256;
    reg.valid = true;
    registry.set(reg);
  }
  return registry;
}

// Two layers, two slots each, alternating nodes, inside window `window`.
std::vector<PlannedSlot> request_in_window(uint32_t window) {
  const size_t base = window * kWindowBytes;
  std::vector<PlannedSlot> slots;
  for (uint32_t i = 0; i < 4; ++i) {
    PlannedSlot slot;
    slot.node_name = i % 2 == 0 ? "node-a" : "node-b";
    slot.digest_hex = "d" + std::to_string(window) + std::to_string(i);
    slot.layer_id = i / 2;
    slot.offset = base + i * 64;
    slot.length = 64;
    slots.push_back(slot);
  }
  return slots;
}

std::string accept_all(const std::string&, const std::string& command) {
  const size_t n = pipelined_command_slot_indices(command).size();
  return "n=" + std::to_string(n) + ";accepted=" + std::to_string(n) +
         ";bytes=0";
}

struct Fixture {
  SlotPlanner planner{{two_layer_layout()}};
  NodeRegistry registry = two_nodes();
  PipelinedFetchPool pool{planner,    registry,     "kv",     kRecordCap,
                          kRecordCap, kWindowBytes, kWindows, kDepth};
};

void test_construction_limits() {
  std::cout << "the pool refuses unusable window counts and depths\n";
  SlotPlanner planner({two_layer_layout()});
  NodeRegistry registry = two_nodes();
  auto build = [&](uint32_t windows, uint32_t depth) {
    PipelinedFetchPool pool(planner, registry, "kv", kRecordCap, kRecordCap,
                            kWindowBytes, windows, depth);
    return pool.max_slots_per_request();
  };
  check(throws<std::invalid_argument>([&] { build(0, kDepth); }),
        "zero windows are refused");
  check(throws<std::invalid_argument>(
            [&] { build(kMaxFetchWindows + 1, 1u << 20); }),
        "more than kMaxFetchWindows windows are refused");
  check(throws<std::invalid_argument>([&] { build(kWindows, kWindows - 1); }),
        "a depth below one slot per window is refused");
  check(build(kWindows, kDepth) == kDepth / kWindows,
        "each window gets depth / window_count slots");
  check(build(3, 10) == 3, "the share rounds down");
}

void test_windows_run_concurrently() {
  std::cout << "requests in different windows are active at the same time\n";
  Fixture f;
  const uint16_t a =
      issue_planned_fetch(f.pool, accept_all, request_in_window(0));
  const uint16_t b =
      issue_planned_fetch(f.pool, accept_all, request_in_window(2));
  check(a != b, "the two requests have different generations");
  check(f.pool.window_of_generation(a) == 0,
        "the first request's generation "
        "names window 0");
  check(f.pool.window_of_generation(b) == 2,
        "the second request's generation "
        "names window 2");
  check(f.pool.is_active(a) && f.pool.is_active(b), "both are active");
  check(throws<std::runtime_error>(
            [&] { f.pool.begin_request_from_slots(request_in_window(0)); }),
        "a second request in a busy window is refused");
  check(f.pool.is_active(a), "the refusal leaves the busy window's fetch");
  f.pool.finish_request(a);
  const uint16_t c =
      issue_planned_fetch(f.pool, accept_all, request_in_window(0));
  check(f.pool.window_of_generation(c) == 0 && c != a,
        "a finished window takes a new request with a new generation");
  f.pool.abandon_request(b);
  f.pool.abandon_request(c);
  check(!f.pool.has_active_request(), "abandoning both leaves the pool idle");
}

void test_immediates_reach_their_own_request() {
  std::cout << "immediates on the shared queue reach their own request\n";
  Fixture f;
  const uint16_t a =
      issue_planned_fetch(f.pool, accept_all, request_in_window(1));
  const uint16_t b =
      issue_planned_fetch(f.pool, accept_all, request_in_window(3));
  // One poll's worth, interleaved: all of a's layer 0, then b's slot 0.
  f.pool.on_notifications(
      {encode_immediate(a, 0), encode_immediate(b, 0), encode_immediate(a, 1)});
  check(f.pool.is_layer_ready(0, a), "a's layer 0 is ready");
  check(!f.pool.is_layer_ready(0, b), "b's layer 0 is not: one slot landed");
  check(!f.pool.is_layer_ready(1, a), "a's layer 1 is not");
  f.pool.on_notifications(
      {encode_immediate(b, 1), encode_immediate(b, 2), encode_immediate(b, 3)});
  check(f.pool.is_layer_ready(0, b) && f.pool.is_layer_ready(1, b),
        "b completes without touching a");
  check(!f.pool.is_layer_ready(1, a), "a's layer 1 is still not ready");
  check(!f.pool.is_layer_ready(0, kNoGeneration),
        "generation 0 is never ready");
  f.pool.on_notifications({encode_immediate(kNoGeneration, 0)});
  check(true, "an immediate with generation 0 is dropped");
  f.pool.finish_request(b);
  f.pool.abandon_request(a);
}

void test_late_write_after_abandon_is_ignored() {
  std::cout << "a late write of an abandoned request touches nothing\n";
  Fixture f;
  const uint16_t old =
      issue_planned_fetch(f.pool, accept_all, request_in_window(2));
  const uint16_t other =
      issue_planned_fetch(f.pool, accept_all, request_in_window(0));
  f.pool.abandon_request(old);
  const uint16_t fresh =
      issue_planned_fetch(f.pool, accept_all, request_in_window(2));
  check(fresh != old, "the window's next request has a new generation");
  f.pool.on_notifications({encode_immediate(old, 0), encode_immediate(old, 1)});
  check(!f.pool.is_layer_ready(0, fresh),
        "the old generation's writes do not count for the new request");
  check(!f.pool.is_layer_ready(0, other), "nor for the other window's request");
  check(f.pool.unservable_layers(old).empty(),
        "an abandoned generation reports nothing");
  f.pool.abandon_request(old);
  check(f.pool.is_active(fresh), "abandoning it again is a no-op");
  f.pool.abandon_request(fresh);
  f.pool.abandon_request(other);
}

void test_finish_needs_the_active_generation() {
  std::cout << "finish names the active request exactly\n";
  Fixture f;
  const uint16_t a =
      issue_planned_fetch(f.pool, accept_all, request_in_window(1));
  check(throws<std::runtime_error>([&] {
          f.pool.finish_request(static_cast<uint16_t>(a + kWindows));
        }),
        "finishing a later generation of the same window throws");
  check(
      throws<std::runtime_error>([&] { f.pool.finish_request(kNoGeneration); }),
      "finishing generation 0 throws");
  check(f.pool.is_active(a), "failed finishes leave the request active");
  f.pool.finish_request(a);
  check(throws<std::runtime_error>([&] { f.pool.finish_request(a); }),
        "finishing twice throws");
}

void test_share_limits_one_request() {
  std::cout << "one request may use only its window's share\n";
  SlotPlanner planner({two_layer_layout()});
  NodeRegistry registry = two_nodes();
  // 4 windows, depth 12: 3 slots each, and the request carries 4.
  PipelinedFetchPool pool(planner, registry, "kv", kRecordCap, kRecordCap,
                          kWindowBytes, kWindows, 12);
  check(throws<PlanTooLargeError>(
            [&] { pool.begin_request_from_slots(request_in_window(0)); }),
        "a request over the share is refused as too large");
  check(!pool.has_active_request(), "and nothing was begun");
}

void test_first_slot_picks_the_window() {
  std::cout << "the first slot picks the window, and all slots stay in it\n";
  Fixture f;
  check(throws<std::invalid_argument>([&] {
          f.pool.begin_request_from_slots(request_in_window(kWindows));
        }),
        "a request past the last window is refused");
  std::vector<PlannedSlot> spanning = request_in_window(1);
  spanning.back().offset = 2 * kWindowBytes;
  check(throws<std::invalid_argument>(
            [&] { f.pool.begin_request_from_slots(spanning); }),
        "a request spanning two windows is refused");
  check(throws<std::invalid_argument>(
            [&] { f.pool.begin_request_from_slots({}); }),
        "an empty request is refused");
  check(!f.pool.has_active_request(), "none of them began");
}

void test_failed_issue_abandons_only_its_request() {
  std::cout << "a failed issue abandons only its own request\n";
  Fixture f;
  const uint16_t kept =
      issue_planned_fetch(f.pool, accept_all, request_in_window(0));
  auto garbled = [](const std::string&, const std::string&) {
    return std::string("n=1;accepted=7;bytes=0");
  };
  check(throws<std::exception>([&] {
          issue_planned_fetch(f.pool, garbled, request_in_window(1));
        }),
        "a malformed reply fails the issue");
  check(f.pool.is_active(kept), "the other window's request is untouched");
  const uint16_t retry =
      issue_planned_fetch(f.pool, accept_all, request_in_window(1));
  check(f.pool.window_of_generation(retry) == 1,
        "the failed window is free again");
  f.pool.abandon_request(kept);
  f.pool.abandon_request(retry);
}

void test_generations_stay_in_their_window_across_wrap() {
  std::cout << "a window's generations stay in its class across wrap\n";
  Fixture f;
  std::set<uint16_t> seen;
  bool in_class = true;
  bool never_zero = true;
  // More than 65535 / kWindows requests, so window 3's counter wraps.
  for (int i = 0; i < 70000 / static_cast<int>(kWindows); ++i) {
    const uint16_t g = f.pool.begin_request_from_slots(request_in_window(3));
    in_class = in_class && f.pool.window_of_generation(g) == 3;
    never_zero = never_zero && g != kNoGeneration;
    seen.insert(g);
    f.pool.finish_request(g);
  }
  check(in_class, "every generation names window 3");
  check(never_zero, "generation 0 is never allocated");
  // Window 3 owns 4, 8, ..., 65532.
  check(seen.size() == 65535 / kWindows,
        "the counter cycles through exactly its class");
}

void test_counters_carry_to_a_replacement_pool() {
  std::cout << "a replacement pool continues each window's generations\n";
  Fixture f;
  const uint16_t a = f.pool.begin_request_from_slots(request_in_window(2));
  f.pool.finish_request(a);
  const std::vector<uint16_t> counters = f.pool.generation_counters();
  PipelinedFetchPool next(f.planner, f.registry, "kv", kRecordCap, kRecordCap,
                          kWindowBytes, kWindows, kDepth);
  next.restore_generation_counters(counters);
  const uint16_t b = next.begin_request_from_slots(request_in_window(2));
  check(b != a && next.window_of_generation(b) == 2,
        "the replacement does not reuse the last generation");
  check(throws<std::invalid_argument>(
            [&] { next.restore_generation_counters({1, 2}); }),
        "counters for a different window count are refused");
  next.abandon_request(b);
}

void test_session_generation_class() {
  std::cout << "a session's generation class is validated\n";
  SlotPlanner planner({two_layer_layout()});
  NodeRegistry registry = two_nodes();
  PipelinedFetchSession session(planner, registry, "kv", kRecordCap, kRecordCap,
                                kWindowBytes, kDepth);
  check(throws<std::invalid_argument>(
            [&] { session.set_generation_class(0, 4); }),
        "first 0 is refused");
  check(throws<std::invalid_argument>(
            [&] { session.set_generation_class(5, 4); }),
        "first above stride is refused");
  session.set_generation_class(2, 4);
  check(session.next_generation() == 2, "the class starts at first");
  session.begin_request_from_slots(request_in_window(0));
  check(throws<std::runtime_error>([&] { session.set_generation_class(1, 4); }),
        "the class cannot change during a fetch");
  session.finish_request();
  check(session.next_generation() == 6,
        "the next generation is first + stride");
}

}  // namespace

int main() {
  test_construction_limits();
  test_windows_run_concurrently();
  test_immediates_reach_their_own_request();
  test_late_write_after_abandon_is_ignored();
  test_finish_needs_the_active_generation();
  test_share_limits_one_request();
  test_first_slot_picks_the_window();
  test_failed_issue_abandons_only_its_request();
  test_generations_stay_in_their_window_across_wrap();
  test_counters_carry_to_a_replacement_pool();
  test_session_generation_class();
  if (failures != 0) {
    std::cout << failures << " FAILED\n";
    return 1;
  }
  std::cout << "all pipelined fetch pool checks passed\n";
  return 0;
}
