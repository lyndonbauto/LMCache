// SPDX-License-Identifier: Apache-2.0

#include "slot_planner.h"

#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

namespace lmcache {
namespace connector {
namespace rdma {

size_t plane_bytes(const KernelGroupLayout& group) {
  return group.num_slots * group.hidden_dim * group.element_size;
}

size_t kernel_group_bytes(const KernelGroupLayout& group) {
  return static_cast<size_t>(group.kv_size) * group.layer_indices.size() *
         plane_bytes(group);
}

size_t object_group_bytes(const ObjectGroupLayout& layout) {
  size_t total = 0;
  for (const KernelGroupLayout& group : layout.kernel_groups) {
    total += kernel_group_bytes(group);
  }
  return total;
}

size_t window_bytes_per_chunk(const std::vector<ObjectGroupLayout>& layouts,
                              size_t align_bytes) {
  size_t total = 0;
  for (const ObjectGroupLayout& layout : layouts) {
    size_t bytes = object_group_bytes(layout);
    if (align_bytes > 0) {
      bytes = (bytes + align_bytes - 1) / align_bytes * align_bytes;
    }
    total += bytes;
  }
  return total;
}

std::string window_fit_error(const std::vector<ObjectGroupLayout>& layouts,
                             size_t window_bytes, size_t align_bytes) {
  const size_t needed = window_bytes_per_chunk(layouts, align_bytes);
  if (needed <= window_bytes) {
    return {};
  }
  std::ostringstream os;
  os << "RDMA window_bytes is " << window_bytes << " but one chunk of this "
     << "model needs " << needed << " bytes, so no retrieve can be pipelined; "
     << "set rdma.window_bytes to at least " << needed;
  return os.str();
}

SlotPlanner::SlotPlanner(std::vector<ObjectGroupLayout> layouts)
    : layouts_(std::move(layouts)) {
  if (layouts_.empty()) {
    throw std::invalid_argument("SlotPlanner: no object groups in the layout");
  }

  for (const ObjectGroupLayout& layout : layouts_) {
    // A kernel group's base is the running sum of those declared before it,
    // because the payload is their tensors concatenated in order.
    size_t group_base = 0;
    for (const KernelGroupLayout& group : layout.kernel_groups) {
      if (group.layer_indices.empty()) {
        throw std::invalid_argument(
            "SlotPlanner: kernel group holds no layers");
      }
      if (group.kv_size == 0 || group.num_slots == 0 || group.hidden_dim == 0 ||
          group.element_size == 0) {
        throw std::invalid_argument(
            "SlotPlanner: kernel group has a zero dimension, so its plane "
            "size would be zero and its layers could never complete");
      }

      const size_t plane = plane_bytes(group);
      const size_t num_layers = group.layer_indices.size();
      for (size_t position = 0; position < num_layers; ++position) {
        const uint32_t layer_id = group.layer_indices[position];
        if (layer_locations_.count(layer_id) != 0) {
          throw std::invalid_argument(
              "SlotPlanner: layer " + std::to_string(layer_id) +
              " appears in more than one kernel group, so its plane offsets "
              "would be ambiguous");
        }

        LayerLocation location;
        location.object_group_id = layout.object_group_id;
        location.planes.reserve(group.kv_size);
        // The K/V dimension is outermost, so a layer's planes are strided by
        // a whole layer dimension apart -- not adjacent. This loop is the
        // reason a layer is several ranges rather than one.
        for (uint32_t kv = 0; kv < group.kv_size; ++kv) {
          const size_t plane_index =
              (static_cast<size_t>(kv) * num_layers) + position;
          location.planes.push_back(
              ByteRange{group_base + (plane_index * plane), plane});
        }
        layer_locations_.emplace(layer_id, std::move(location));
      }

      group_base += kernel_group_bytes(group);
    }
  }

  if (layer_locations_.empty()) {
    throw std::invalid_argument("SlotPlanner: layout holds no layers");
  }
}

std::vector<ByteRange> SlotPlanner::layer_plane_ranges(
    uint32_t layer_id) const {
  const auto found = layer_locations_.find(layer_id);
  if (found == layer_locations_.end()) {
    throw std::out_of_range("SlotPlanner: no layer " +
                            std::to_string(layer_id) + " in the layout");
  }
  return found->second.planes;
}

uint32_t SlotPlanner::object_group_of_layer(uint32_t layer_id) const {
  const auto found = layer_locations_.find(layer_id);
  if (found == layer_locations_.end()) {
    throw std::out_of_range("SlotPlanner: no layer " +
                            std::to_string(layer_id) + " in the layout");
  }
  return found->second.object_group_id;
}

std::vector<uint32_t> SlotPlanner::layer_ids() const {
  std::vector<uint32_t> out;
  out.reserve(layer_locations_.size());
  for (const auto& entry : layer_locations_) {
    out.push_back(entry.first);
  }
  return out;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
