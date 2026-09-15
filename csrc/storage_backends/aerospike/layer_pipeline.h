// SPDX-License-Identifier: Apache-2.0
#pragma once

// Per-layer arrival tracking for a pipelined RDMA fetch.
//
// The baseline kv-sink protocol makes a fetch all-or-nothing: the server
// fences on its own send CQ and the info reply *is* the completion, so
// LMCache cannot learn that layer 0 landed while layer 7 is still in flight.
// Pipelining needs that signal, and RDMA already has the primitive for it --
// RDMA_WRITE_WITH_IMM raises a receive completion carrying 32 bits of
// immediate data, and the payload is guaranteed visible before that
// completion is.
//
// This file owns the two things LMCache needs in order to consume such a
// stream: what those 32 bits mean, and when a layer is done.
//
// == Why the immediate carries a slot index, not a layer id ==
//
// A layer can be larger than one RDMA write. EFA caps a single write at
// `max_rdma_size` (from efadv_query_device) and the destination window bounds
// it further, so one layer becomes N writes -- "a piece of a layer". The
// immediate therefore names a *slot*: one write's worth of one layer, from a
// plan LMCache built itself. Because LMCache chose every destination offset,
// a slot index is enough to recover the layer, the offset, and the length
// without the server knowing anything about transformer layers.
//
// == Why arrival order cannot be trusted ==
//
// This is the load-bearing constraint, and it is specific to AWS. SRD
// deliberately provides reliable but *out-of-order* delivery -- it sprays
// packets across up to 64 paths to cut tail latency and leaves reordering to
// the layer above. AWS's own SRD.txt states it, and there is no ordering
// guarantee between any two operations even on a single queue pair.
//
// The consequence: readiness is a **set**, not a high-water mark. Layer 5 can
// complete before layer 2. A watermark ("everything up to layer k has
// arrived") would be correct on Soft-RoCE and silently wrong on EFA, which is
// the worst possible failure shape -- it would pass every local test.
//
// vLLM consumes layers in order and so asks "is layer i ready?", which a set
// answers directly. Nothing here assumes the answer arrives in order.
//
// == Why there is a generation ==
//
// A write from a fetch that already timed out can land after the window has
// been leased to a different request. The window rkey is still valid, so the
// NIC will happily perform that write. Tagging each fetch with a generation
// and rejecting immediates that do not match is what stops a late writer from
// being mistaken for progress on the current request. It does not prevent the
// stray write itself -- only re-registration can do that -- but it stops
// LMCache from acting on it.

#include <cstddef>
#include <cstdint>
#include <map>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// Immediate data layout: generation in the high 16 bits, slot index in the
// low 16. 65536 slots is far more than any single fetch needs (a 60-layer
// model at several writes per layer is O(100)), and 65536 generations wrap
// slowly enough to outlive any in-flight write.
constexpr uint32_t kGenerationShift = 16;
constexpr uint32_t kSlotIndexMask = 0xffffu;
constexpr uint32_t kMaxSlotsPerFetch = 0x10000u;

// Pack a generation and slot index into the 32 bits of RDMA immediate data.
uint32_t encode_immediate(uint16_t generation, uint16_t slot_index);

// Extract the generation from immediate data produced by encode_immediate.
uint16_t immediate_generation(uint32_t immediate);

// Extract the slot index from immediate data produced by encode_immediate.
uint16_t immediate_slot_index(uint32_t immediate);

// One RDMA write's worth of one layer: a piece of a layer.
//
// `offset` is relative to the base of the registered window this fetch was
// leased, and is chosen by LMCache. `length` is bounded by the device's
// maximum RDMA transfer size.
struct FetchSlot {
  uint32_t layer_id = 0;
  size_t offset = 0;
  size_t length = 0;
};

// The set of writes LMCache expects for one pipelined fetch.
//
// Built by LMCache before the fetch is issued, in the order it wants the
// server to push -- layer-major, so layer 0 is requested first. The ordering
// is a *hint*: the server is free to complete them in any order, and on SRD
// it will.
//
// Not thread safe. A plan is built by one thread and then read.
class FetchPlan {
 public:
  // Construct an empty plan tagged with `generation`.
  //
  // The generation distinguishes this fetch from a previous one that may
  // still have writes in flight against the same window.
  explicit FetchPlan(uint16_t generation) : generation_(generation) {}

  // Append a slot and return its index, for use as immediate data.
  //
  // Throws std::length_error if the plan already holds kMaxSlotsPerFetch
  // slots, and std::invalid_argument if `length` is zero.
  uint16_t add_slot(uint32_t layer_id, size_t offset, size_t length);

  // Return the slot at `slot_index`.
  //
  // Throws std::out_of_range if `slot_index` is not in the plan.
  const FetchSlot& slot(uint16_t slot_index) const;

  // Number of slots in the plan.
  size_t slot_count() const { return slots_.size(); }

  // Layer ids present in the plan, ascending and deduplicated.
  std::vector<uint32_t> layer_ids() const;

  // How many slots `layer_id` needs before it is complete. Zero if the layer
  // is not in the plan.
  uint32_t expected_slots(uint32_t layer_id) const;

  // Generation this plan was tagged with.
  uint16_t generation() const { return generation_; }

  // Immediate data the server should echo back for `slot_index`.
  //
  // Throws std::out_of_range if `slot_index` is not in the plan.
  uint32_t immediate_for(uint16_t slot_index) const;

 private:
  uint16_t generation_;
  std::vector<FetchSlot> slots_;
  std::map<uint32_t, uint32_t> expected_per_layer_;
};

// Outcome of feeding one immediate value to LayerReadiness.
//
// Split from the "is it ready" query deliberately: a caller that ignores the
// difference between a duplicate and a stale generation cannot tell a benign
// retransmit from a late writer scribbling into a reused window.
enum class ArrivalStatus {
  // Counted, and this slot completed its layer.
  kLayerComplete,
  // Counted; the layer still has outstanding slots.
  kAccepted,
  // This slot was already counted. Not an error.
  kDuplicate,
  // Generation does not match: a write from a superseded fetch. Ignored.
  kStaleGeneration,
  // Slot index is not in the plan. Indicates a protocol violation.
  kUnknownSlot,
};

// Tracks which layers of one fetch have fully landed.
//
// Copies the counts it needs out of the plan at construction, so it does not
// outlive-reference the plan.
//
// Not thread safe. In the intended use one thread polls the completion queue
// and feeds arrivals in; readers of layer readiness are on that same thread.
class LayerReadiness {
 public:
  // Build a tracker for `plan`.
  explicit LayerReadiness(const FetchPlan& plan);

  // Record the arrival of the slot named by `immediate`.
  //
  // Safe to call with arbitrary 32-bit values: anything not belonging to this
  // fetch is reported rather than counted.
  ArrivalStatus note_arrival(uint32_t immediate);

  // Report whether every slot of `layer_id` has landed.
  //
  // False for a layer that is not in the plan, since nothing guarantees its
  // bytes are present.
  bool is_layer_ready(uint32_t layer_id) const;

  // Layers that have fully landed, ascending.
  //
  // This is a set and not a prefix: on SRD a later layer can be ready while
  // an earlier one is not.
  std::vector<uint32_t> ready_layers() const;

  // Report whether every slot in the plan has landed.
  bool all_ready() const { return landed_slots_ == expected_slots_; }

  // Number of distinct slots counted so far.
  size_t slots_landed() const { return landed_slots_; }

 private:
  uint16_t generation_;
  size_t expected_slots_ = 0;
  size_t landed_slots_ = 0;
  std::vector<bool> slot_seen_;
  std::vector<uint32_t> slot_layer_;
  std::map<uint32_t, uint32_t> expected_per_layer_;
  std::map<uint32_t, uint32_t> landed_per_layer_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
