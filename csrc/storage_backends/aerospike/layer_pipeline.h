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
// immediate therefore names a *slot*: one write's worth of one layer of one
// chunk, from a plan LMCache built itself. Because LMCache chose every
// destination offset, a slot index is enough to recover the layer, the
// offset, and the length without the server knowing anything about
// transformer layers.
//
// == Why the plan is scoped to a request, not to a fetch ==
//
// This is the constraint that shapes the whole file, and getting it wrong
// produces a cache that looks correct.
//
// vLLM computes layer L for the **whole sequence**, so it needs layer L of
// every participating chunk. Chunks are distinct keys, distributed across
// cluster nodes by digest, so one layer's data arrives from several nodes via
// several fetch commands. All of those notifications land on one queue pair
// and therefore in one readiness table.
//
// So slot indices must be assigned once per *request* and not per fetch
// command. If each node's fetch numbered its slots from zero, node 2's slot 5
// would be indistinguishable from node 4's slot 5: the table would count the
// second arrival as a duplicate, that layer would never reach its expected
// count, and the fetch would hang -- or worse, a different layer's counter
// would complete early and hand the model uninitialised KV.
//
// A layer is therefore ready only when every slot of every participating
// chunk has landed:
//
//   expected(L) = sum over participating chunks c of slots(L, c)
//   ready(L)    = landed(L) == expected(L)
//
// `add_slot` tallies expected counts as slots are appended, so this falls out
// of building one plan per request rather than needing separate bookkeeping.
//
// == Why "participating chunks" is not just "all chunks" ==
//
// A sliding-window group only covers a window of trailing chunks, so
// requiring every chunk of the request for such a group would wait forever.
// The caller must add slots only for the chunks a group actually covers; the
// plan counts what it is given and does not assume a rectangular
// chunks x layers grid.
//
// This also makes the plan correct under CacheBlend without special casing.
// Each blend leg reads a different subset of object groups, but a layer
// belongs to exactly one kernel group and therefore exactly one object group
// and one leg, so per-layer counts never mix legs. Where two legs cover
// different chunks of the same layer, summing over participating chunks is
// precisely the intended answer.
//
// == Why arrival order cannot be trusted ==
//
// This is the load-bearing runtime constraint, and it is specific to AWS. SRD
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
// Multi-node fetch compounds this: even if every node pushed its layers in
// perfect order, the nodes are independent, so the merged arrival stream at
// the queue pair is interleaved regardless.
//
// vLLM consumes layers in order and so asks "is layer i ready?", which a set
// answers directly. Nothing here assumes the answer arrives in order.
//
// == Why there is a generation ==
//
// A write from a request that already timed out can land after the window has
// been leased to a different request. The window rkey is still valid, so the
// NIC will happily perform that write. Tagging each request with a generation
// and rejecting immediates that do not match is what stops a late writer from
// being mistaken for progress on the current request. It does not prevent the
// stray write itself -- only re-registration can do that -- but it stops
// LMCache from acting on it.
//
// The generation identifies the *request*, so every fetch command issued on
// that request's behalf carries the same one.

#include <cstddef>
#include <cstdint>
#include <map>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// Immediate data layout: generation in the high 16 bits, slot index in the
// low 16.
//
// The slot budget is per request, and a request is larger than a single
// fetch: it spans every participating chunk, so
//
//   slots ~= chunks x layers x kv_size x pieces_per_plane
//
// An 80-layer model over 100 chunks with kv_size = 2 and one piece per plane
// is 16,000 -- comfortable, but a 4x margin rather than a 4000x one. A long
// context with small chunks and multi-piece planes could approach the limit,
// so `add_slot` throws rather than letting an index wrap silently. If the
// ceiling is ever reached the fix is a coarser readiness granularity, not
// more bits, since the 32 bits are fixed by RDMA.
constexpr uint32_t kGenerationShift = 16;
constexpr uint32_t kSlotIndexMask = 0xffffu;
constexpr uint32_t kMaxSlotsPerRequest = 0x10000u;

// Pack a generation and slot index into the 32 bits of RDMA immediate data.
uint32_t encode_immediate(uint16_t generation, uint16_t slot_index);

// Extract the generation from immediate data produced by encode_immediate.
uint16_t immediate_generation(uint32_t immediate);

// Extract the slot index from immediate data produced by encode_immediate.
uint16_t immediate_slot_index(uint32_t immediate);

// One RDMA write's worth of one layer of one chunk: a piece of a layer.
//
// `offset` is relative to the base of the registered window this request was
// leased, and is chosen by LMCache. `length` is bounded by the device's
// maximum RDMA transfer size.
//
// `chunk_id` is what lets a request-scoped plan be split into per-node fetch
// commands, since chunks are what the cluster distributes. Its numbering is
// the caller's own -- typically the chunk's position in the request -- and is
// never interpreted here.
struct FetchSlot {
  uint32_t layer_id = 0;
  uint32_t chunk_id = 0;
  size_t offset = 0;
  size_t length = 0;
};

// Every write LMCache expects for one request, across all participating
// chunks and all the nodes holding them.
//
// Built before any fetch command is issued, in the order LMCache wants the
// servers to push -- layer-major, so layer 0 is requested first. The ordering
// is a *hint*: a server may complete its writes in any order, and on SRD it
// will, and independent nodes interleave in any case.
//
// Not thread safe. A plan is built by one thread and then read.
class RequestPlan {
 public:
  // Construct an empty plan tagged with `generation`.
  //
  // The generation distinguishes this request from a previous one that may
  // still have writes in flight against the same window.
  explicit RequestPlan(uint16_t generation) : generation_(generation) {}

  // Append a slot for `layer_id` of `chunk_id` and return its index, for use
  // as immediate data.
  //
  // Call this for every (chunk, layer, plane, piece) the request expects,
  // including chunks held by other nodes: the index space is shared across
  // the whole request, which is what keeps two nodes' notifications
  // distinguishable.
  //
  // Throws std::length_error if the plan already holds kMaxSlotsPerRequest
  // slots, and std::invalid_argument if `length` is zero.
  uint16_t add_slot(uint32_t layer_id, uint32_t chunk_id, size_t offset,
                    size_t length);

  // Return the slot at `slot_index`.
  //
  // Throws std::out_of_range if `slot_index` is not in the plan.
  const FetchSlot& slot(uint16_t slot_index) const;

  // Number of slots in the plan.
  size_t slot_count() const { return slots_.size(); }

  // Layer ids present in the plan, ascending and deduplicated.
  std::vector<uint32_t> layer_ids() const;

  // Chunk ids present in the plan, ascending and deduplicated.
  std::vector<uint32_t> chunk_ids() const;

  // Slot indices belonging to `chunk_id`, ascending. Empty if the chunk is
  // not in the plan.
  //
  // This is how a request-scoped plan becomes per-node fetch commands: group
  // the request's chunks by the node that holds them, then send each node the
  // slots of its own chunks. The indices stay in the request's numbering, so
  // the immediates the nodes echo back remain unambiguous.
  std::vector<uint16_t> slots_for_chunk(uint32_t chunk_id) const;

  // How many slots `layer_id` needs before it is complete, summed over every
  // participating chunk. Zero if the layer is not in the plan.
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
  std::map<uint32_t, std::vector<uint16_t>> slots_per_chunk_;
};

// Outcome of feeding one immediate value to LayerReadiness.
//
// Split from the "is it ready" query deliberately: a caller that ignores the
// difference between a duplicate and a stale generation cannot tell a benign
// retransmit from a late writer scribbling into a reused window.
enum class ArrivalStatus {
  // Counted, and this slot completed its layer across every participating
  // chunk.
  kLayerComplete,
  // Counted; the layer still has outstanding slots, possibly on other nodes.
  kAccepted,
  // This slot was already counted. Not an error.
  kDuplicate,
  // Generation does not match: a write from a superseded request. Ignored.
  kStaleGeneration,
  // Slot index is not in the plan. Indicates a protocol violation.
  kUnknownSlot,
};

// Tracks which layers of one request have fully landed.
//
// Copies the counts it needs out of the plan at construction, so it does not
// outlive-reference the plan.
//
// One tracker serves the whole request, including chunks fetched from other
// nodes, because all of their notifications arrive on the same queue pair.
//
// Not thread safe. In the intended use one thread polls the completion queue
// and feeds arrivals in; readers of layer readiness are on that same thread.
class LayerReadiness {
 public:
  // Build a tracker for `plan`.
  explicit LayerReadiness(const RequestPlan& plan);

  // Record the arrival of the slot named by `immediate`.
  //
  // Safe to call with arbitrary 32-bit values: anything not belonging to this
  // request is reported rather than counted.
  ArrivalStatus note_arrival(uint32_t immediate);

  // Record that the server will never write the slot at `slot_index`.
  //
  // A fetch reply names the slots it could not serve -- a missing record, a
  // read error. Without this the request would wait for them until its
  // deadline, because a slot that never arrives is indistinguishable from one
  // still in flight.
  //
  // The slot's layer becomes permanently unready rather than being reported
  // complete on partial data: some of its bytes are absent, and serving a
  // layer from a partly-written buffer is the failure this whole file exists
  // to prevent. The caller is expected to treat the affected layers as a
  // cache miss and recompute them.
  //
  // Returns true if this call newly marked the slot, and false if it was
  // already marked or has in fact already landed. Safe to call with a slot
  // index outside the plan, which is ignored.
  bool note_unservable(uint16_t slot_index);

  // Report whether every slot of `layer_id` has landed, over every
  // participating chunk.
  //
  // False for a layer that is not in the plan, since nothing guarantees its
  // bytes are present, and false for a layer with an unservable slot.
  bool is_layer_ready(uint32_t layer_id) const;

  // Layers that can never complete because a slot of theirs was reported
  // unservable, ascending.
  //
  // Empty unless note_unservable has been called. These are the layers the
  // caller must recompute.
  std::vector<uint32_t> unservable_layers() const;

  // Report whether any slot has been marked unservable.
  bool has_unservable_slots() const { return !unservable_per_layer_.empty(); }

  // Layers that have fully landed, ascending.
  //
  // This is a set and not a prefix: on SRD a later layer can be ready while
  // an earlier one is not, and independent nodes interleave regardless.
  std::vector<uint32_t> ready_layers() const;

  // Report whether every slot in the plan has landed.
  //
  // Never true once a slot is unservable: that slot cannot also land, so the
  // counts can no longer meet.
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
  // Layers with at least one slot the server will not write, and how many.
  std::map<uint32_t, uint32_t> unservable_per_layer_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
