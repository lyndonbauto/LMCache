// SPDX-License-Identifier: Apache-2.0
#pragma once

// Where each layer of a registered model sits inside one chunk's object, and
// whether one chunk fits an RDMA window.
//
// The slot schedule itself -- which records to fetch, in which order -- is
// built in Python by `FetchPlanner` (lmcache/v1/layerwise/planner.py). The
// fetch driver uses this file only to validate the registered layout and to
// refuse a window that cannot hold one chunk.
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
// elements. A layer reported ready after its K range alone would hand the
// model a V half holding whatever was in the window before, with no error.
//
// The `NL_X_NB_BS_HS` engine format is the exception: it drops the leading
// K/V dimension, so the layer dimension is outermost and a layer *is*
// contiguous. That needs no special case here -- it is `kv_size == 1`, and
// the same arithmetic produces a single plane.
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

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
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

// Bytes in one K/V plane of `group`: one layer's extent within one K/V half.
size_t plane_bytes(const KernelGroupLayout& group);

// Total bytes `group`'s tensor occupies.
size_t kernel_group_bytes(const KernelGroupLayout& group);

// Total bytes one chunk's object for `layout` occupies.
size_t object_group_bytes(const ObjectGroupLayout& layout);

// Bytes one chunk takes inside an RDMA window: one object per object group,
// each rounded up to `align_bytes` the way L1 allocates it. `align_bytes` of
// 0 means no rounding.
size_t window_bytes_per_chunk(const std::vector<ObjectGroupLayout>& layouts,
                              size_t align_bytes);

// Empty when one chunk fits in a window of `window_bytes`; otherwise a
// message naming both sizes and the fix. A window that cannot hold one chunk
// can serve no pipelined retrieve at all.
std::string window_fit_error(const std::vector<ObjectGroupLayout>& layouts,
                             size_t window_bytes, size_t align_bytes);

// Resolves global layer indices to byte ranges within an object group's
// payload.
//
// Built once from the registered layout, so the layer lookup is a prepared
// map rather than a search.
//
// Not thread safe to construct; const afterwards, so concurrent lookups are
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
  // caller adds the chunk's destination offset to place them in the window.
  //
  // Throws std::out_of_range if `layer_id` is not in the layout.
  std::vector<ByteRange> layer_plane_ranges(uint32_t layer_id) const;

  // Object group holding `layer_id`.
  //
  // Throws std::out_of_range if `layer_id` is not in the layout.
  uint32_t object_group_of_layer(uint32_t layer_id) const;

  // Global layer indices in the layout, ascending.
  std::vector<uint32_t> layer_ids() const;

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
