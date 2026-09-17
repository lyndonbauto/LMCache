// SPDX-License-Identifier: Apache-2.0
#pragma once

// Turning a model layout into the slot schedule for one pipelined request.
//
// layer_pipeline.h owns what a slot *means* and when a layer is done. This
// file owns where the slots come from: given the registered layout and the
// chunks a request needs, it produces every write the servers should perform,
// ordered layer-major so layer 0 is asked for first.
//
// == The trap this exists to avoid ==
//
// In the standard layout the K/V dimension is the **outermost** one, not the
// layer dimension:
//
//   (kv_size, num_layers, num_slots, hidden_dim)
//
// K for every layer comes first, then V for every layer. So one model layer
// does **not** occupy one contiguous byte range -- it occupies `kv_size`
// disjoint ranges, separated by `num_layers * num_slots * hidden_dim`
// elements.
//
// A planner that emitted one slot per layer per chunk would deliver K,
// see its single slot land, report the layer ready, and hand the model a
// cache whose V half is still whatever was in the window before. The tensors
// would look correct -- right shape, right dtype, plausible magnitudes -- and
// nothing would raise an error. That is the worst failure shape available
// here, and it is the reason this file computes plane ranges explicitly
// rather than treating a layer as a range.
//
//   slots(layer L, chunk c) = kv_size * ceil(plane_bytes / max_write_bytes)
//
// where `plane_bytes = num_slots * hidden_dim * element_size`.
//
// The `NL_X_NB_BS_HS` engine format is the exception: it drops the leading
// K/V dimension, so the layer dimension is outermost and a layer *is*
// contiguous. That needs no special case here -- it is `kv_size == 1`, and
// the same arithmetic produces a single plane.
//
// == Why the caller supplies the chunk placements ==
//
// Two reasons, and both are about not guessing.
//
// LMCache chooses every destination address, because the window is its own
// registered memory; the server is told where to write and never decides. So
// the base offset of each chunk's object within the leased window is an input
// here, not something to derive.
//
// And "which chunks participate" is group-dependent. A sliding-window group
// covers only a window of trailing chunks, so requiring every chunk of the
// request for such a group would wait forever. Rather than have this file
// infer windows, the caller passes exactly the (chunk, object group) pairs it
// wants, and the plan counts what it is given. `participating_chunks` exists
// so that decision is made once, in one tested place, instead of open-coded
// at each call site.
//
// == Why strides are not derived from model config ==
//
// They come from the layout LMCache already publishes at registration --
// `MemoryLayoutDesc`, one shape and dtype per kernel group, in
// `kernel_group_indices` order. Re-deriving them from model config would be a
// second source of truth that can drift. Each kernel group is internally
// uniform by construction (every layer in it shares one shape), so per-layer
// offsets are exactly computable; the hazard is not that strides are
// unpredictable but that one group's stride gets used for another group's
// layers, which is why geometry is held per kernel group here and never
// flattened.

#include "layer_pipeline.h"

#include <cstddef>
#include <cstdint>
#include <map>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// A contiguous span of an object group's payload.
struct ByteRange {
  size_t offset = 0;
  size_t length = 0;
};

// One kernel group's geometry, as published by `MemoryLayoutDesc`.
//
// Taken from the group's shape and dtype: for the standard
// `(kv_size, num_layers, num_slots, hidden_dim)` shape all four fields are
// read directly, and for `NL_X_NB_BS_HS` the shape has no leading K/V
// dimension, so `kv_size` is 1.
//
// `num_slots` is a slot count and not a token count. Compressed groups have
// `tokens_per_block != slots_per_block`, so it must come from the shape
// rather than from the request's token count.
struct KernelGroupLayout {
  // Global layer indices this group holds, in the order they appear along the
  // tensor's layer dimension. Position in this vector is the layer's stride
  // index; the values are what vLLM's `layer_name` resolves to.
  std::vector<uint32_t> layer_indices;
  // Number of K/V planes per layer: 2 for the standard layout, 1 when the
  // engine format puts the layer dimension outermost.
  uint32_t kv_size = 0;
  size_t num_slots = 0;
  size_t hidden_dim = 0;
  size_t element_size = 0;
};

// One object group's payload: its kernel groups concatenated in order.
//
// The payload is `[kg0 K][kg0 V][kg1 K][kg1 V]` -- planes are outermost
// *within* each kernel group, not across the object -- so a kernel group's
// base offset is the running sum of the sizes of those before it.
struct ObjectGroupLayout {
  uint32_t object_group_id = 0;
  std::vector<KernelGroupLayout> kernel_groups;
};

// Where one chunk's object for one object group has been placed in the
// registered window.
//
// `dest_offset` is relative to the base of the leased window and is chosen by
// LMCache.
struct ChunkPlacement {
  uint32_t chunk_id = 0;
  uint32_t object_group_id = 0;
  size_t dest_offset = 0;
};

// Chunks a group covers: the trailing `window_chunks` of `chunk_count`.
//
// Pass `window_chunks == 0` for a group with no window, such as full
// attention, which covers every chunk. A window wider than the request is
// clamped to it.
//
// Returns chunk ids ascending, where a chunk id is its index in the request's
// chunk list, so the last chunk is `chunk_count - 1`.
std::vector<uint32_t> participating_chunks(uint32_t chunk_count,
                                           uint32_t window_chunks);

// Bytes in one K/V plane of `group`: one layer's extent within one K/V half.
size_t plane_bytes(const KernelGroupLayout& group);

// Total bytes `group`'s tensor occupies.
size_t kernel_group_bytes(const KernelGroupLayout& group);

// Total bytes one chunk's object for `layout` occupies.
size_t object_group_bytes(const ObjectGroupLayout& layout);

// Resolves global layer indices to byte ranges within an object group's
// payload, and builds the slot schedule for a request.
//
// Built once from the registered layout and then reused for every request, so
// the layer lookup is a prepared map rather than a search.
//
// Not thread safe to construct; const afterwards, so concurrent planning is
// fine.
class SlotPlanner {
 public:
  // Build a planner over every object group of the model.
  //
  // Throws std::invalid_argument if `layouts` is empty, if any kernel group
  // has a zero dimension, `kv_size` of 0, or no layers, or if a global layer
  // index appears in more than one kernel group -- a layer belongs to exactly
  // one kernel group, and a duplicate would make its plane offsets ambiguous.
  explicit SlotPlanner(std::vector<ObjectGroupLayout> layouts);

  // Byte ranges of `layer_id` within its object group's payload, one per K/V
  // plane, ascending by offset.
  //
  // Offsets are relative to the start of the object group's payload, so a
  // caller adds the chunk's `dest_offset` to place them in the window.
  //
  // Throws std::out_of_range if `layer_id` is not in the layout.
  std::vector<ByteRange> layer_plane_ranges(uint32_t layer_id) const;

  // Object group holding `layer_id`.
  //
  // Throws std::out_of_range if `layer_id` is not in the layout.
  uint32_t object_group_of_layer(uint32_t layer_id) const;

  // Global layer indices in the layout, ascending.
  std::vector<uint32_t> layer_ids() const;

  // Build the slot schedule for one request.
  //
  // Slots are appended layer-major -- every chunk's pieces of layer 0, then
  // of layer 1, and so on -- which is the order the servers are asked to push
  // in so that the earliest layers arrive first. The order is only a hint:
  // SRD reorders writes in flight, and independent nodes interleave anyway.
  //
  // `placements` decides which chunks participate for each object group;
  // placements for an object group with no layers in the layout are ignored.
  // A layer whose object group has no placements contributes no slots and is
  // therefore never reported ready, which is the correct answer for a layer
  // the request is not fetching.
  //
  // Throws std::invalid_argument if `max_write_bytes` is 0 or if two
  // placements share a (chunk, object group) pair, and std::length_error if
  // the schedule exceeds `kMaxSlotsPerRequest`.
  RequestPlan plan_request(const std::vector<ChunkPlacement>& placements,
                           size_t max_write_bytes, uint16_t generation) const;

 private:
  // Where a layer sits: which object group, and its plane ranges within that
  // group's payload.
  struct LayerLocation {
    uint32_t object_group_id = 0;
    std::vector<ByteRange> planes;
  };

  std::vector<ObjectGroupLayout> layouts_;
  std::map<uint32_t, LayerLocation> layer_locations_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
