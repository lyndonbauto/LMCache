// SPDX-License-Identifier: Apache-2.0

#include "slot_planner.h"

#include <algorithm>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

using lmcache::connector::plane_segment_bytes;

size_t ceil_div(size_t numerator, size_t denominator) {
  return (numerator + denominator - 1) / denominator;
}

}  // namespace

std::vector<uint32_t> participating_chunks(uint32_t chunk_count,
                                           uint32_t window_chunks) {
  const uint32_t covered =
      window_chunks == 0 ? chunk_count : std::min(window_chunks, chunk_count);
  std::vector<uint32_t> out;
  out.reserve(covered);
  // A window covers the *trailing* chunks, the ones nearest the tail of the
  // sequence, so it starts where the uncovered prefix ends.
  for (uint32_t chunk = chunk_count - covered; chunk < chunk_count; ++chunk) {
    out.push_back(chunk);
  }
  return out;
}

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

RequestPlan SlotPlanner::plan_request(
    const std::vector<ChunkPlacement>& placements, size_t max_record_bytes,
    size_t max_write_bytes, uint16_t generation) const {
  if (max_record_bytes == 0) {
    throw std::invalid_argument(
        "SlotPlanner: max_record_bytes is zero, so a plane could not be cut "
        "into records");
  }
  if (max_write_bytes == 0) {
    throw std::invalid_argument(
        "SlotPlanner: max_write_bytes is zero, so no write could carry any "
        "payload");
  }

  // Group placements by object group, preserving the caller's chunk order,
  // and reject a repeat: two placements for one (chunk, object group) would
  // double-count that layer's expected slots and the layer would never
  // complete.
  std::map<uint32_t, std::vector<const ChunkPlacement*>> by_group;
  std::set<std::pair<uint32_t, uint32_t>> seen;
  for (const ChunkPlacement& placement : placements) {
    const auto key =
        std::make_pair(placement.object_group_id, placement.chunk_id);
    if (!seen.insert(key).second) {
      throw std::invalid_argument("SlotPlanner: chunk " +
                                  std::to_string(placement.chunk_id) +
                                  " is placed twice for object group " +
                                  std::to_string(placement.object_group_id));
    }
    by_group[placement.object_group_id].push_back(&placement);
  }

  RequestPlan plan(generation);
  // Layer-major: ascending layer, and within a layer every participating
  // chunk, so the servers are asked for layer 0 everywhere before layer 1
  // anywhere.
  for (const auto& [layer_id, location] : layer_locations_) {
    const auto group_placements = by_group.find(location.object_group_id);
    if (group_placements == by_group.end()) {
      continue;
    }
    for (const ChunkPlacement* placement : group_placements->second) {
      for (const ByteRange& plane : location.planes) {
        const size_t base = placement->dest_offset + plane.offset;
        // One plane is one or more records, and a slot is one of them. The
        // last is short when the plane is not a whole multiple.
        const size_t record_bytes =
            plane_segment_bytes(plane.length, max_record_bytes);
        if (record_bytes > max_write_bytes) {
          throw std::invalid_argument(
              "SlotPlanner: a record of " + std::to_string(record_bytes) +
              " bytes exceeds the maximum RDMA write of " +
              std::to_string(max_write_bytes) +
              ", and a sink cannot name part of a record, so lower the "
              "record cap");
        }
        const size_t pieces = ceil_div(plane.length, record_bytes);
        for (size_t piece = 0; piece < pieces; ++piece) {
          const size_t piece_offset = piece * record_bytes;
          const size_t piece_length =
              std::min(record_bytes, plane.length - piece_offset);
          plan.add_slot(layer_id, placement->chunk_id, base + piece_offset,
                        piece_length);
        }
      }
    }
  }
  return plan;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
