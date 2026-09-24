// SPDX-License-Identifier: Apache-2.0
#include "shard_plan.h"

#include <algorithm>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace lmcache {
namespace connector {
namespace {

size_t ceil_div(size_t numerator, size_t denominator) {
  return (numerator + denominator - 1) / denominator;
}

void require_valid_runs(const std::vector<PlaneRun>& runs) {
  if (runs.empty()) {
    throw std::invalid_argument("layered shard plan: no runs");
  }
  for (const PlaneRun& run : runs) {
    if (run.plane_bytes == 0 || run.planes == 0) {
      throw std::invalid_argument(
          "layered shard plan: a run has a zero plane size or no planes");
    }
  }
}

uint64_t parse_positive(const std::string& field) {
  // 19 digits always fit in uint64_t, so stoull cannot throw out_of_range.
  if (field.empty() || field.size() > 19 ||
      field.find_first_not_of("0123456789") != std::string::npos) {
    throw std::invalid_argument("shard runs: malformed field '" + field + "'");
  }
  const uint64_t value = std::stoull(field);
  if (value == 0) {
    throw std::invalid_argument("shard runs: zero field");
  }
  return value;
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
      return {static_cast<uint32_t>(nseg), seg_b, plane_bytes, {}};
    }
    // Fall through to byte-count sharding, which will either succeed with a
    // smaller stride or throw with a clearer message.
  }

  if (payload_bytes <= single_record_threshold_bytes &&
      payload_bytes <= max_record_bytes) {
    return {1, payload_bytes, 0, {}};
  }

  size_t target = target_segment_bytes == 0
                      ? max_record_bytes
                      : std::min(target_segment_bytes, max_record_bytes);
  size_t nseg = ceil_div(payload_bytes, target);
  size_t seg_b = ceil_div(payload_bytes, nseg);
  if (seg_b > max_record_bytes || nseg > std::numeric_limits<uint32_t>::max()) {
    throw std::runtime_error("payload cannot be sharded within record cap");
  }
  return {static_cast<uint32_t>(nseg), seg_b, 0, {}};
}

size_t layered_payload_bytes(const std::vector<PlaneRun>& runs) {
  size_t total = 0;
  for (const PlaneRun& run : runs) {
    total += run.plane_bytes * run.planes;
  }
  return total;
}

ShardPlan make_layered_shard_plan(const std::vector<PlaneRun>& runs,
                                  size_t max_record_bytes) {
  require_valid_runs(runs);
  if (max_record_bytes == 0) {
    throw std::invalid_argument(
        "make_layered_shard_plan: max_record_bytes is zero");
  }

  const bool uniform =
      std::all_of(runs.begin(), runs.end(), [&](const PlaneRun& run) {
        return run.plane_bytes == runs.front().plane_bytes;
      });

  size_t nseg = 0;
  ShardPlan plan;
  for (const PlaneRun& run : runs) {
    const size_t seg_b = plane_segment_bytes(run.plane_bytes, max_record_bytes);
    nseg += ceil_div(run.plane_bytes, seg_b) * run.planes;
    if (nseg > std::numeric_limits<uint32_t>::max()) {
      throw std::runtime_error(
          "make_layered_shard_plan: record count overflows uint32_t");
    }
    plan.runs.push_back({run.plane_bytes, seg_b, run.planes});
  }
  plan.nseg = static_cast<uint32_t>(nseg);

  if (uniform) {
    // Collapse to the uniform form so the records, their indices and the meta
    // record are identical to what make_shard_plan() writes for this model.
    plan.plane_b = plan.runs.front().plane_b;
    plan.seg_b = plan.runs.front().seg_b;
    plan.runs.clear();
  }
  return plan;
}

std::map<size_t, std::vector<PlaneRun>> record_layouts_by_payload(
    const std::vector<std::vector<PlaneRun>>& object_groups) {
  std::map<size_t, std::vector<PlaneRun>> layouts;
  std::vector<size_t> ambiguous;
  for (const std::vector<PlaneRun>& runs : object_groups) {
    require_valid_runs(runs);
    const size_t payload = layered_payload_bytes(runs);
    const auto found = layouts.find(payload);
    if (found == layouts.end()) {
      layouts.emplace(payload, runs);
    } else if (found->second != runs) {
      ambiguous.push_back(payload);
    }
  }
  for (const size_t payload : ambiguous) {
    layouts.erase(payload);
  }
  return layouts;
}

ShardPlan choose_shard_plan(
    size_t payload_bytes,
    const std::map<size_t, std::vector<PlaneRun>>& layouts,
    size_t target_segment_bytes, size_t max_record_bytes,
    size_t single_record_threshold_bytes, size_t plane_bytes) {
  const auto found = layouts.find(payload_bytes);
  if (found != layouts.end()) {
    return make_layered_shard_plan(found->second, max_record_bytes);
  }
  return make_shard_plan(payload_bytes, target_segment_bytes, max_record_bytes,
                         single_record_threshold_bytes, plane_bytes);
}

std::string encode_shard_runs(const std::vector<ShardRun>& runs) {
  std::ostringstream out;
  for (size_t i = 0; i < runs.size(); ++i) {
    if (i != 0) {
      out << ',';
    }
    out << runs[i].plane_b << ':' << runs[i].seg_b << ':' << runs[i].planes;
  }
  return out.str();
}

std::vector<ShardRun> decode_shard_runs(const std::string& encoded) {
  if (encoded.empty()) {
    throw std::invalid_argument("shard runs: empty");
  }
  std::vector<ShardRun> runs;
  size_t start = 0;
  while (start <= encoded.size()) {
    size_t end = encoded.find(',', start);
    if (end == std::string::npos) {
      end = encoded.size();
    }
    const std::string item = encoded.substr(start, end - start);
    const size_t first = item.find(':');
    const size_t second = first == std::string::npos
                              ? std::string::npos
                              : item.find(':', first + 1);
    if (second == std::string::npos ||
        item.find(':', second + 1) != std::string::npos) {
      throw std::invalid_argument("shard runs: malformed run '" + item + "'");
    }
    ShardRun run;
    run.plane_b = parse_positive(item.substr(0, first));
    run.seg_b = parse_positive(item.substr(first + 1, second - first - 1));
    const uint64_t planes = parse_positive(item.substr(second + 1));
    if (planes > std::numeric_limits<uint32_t>::max() ||
        run.seg_b > run.plane_b) {
      throw std::invalid_argument("shard runs: implausible run '" + item + "'");
    }
    run.planes = static_cast<uint32_t>(planes);
    runs.push_back(run);
    start = end + 1;
  }
  return runs;
}

void require_consistent_runs(const ShardPlan& plan, size_t total_bytes) {
  if (plan.runs.empty()) {
    return;
  }
  size_t bytes = 0;
  size_t records = 0;
  for (const ShardRun& run : plan.runs) {
    if (run.plane_b == 0 || run.seg_b == 0 || run.planes == 0 ||
        run.seg_b > run.plane_b) {
      throw std::invalid_argument("shard runs: implausible run");
    }
    bytes += run.plane_b * run.planes;
    records += ceil_div(run.plane_b, run.seg_b) * run.planes;
  }
  if (bytes != total_bytes) {
    throw std::invalid_argument("shard runs cover " + std::to_string(bytes) +
                                " bytes, the object is " +
                                std::to_string(total_bytes));
  }
  if (records != plan.nseg) {
    throw std::invalid_argument("shard runs imply " + std::to_string(records) +
                                " records, the plan has " +
                                std::to_string(plan.nseg));
  }
}

ShardRange segment_range(const ShardPlan& plan, uint32_t index,
                         size_t total_bytes) {
  if (index >= plan.nseg) {
    throw std::out_of_range("segment_range: index beyond plan");
  }

  if (!plan.runs.empty()) {
    // Walk the runs in payload order; within a run the uniform rule applies,
    // so a plane that is not a multiple of seg_b ends in a short record.
    size_t base = 0;
    size_t remaining = index;
    for (const ShardRun& run : plan.runs) {
      const size_t pieces = ceil_div(run.plane_b, run.seg_b);
      const size_t records = pieces * run.planes;
      if (remaining < records) {
        const size_t piece_start = (remaining % pieces) * run.seg_b;
        const size_t offset =
            base + ((remaining / pieces) * run.plane_b) + piece_start;
        if (offset >= total_bytes) {
          return {total_bytes, 0};
        }
        const size_t length = std::min(run.seg_b, run.plane_b - piece_start);
        return {offset, std::min(length, total_bytes - offset)};
      }
      remaining -= records;
      base += run.plane_b * run.planes;
    }
    throw std::out_of_range("segment_range: runs describe fewer records");
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
