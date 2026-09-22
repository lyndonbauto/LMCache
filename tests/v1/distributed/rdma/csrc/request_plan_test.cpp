// SPDX-License-Identifier: Apache-2.0
//
// Request-scoped readiness tests for the RDMA layer pipeline.
//
// Needs no RDMA device: this is the bookkeeping, not the data path. The
// fabric is exercised by rdma_pipeline_test.cpp, which pushes real
// RDMA_WRITE_WITH_IMM traffic but only for a single chunk.
//
// What is being defended here is the difference between per-fetch and
// per-request scope, which is the most dangerous mistake available in this
// design because the failure is silent.
//
// vLLM computes layer L for the whole sequence, so it needs layer L of every
// participating chunk. Chunks are separate keys, spread across cluster nodes
// by digest, so one layer arrives from several nodes via several fetch
// commands -- and every notification lands on one queue pair, so one
// readiness table sees them all. Two consequences are tested:
//
//   1. **A layer is not ready until every chunk's pieces have landed.** A
//      tracker that completed a layer when one node finished would hand the
//      model a tensor whose other chunks are still zeros. No error, no
//      crash, just wrong attention output.
//
//   2. **Slot indices must be unique across the request, not per fetch.** If
//      each node numbered its slots from zero, the second node's slot 5 would
//      look like a duplicate of the first's. The layer would never reach its
//      expected count and the fetch would hang. This is tested by asserting
//      that no two chunks share a slot index, and that arrivals interleaved
//      across chunks are all counted.
//
// Also covered: sliding-window groups, where a layer covers only a window of
// trailing chunks. Requiring every chunk of the request for such a layer
// would wait forever, so the plan must count only what it was given.
//
// Usage: request_plan_test    (takes no arguments; ignores any passed, so it
// can share the harness runner with the device-dependent tests)

#include <cstdint>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "layer_pipeline.h"

namespace {

using lmcache::connector::rdma::ArrivalStatus;
using lmcache::connector::rdma::encode_immediate;
using lmcache::connector::rdma::kMaxSlotsPerRequest;
using lmcache::connector::rdma::LayerReadiness;
using lmcache::connector::rdma::RequestPlan;

constexpr uint16_t kGeneration = 0x1234;
constexpr size_t kPieceBytes = 4096;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

// Build a plan covering `layers` layers of `chunks` chunks, with
// `pieces_per_layer` writes per layer per chunk -- the kv_size x
// pieces_per_plane product. Offsets are unique and dense.
RequestPlan build_plan(uint32_t chunks, uint32_t layers,
                       uint32_t pieces_per_layer) {
  RequestPlan plan(kGeneration);
  size_t offset = 0;
  // Layer-major, which is the order the servers are asked to push in.
  for (uint32_t layer = 0; layer < layers; ++layer) {
    for (uint32_t chunk = 0; chunk < chunks; ++chunk) {
      for (uint32_t piece = 0; piece < pieces_per_layer; ++piece) {
        plan.add_slot(layer, chunk, offset, kPieceBytes);
        offset += kPieceBytes;
      }
    }
  }
  return plan;
}

void test_a_layer_needs_every_chunk() {
  std::cout << "\na layer is only ready once every chunk has landed\n";

  // Three chunks, as if held by three different nodes.
  const uint32_t kChunks = 3;
  RequestPlan plan = build_plan(kChunks, /*layers=*/4, /*pieces_per_layer=*/2);
  LayerReadiness readiness(plan);

  check(plan.expected_slots(0) == kChunks * 2,
        "layer 0 expects the pieces of all three chunks, not just one");

  // Land every slot of layer 0 that belongs to chunk 0 only -- one node
  // finishing its share.
  for (uint16_t slot : plan.slots_for_chunk(0)) {
    if (plan.slot(slot).layer_id == 0) {
      readiness.note_arrival(plan.immediate_for(slot));
    }
  }
  check(!readiness.is_layer_ready(0),
        "one node finishing its chunk does not make the layer ready");

  for (uint16_t slot : plan.slots_for_chunk(1)) {
    if (plan.slot(slot).layer_id == 0) {
      readiness.note_arrival(plan.immediate_for(slot));
    }
  }
  check(!readiness.is_layer_ready(0), "two of three nodes is still not ready");

  ArrivalStatus last = ArrivalStatus::kAccepted;
  for (uint16_t slot : plan.slots_for_chunk(2)) {
    if (plan.slot(slot).layer_id == 0) {
      last = readiness.note_arrival(plan.immediate_for(slot));
    }
  }
  check(readiness.is_layer_ready(0),
        "the layer is ready once the last chunk lands");
  check(last == ArrivalStatus::kLayerComplete,
        "the final piece reports completion");
  check(!readiness.is_layer_ready(1),
        "completing layer 0 says nothing about layer 1");
  check(!readiness.all_ready(), "the request as a whole is not done");
}

void test_slot_indices_are_unique_across_chunks() {
  std::cout << "\nslot indices are unique across the whole request\n";

  RequestPlan plan = build_plan(/*chunks=*/4, /*layers=*/3,
                                /*pieces_per_layer=*/2);

  // The bug this guards against: per-fetch numbering, where each node starts
  // at zero and two nodes' slot 0 collide.
  std::set<uint16_t> seen;
  size_t total = 0;
  for (uint32_t chunk : plan.chunk_ids()) {
    for (uint16_t slot : plan.slots_for_chunk(chunk)) {
      seen.insert(slot);
      ++total;
    }
  }
  check(total == plan.slot_count(),
        "every slot in the plan belongs to exactly one chunk");
  check(seen.size() == plan.slot_count(), "no two chunks share a slot index");

  // Each chunk's slots really do carry that chunk's id, so a caller
  // partitioning the plan by node cannot mis-route a write.
  bool correctly_attributed = true;
  for (uint32_t chunk : plan.chunk_ids()) {
    for (uint16_t slot : plan.slots_for_chunk(chunk)) {
      correctly_attributed =
          correctly_attributed && plan.slot(slot).chunk_id == chunk;
    }
  }
  check(correctly_attributed,
        "slots_for_chunk returns only that chunk's slots");

  // Offsets are distinct, so no two slots write over each other.
  std::set<size_t> offsets;
  for (uint16_t i = 0; i < plan.slot_count(); ++i) {
    offsets.insert(plan.slot(i).offset);
  }
  check(offsets.size() == plan.slot_count(),
        "no two slots target the same destination offset");
}

void test_arrivals_interleaved_across_nodes() {
  std::cout << "\narrivals interleaved across nodes and layers\n";

  // Independent nodes, so the merged stream at the queue pair is interleaved
  // even if each node pushes its own layers in order.
  RequestPlan plan = build_plan(/*chunks=*/2, /*layers=*/3,
                                /*pieces_per_layer=*/1);
  LayerReadiness readiness(plan);

  // Deliver in an order no watermark would tolerate: the last layer first,
  // and each layer's two chunks split apart.
  const std::vector<std::pair<uint32_t, uint32_t>> order = {
      {2, 0}, {0, 1}, {2, 1}, {1, 0}, {0, 0}, {1, 1}};
  for (const auto& [layer, chunk] : order) {
    for (uint16_t slot : plan.slots_for_chunk(chunk)) {
      if (plan.slot(slot).layer_id == layer) {
        readiness.note_arrival(plan.immediate_for(slot));
      }
    }
  }
  check(readiness.all_ready(), "every slot is counted exactly once");
  check(readiness.ready_layers().size() == 3, "all three layers are ready");

  // Layer 2 completes before layer 0 and 1: readiness is a set, not a
  // watermark. Replay the prefix to show the intermediate state.
  LayerReadiness partial(plan);
  for (uint16_t slot : plan.slots_for_chunk(0)) {
    if (plan.slot(slot).layer_id == 2) {
      partial.note_arrival(plan.immediate_for(slot));
    }
  }
  for (uint16_t slot : plan.slots_for_chunk(1)) {
    if (plan.slot(slot).layer_id == 2) {
      partial.note_arrival(plan.immediate_for(slot));
    }
  }
  check(partial.is_layer_ready(2) && !partial.is_layer_ready(0),
        "a later layer can be ready while an earlier one is not");
}

void test_a_sliding_window_layer_does_not_wait_for_every_chunk() {
  std::cout << "\nsliding-window groups cover only a window of chunks\n";

  // Layer 0 is full attention over four chunks; layer 1 belongs to a
  // sliding-window group covering only the two trailing chunks. Requiring all
  // four for layer 1 would wait forever.
  RequestPlan plan(kGeneration);
  size_t offset = 0;
  for (uint32_t chunk = 0; chunk < 4; ++chunk) {
    plan.add_slot(/*layer_id=*/0, chunk, offset, kPieceBytes);
    offset += kPieceBytes;
  }
  for (uint32_t chunk = 2; chunk < 4; ++chunk) {
    plan.add_slot(/*layer_id=*/1, chunk, offset, kPieceBytes);
    offset += kPieceBytes;
  }

  check(plan.expected_slots(0) == 4, "the full-attention layer spans 4 chunks");
  check(plan.expected_slots(1) == 2,
        "the sliding-window layer expects only its window");

  LayerReadiness readiness(plan);
  for (uint32_t chunk : {2u, 3u}) {
    for (uint16_t slot : plan.slots_for_chunk(chunk)) {
      if (plan.slot(slot).layer_id == 1) {
        readiness.note_arrival(plan.immediate_for(slot));
      }
    }
  }
  check(readiness.is_layer_ready(1),
        "the windowed layer is ready without chunks outside its window");
  check(!readiness.is_layer_ready(0),
        "the full-attention layer still waits for all four chunks");

  // Chunks outside the window carry no slots for that layer at all.
  bool window_respected = true;
  for (uint32_t chunk : {0u, 1u}) {
    for (uint16_t slot : plan.slots_for_chunk(chunk)) {
      window_respected = window_respected && plan.slot(slot).layer_id != 1;
    }
  }
  check(window_respected,
        "chunks outside the window hold no slots for the windowed layer");
}

void test_unknown_and_superseded_arrivals() {
  std::cout << "\narrivals that do not belong to this request\n";

  RequestPlan plan = build_plan(/*chunks=*/2, /*layers=*/2,
                                /*pieces_per_layer=*/1);
  LayerReadiness readiness(plan);

  check(readiness.note_arrival(encode_immediate(kGeneration + 1, 0)) ==
            ArrivalStatus::kStaleGeneration,
        "a write from a superseded request is rejected by generation");
  check(readiness.slots_landed() == 0,
        "a stale write does not count as progress");

  const uint16_t past_end = static_cast<uint16_t>(plan.slot_count());
  check(readiness.note_arrival(encode_immediate(kGeneration, past_end)) ==
            ArrivalStatus::kUnknownSlot,
        "an immediate naming no slot in the plan is rejected");

  check(
      readiness.note_arrival(plan.immediate_for(0)) == ArrivalStatus::kAccepted,
      "a first arrival for a multi-chunk layer is accepted but incomplete");
  check(readiness.note_arrival(plan.immediate_for(0)) ==
            ArrivalStatus::kDuplicate,
        "a repeated immediate is a duplicate, not double counted");
  check(readiness.slots_landed() == 1, "duplicates do not inflate the count");

  check(!readiness.is_layer_ready(99),
        "a layer absent from the plan is never reported ready");
}

void test_the_slot_budget_is_enforced() {
  std::cout << "\nthe per-request slot budget\n";

  // The budget is per request, so it is shared by every chunk. The immediate
  // has 16 bits for the index and add_slot must refuse to wrap.
  RequestPlan plan(kGeneration);
  for (uint32_t i = 0; i < kMaxSlotsPerRequest; ++i) {
    plan.add_slot(/*layer_id=*/0, /*chunk_id=*/i, i * kPieceBytes, kPieceBytes);
  }
  check(plan.slot_count() == kMaxSlotsPerRequest,
        "a request can hold the full 16-bit slot space");

  bool threw = false;
  try {
    plan.add_slot(0, 0, 0, kPieceBytes);
  } catch (const std::length_error&) {
    threw = true;
  }
  check(threw, "one slot beyond the budget is refused rather than wrapping");

  threw = false;
  try {
    RequestPlan small(kGeneration);
    small.add_slot(0, 0, 0, /*length=*/0);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw,
        "a zero-length slot is refused, since it would never raise a "
        "completion and its layer could never complete");

  threw = false;
  try {
    RequestPlan small(kGeneration);
    small.add_slot(0, 0, 0, kPieceBytes);
    small.slot(1);
  } catch (const std::out_of_range&) {
    threw = true;
  }
  check(threw, "a slot index beyond the plan is refused");
}

}  // namespace

int main() {
  try {
    test_a_layer_needs_every_chunk();
    test_slot_indices_are_unique_across_chunks();
    test_arrivals_interleaved_across_nodes();
    test_a_sliding_window_layer_does_not_wait_for_every_chunk();
    test_unknown_and_superseded_arrivals();
    test_the_slot_budget_is_enforced();
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
