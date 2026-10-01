// SPDX-License-Identifier: Apache-2.0
//
// Bookkeeping tests for SinkFetchTable, the I/O-free half of the pipelined
// kv-sink fetch.
//
// Needs no RDMA device or cluster. What is defended here is what the Python
// layerwise source relies on:
//
//   - a plan becomes one batch per layer, in plan order, so the server can
//     place layer 0 first;
//   - a layer is ready only when every one of its slots landed, and a failed
//     slot makes its layer unservable for good;
//   - one fetch per window, and results for a fetch that is no longer active
//     -- including one whose generation was reused after it wrapped -- never
//     count toward the window's next fetch.
//
// Usage: sink_fetch_table_test    (takes no arguments)

#include <cstdint>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "sink_fetch_table.h"

namespace {

using lmcache::connector::sink::BegunFetch;
using lmcache::connector::sink::kNoGeneration;
using lmcache::connector::sink::PlanTooLargeError;
using lmcache::connector::sink::SinkFetchTable;
using lmcache::connector::sink::SinkSlot;

constexpr size_t kWindowBytes = 1 << 16;
constexpr size_t kSlotBytes = 1024;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (!condition) {
    std::cout << "  FAIL: " << what << "\n";
    ++failures;
  }
}

template <typename Error, typename Fn>
void check_throws(Fn fn, const std::string& what) {
  try {
    fn();
  } catch (const Error&) {
    return;
  } catch (const std::exception& e) {
    std::cout << "  FAIL: " << what << " threw the wrong type: " << e.what()
              << "\n";
    ++failures;
    return;
  }
  std::cout << "  FAIL: " << what << " did not throw\n";
  ++failures;
}

// `layers[i]` is the layer of slot i, packed from the start of `window`.
std::vector<SinkSlot> plan(const std::vector<uint32_t>& layers,
                           size_t window = 0) {
  std::vector<SinkSlot> slots;
  for (size_t i = 0; i < layers.size(); ++i) {
    slots.push_back({"key-" + std::to_string(i), layers[i],
                     window * kWindowBytes + i * kSlotBytes, kSlotBytes});
  }
  return slots;
}

void land_all(SinkFetchTable& table, const BegunFetch& begun) {
  for (const auto& batch : begun.batches) {
    for (const uint32_t slot : batch.slot_indices) {
      table.on_slot_result(begun.generation, slot, true, batch.token);
    }
  }
}

void test_one_batch_per_layer_in_plan_order() {
  std::cout << "one batch per layer in plan order\n";
  SinkFetchTable table(kWindowBytes, 1);
  const BegunFetch begun = table.begin(plan({2, 0, 2, 1, 0}));
  check(begun.generation != kNoGeneration, "generation is never 0");
  check(begun.batches.size() == 3, "three layers make three batches");
  check(begun.batches[0].layer_id == 2 && begun.batches[1].layer_id == 0 &&
            begun.batches[2].layer_id == 1,
        "layers come in order of first appearance");
  for (uint32_t i = 0; i < begun.batches.size(); ++i) {
    check(begun.batches[i].priority == i, "priority is the layer's ordinal");
    check(begun.batches[i].generation == begun.generation,
          "every batch quotes the fetch's generation");
  }
  check(begun.batches[0].slot_indices == std::vector<uint32_t>{0, 2},
        "layer 2 carries slots 0 and 2");
  check(begun.batches[1].slot_indices == std::vector<uint32_t>{1, 4},
        "layer 0 carries slots 1 and 4");
  check(begun.batches[2].slot_indices == std::vector<uint32_t>{3},
        "layer 1 carries slot 3");
  std::set<uint64_t> tokens;
  for (const auto& batch : begun.batches) {
    tokens.insert(batch.token);
    check(table.is_active(begun.generation, batch.token),
          "each batch's token belongs to the fetch");
  }
  check(tokens.size() == 3 && tokens.count(0) == 0,
        "tokens are non-zero and unique");
}

void test_layer_is_ready_only_when_every_slot_landed() {
  std::cout << "layer ready only when every slot landed\n";
  SinkFetchTable table(kWindowBytes, 1);
  const BegunFetch begun = table.begin(plan({0, 0, 1}));
  const uint16_t gen = begun.generation;
  check(!table.is_layer_ready(0, gen), "nothing landed yet");
  table.on_slot_result(gen, 0, true);
  check(!table.is_layer_ready(0, gen), "one of layer 0's two slots landed");
  table.on_slot_result(gen, 2, true);
  check(table.is_layer_ready(1, gen), "a later layer can finish first");
  check(!table.is_layer_ready(0, gen), "and layer 0 still waits");
  table.on_slot_result(gen, 0, true);
  check(!table.is_layer_ready(0, gen), "a repeated result counts once");
  table.on_slot_result(gen, 1, true);
  check(table.is_layer_ready(0, gen), "layer 0 is ready");
  check(!table.is_layer_ready(7, gen), "a layer not in the plan is not ready");
  check(!table.is_layer_ready(0, kNoGeneration), "generation 0 is never ready");
  check(table.unservable_layers(gen).empty(), "nothing failed");
}

void test_failed_slot_makes_layer_unservable() {
  std::cout << "failed slot makes layer unservable\n";
  SinkFetchTable table(kWindowBytes, 1);
  const uint16_t gen = table.begin(plan({0, 1, 1})).generation;
  table.on_slot_result(gen, 1, false);
  table.on_slot_result(gen, 1, true);
  table.on_slot_result(gen, 2, true);
  check(!table.is_layer_ready(1, gen), "a failed slot is not revived");
  check(table.unservable_layers(gen) == std::vector<uint32_t>{1},
        "layer 1 is unservable");
  table.on_slot_result(gen, 0, true);
  check(table.is_layer_ready(0, gen), "other layers are unaffected");
  table.on_slot_result(gen, 99, false);
  check(table.unservable_layers(gen) == std::vector<uint32_t>{1},
        "a slot outside the plan is ignored");
}

void test_one_fetch_per_window() {
  std::cout << "one fetch per window\n";
  SinkFetchTable table(kWindowBytes, 2);
  const BegunFetch first = table.begin(plan({0}, 0));
  check_throws<std::runtime_error>([&] { table.begin(plan({0}, 0)); },
                                   "a second fetch in a busy window");
  const BegunFetch second = table.begin(plan({0, 1}, 1));
  check(first.generation != second.generation,
        "live fetches never share a generation");
  check(table.has_active_request(), "two fetches are active");
  land_all(table, second);
  check(table.is_layer_ready(1, second.generation) &&
            !table.is_layer_ready(0, first.generation),
        "windows keep separate accounts");
  table.finish(first.generation);
  table.finish(second.generation);
  check(!table.has_active_request(), "finishing frees both windows");
  table.begin(plan({0}, 0));
}

void test_malformed_plans_are_refused() {
  std::cout << "malformed plans are refused\n";
  SinkFetchTable table(kWindowBytes, 2, 4);
  check_throws<std::invalid_argument>([&] { table.begin({}); }, "empty plan");
  check_throws<std::invalid_argument>(
      [&] { table.begin({{"", 0, 0, kSlotBytes}}); }, "empty key");
  check_throws<std::invalid_argument>([&] { table.begin({{"k", 0, 0, 0}}); },
                                      "zero length");
  check_throws<std::invalid_argument>(
      [&] { table.begin({{"k", 0, kWindowBytes - 8, 16}}); },
      "a slot crossing the window's end");
  check_throws<std::invalid_argument>(
      [&] {
        table.begin({{"a", 0, 0, 8}, {"b", 0, kWindowBytes, 8}});
      },
      "a slot in a different window from the first");
  check_throws<std::invalid_argument>(
      [&] { table.begin({{"k", 0, 2 * kWindowBytes, 8}}); },
      "a slot past the last window");
  check_throws<PlanTooLargeError>(
      [&] { table.begin(plan({0, 0, 0, 0, 0})); },
      "more slots than one fetch carries");
  check(!table.has_active_request(), "a refused plan leaves nothing active");
  table.begin({{"k", 0, kWindowBytes - 16, 16}});
}

void test_constructor_rejects_empty_ranges() {
  std::cout << "constructor rejects empty ranges\n";
  check_throws<std::invalid_argument>([] { SinkFetchTable(0, 1); },
                                      "zero window bytes");
  check_throws<std::invalid_argument>([] { SinkFetchTable(kWindowBytes, 0); },
                                      "zero windows");
  check_throws<std::invalid_argument>(
      [] { SinkFetchTable(kWindowBytes, 1, 0); }, "zero slots per fetch");
}

void test_finish_and_abandon() {
  std::cout << "finish and abandon\n";
  SinkFetchTable table(kWindowBytes, 1);
  const uint16_t gen = table.begin(plan({0})).generation;
  table.abandon(gen);
  check(!table.is_active(gen), "abandon drops the fetch");
  table.abandon(gen);
  check_throws<std::runtime_error>([&] { table.finish(gen); },
                                   "finishing an inactive fetch");
  table.on_slot_result(gen, 0, true);
  check(!table.is_layer_ready(0, gen), "a late result is dropped");
  check(table.unservable_layers(gen).empty(),
        "an inactive fetch has no unservable layers");
}

// A batch still in flight after abandon must not be credited to the window's
// next fetch, even once the 16-bit generation has wrapped back to its value.
void test_stale_batch_after_generation_wraps() {
  std::cout << "stale batch after generation wraps\n";
  SinkFetchTable table(kWindowBytes, 1);
  const BegunFetch old_fetch = table.begin(plan({0}));
  const uint64_t old_token = old_fetch.batches[0].token;
  table.abandon(old_fetch.generation);

  BegunFetch reused;
  for (int i = 0; i < 0x10000; ++i) {
    reused = table.begin(plan({0}));
    if (reused.generation == old_fetch.generation) {
      break;
    }
    table.finish(reused.generation);
  }
  check(reused.generation == old_fetch.generation,
        "the generation wraps back to the abandoned one");
  check(!table.is_active(reused.generation, old_token),
        "the old batch is not part of the new fetch");
  table.on_slot_result(reused.generation, 0, true, old_token);
  check(!table.is_layer_ready(0, reused.generation),
        "the old batch's result is dropped");
  table.on_slot_result(reused.generation, 0, true,
                       reused.batches[0].token);
  check(table.is_layer_ready(0, reused.generation),
        "the new batch's result counts");
}

}  // namespace

int main() {
  test_one_batch_per_layer_in_plan_order();
  test_layer_is_ready_only_when_every_slot_landed();
  test_failed_slot_makes_layer_unservable();
  test_one_fetch_per_window();
  test_malformed_plans_are_refused();
  test_constructor_rejects_empty_ranges();
  test_finish_and_abandon();
  test_stale_batch_after_generation_wraps();
  if (failures != 0) {
    std::cout << failures << " check(s) failed\n";
    return 1;
  }
  std::cout << "PASS\n";
  return 0;
}
