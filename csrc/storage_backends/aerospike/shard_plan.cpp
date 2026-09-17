// SPDX-License-Identifier: Apache-2.0
#include "shard_plan.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace lmcache {
namespace connector {
namespace {

size_t ceil_div(size_t numerator, size_t denominator) {
  return (numerator + denominator - 1) / denominator;
}

}  // namespace

uint32_t pieces_per_plane(const ShardPlan& plan) {
  if (plan.plane_b == 0 || plan.seg_b == 0) {
    return 1;
  }
  return static_cast<uint32_t>(ceil_div(plan.plane_b, plan.seg_b));
}

size_t plane_segment_bytes(size_t plane_bytes, size_t max_record_bytes) {
  if (plane_bytes == 0) {
    throw std::invalid_argument("plane_segment_bytes: plane_bytes is zero");
  }
  if (max_record_bytes == 0) {
    throw std::invalid_argument(
        "plane_segment_bytes: max_record_bytes is zero");
  }
  return ceil_div(plane_bytes, ceil_div(plane_bytes, max_record_bytes));
}

ShardPlan make_shard_plan(size_t payload_bytes, size_t target_segment_bytes,
                          size_t max_record_bytes,
                          size_t single_record_threshold_bytes,
                          size_t plane_bytes) {
  if (payload_bytes == 0) {
    throw std::invalid_argument("make_shard_plan: payload_bytes is zero");
  }
  if (max_record_bytes == 0) {
    throw std::invalid_argument("make_shard_plan: max_record_bytes is zero");
  }

  // The plane-aligned path is checked before the single-record fast path on
  // purpose. A payload spanning several planes would otherwise land in one
  // record whenever it fit under the threshold, which is the "one record
  // holding two planes" layout this module exists to rule out.
  if (plane_bytes > 0 && payload_bytes % plane_bytes == 0) {
    uint32_t pieces =
        static_cast<uint32_t>(ceil_div(plane_bytes, max_record_bytes));
    size_t seg_b = plane_segment_bytes(plane_bytes, max_record_bytes);
    size_t planes = payload_bytes / plane_bytes;
    size_t nseg = static_cast<size_t>(pieces) * planes;
    if (seg_b <= max_record_bytes &&
        nseg <= std::numeric_limits<uint32_t>::max()) {
      return {static_cast<uint32_t>(nseg), seg_b, plane_bytes};
    }
    // Fall through to byte-count sharding, which will either succeed with a
    // smaller stride or throw with a clearer message.
  }

  if (payload_bytes <= single_record_threshold_bytes &&
      payload_bytes <= max_record_bytes) {
    return {1, payload_bytes, 0};
  }

  size_t target = target_segment_bytes == 0
                      ? max_record_bytes
                      : std::min(target_segment_bytes, max_record_bytes);
  size_t nseg = ceil_div(payload_bytes, target);
  size_t seg_b = ceil_div(payload_bytes, nseg);
  if (seg_b > max_record_bytes || nseg > std::numeric_limits<uint32_t>::max()) {
    throw std::runtime_error("payload cannot be sharded within record cap");
  }
  return {static_cast<uint32_t>(nseg), seg_b, 0};
}

ShardRange segment_range(const ShardPlan& plan, uint32_t index,
                         size_t total_bytes) {
  if (index >= plan.nseg) {
    throw std::out_of_range("segment_range: index beyond plan");
  }
  if (plan.seg_b == 0) {
    throw std::invalid_argument("segment_range: plan has zero segment size");
  }

  if (plan.plane_b == 0) {
    size_t offset = static_cast<size_t>(index) * plan.seg_b;
    if (offset >= total_bytes) {
      return {total_bytes, 0};
    }
    return {offset, std::min(plan.seg_b, total_bytes - offset)};
  }

  // Plane-aligned: the record's position is resolved within its own plane, so
  // a plane whose size is not an exact multiple of seg_b ends in a short
  // record instead of spilling into the next plane.
  uint32_t pieces = pieces_per_plane(plan);
  size_t plane_index = index / pieces;
  size_t piece_index = index % pieces;
  size_t piece_start = piece_index * plan.seg_b;
  size_t offset = plane_index * plan.plane_b + piece_start;
  if (offset >= total_bytes || piece_start >= plan.plane_b) {
    return {std::min(offset, total_bytes), 0};
  }
  size_t length = std::min(plan.seg_b, plan.plane_b - piece_start);
  return {offset, std::min(length, total_bytes - offset)};
}

}  // namespace connector
}  // namespace lmcache
