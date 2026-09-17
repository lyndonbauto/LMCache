// SPDX-License-Identifier: Apache-2.0

#include "pipelined_fetch_session.h"

#include <algorithm>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

using lmcache::connector::plane_segment_bytes;

void require_active_request(bool has_request) {
  if (!has_request) {
    throw std::runtime_error(
        "PipelinedFetchSession: no active pipelined fetch request");
  }
}

std::map<uint32_t, std::string> chunk_node_map(
    const std::vector<ChunkNodeBinding>& chunk_nodes) {
  std::map<uint32_t, std::string> out;
  for (const ChunkNodeBinding& binding : chunk_nodes) {
    if (binding.node_name.empty()) {
      throw std::invalid_argument("PipelinedFetchSession: chunk " +
                                  std::to_string(binding.chunk_id) +
                                  " has no owning node");
    }
    const auto inserted =
        out.emplace(binding.chunk_id, binding.node_name).second;
    if (!inserted) {
      throw std::invalid_argument("PipelinedFetchSession: chunk " +
                                  std::to_string(binding.chunk_id) +
                                  " is bound to multiple nodes");
    }
  }
  return out;
}

struct SlotDigestKey {
  uint32_t chunk_id = 0;
  uint32_t layer_id = 0;
  uint32_t plane = 0;
  uint32_t piece = 0;

  bool operator<(const SlotDigestKey& other) const {
    if (chunk_id != other.chunk_id) {
      return chunk_id < other.chunk_id;
    }
    if (layer_id != other.layer_id) {
      return layer_id < other.layer_id;
    }
    if (plane != other.plane) {
      return plane < other.plane;
    }
    return piece < other.piece;
  }
};

std::vector<SlotDigestKey> digest_keys_for_plan(
    const SlotPlanner& planner, const RequestPlan& plan,
    const std::vector<ChunkPlacement>& placements, size_t max_record_bytes,
    size_t max_write_bytes) {
  std::vector<SlotDigestKey> keys;
  keys.reserve(plan.slot_count());

  std::map<uint32_t, std::vector<const ChunkPlacement*>> by_group;
  for (const ChunkPlacement& placement : placements) {
    by_group[placement.object_group_id].push_back(&placement);
  }

  for (const uint32_t layer_id : planner.layer_ids()) {
    const uint32_t group_id = planner.object_group_of_layer(layer_id);
    const auto group_placements = by_group.find(group_id);
    if (group_placements == by_group.end()) {
      continue;
    }
    const std::vector<ByteRange> planes = planner.layer_plane_ranges(layer_id);
    for (const ChunkPlacement* placement : group_placements->second) {
      for (uint32_t plane_index = 0; plane_index < planes.size();
           ++plane_index) {
        const ByteRange& plane = planes[plane_index];
        const size_t record_bytes =
            plane_segment_bytes(plane.length, max_record_bytes);
        if (record_bytes > max_write_bytes) {
          throw std::invalid_argument(
              "PipelinedFetchSession: record size exceeds max RDMA write");
        }
        const size_t pieces = (plane.length + record_bytes - 1) / record_bytes;
        for (size_t piece = 0; piece < pieces; ++piece) {
          SlotDigestKey key;
          key.chunk_id = placement->chunk_id;
          key.layer_id = layer_id;
          key.plane = plane_index;
          key.piece = static_cast<uint32_t>(piece);
          keys.push_back(key);
        }
      }
    }
  }

  if (keys.size() != plan.slot_count()) {
    throw std::runtime_error(
        "PipelinedFetchSession: internal slot/digest key count mismatch");
  }
  return keys;
}

std::vector<std::string> digests_for_plan(
    const RequestPlan& plan, const SlotPlanner& planner,
    const std::vector<ChunkPlacement>& placements, size_t max_record_bytes,
    size_t max_write_bytes, const std::vector<SlotDigest>& slot_digests) {
  const std::vector<SlotDigestKey> keys = digest_keys_for_plan(
      planner, plan, placements, max_record_bytes, max_write_bytes);

  std::map<SlotDigestKey, std::string> digest_by_key;
  for (const SlotDigest& entry : slot_digests) {
    if (entry.digest_hex.empty()) {
      throw std::invalid_argument(
          "PipelinedFetchSession: slot digest is empty for chunk " +
          std::to_string(entry.chunk_id) + " layer " +
          std::to_string(entry.layer_id));
    }
    SlotDigestKey key{entry.chunk_id, entry.layer_id, entry.plane, entry.piece};
    const auto inserted = digest_by_key.emplace(key, entry.digest_hex).second;
    if (!inserted) {
      throw std::invalid_argument(
          "PipelinedFetchSession: duplicate digest key for chunk " +
          std::to_string(entry.chunk_id));
    }
  }

  std::vector<std::string> out(plan.slot_count());
  for (size_t i = 0; i < keys.size(); ++i) {
    const auto found = digest_by_key.find(keys[i]);
    if (found == digest_by_key.end()) {
      throw std::invalid_argument(
          "PipelinedFetchSession: no digest for chunk " +
          std::to_string(keys[i].chunk_id) + " layer " +
          std::to_string(keys[i].layer_id) + " plane " +
          std::to_string(keys[i].plane) + " piece " +
          std::to_string(keys[i].piece));
    }
    out[i] = found->second;
  }
  return out;
}

void validate_slots_in_window(const RequestPlan& plan, size_t window_bytes) {
  for (size_t i = 0; i < plan.slot_count(); ++i) {
    const FetchSlot& slot = plan.slot(static_cast<uint16_t>(i));
    const size_t end = slot.offset + slot.length;
    if (end > window_bytes || slot.length == 0) {
      std::ostringstream os;
      os << "PipelinedFetchSession: slot " << i << " write [" << slot.offset
         << ", " << end << ") falls outside the " << window_bytes
         << "-byte registered window";
      throw std::invalid_argument(os.str());
    }
  }
}

std::map<std::string, std::set<uint16_t>> slots_owned_by_node(
    const RequestPlan& plan,
    const std::map<uint32_t, std::string>& chunk_to_node) {
  std::map<std::string, std::set<uint16_t>> out;
  for (uint32_t chunk_id : plan.chunk_ids()) {
    const auto node_it = chunk_to_node.find(chunk_id);
    if (node_it == chunk_to_node.end()) {
      continue;
    }
    for (const uint16_t slot_index : plan.slots_for_chunk(chunk_id)) {
      out[node_it->second].insert(slot_index);
    }
  }
  return out;
}

}  // namespace

PipelinedFetchSession::PipelinedFetchSession(
    const SlotPlanner& planner, const NodeRegistry& registry,
    std::string namespace_name, size_t max_record_bytes, size_t max_write_bytes,
    size_t window_bytes, uint32_t max_notification_slots)
    : planner_(planner),
      registry_(registry),
      namespace_name_(std::move(namespace_name)),
      max_record_bytes_(max_record_bytes),
      max_write_bytes_(max_write_bytes),
      window_bytes_(window_bytes),
      max_notification_slots_(max_notification_slots),
      plan_(0),
      readiness_(plan_) {}

bool PipelinedFetchSession::has_active_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  return has_request_;
}

uint16_t PipelinedFetchSession::active_generation() const {
  std::lock_guard<std::mutex> lock(mu_);
  require_active_request(has_request_);
  return active_generation_;
}

uint16_t PipelinedFetchSession::allocate_generation() {
  const uint16_t generation = next_generation_;
  next_generation_ = static_cast<uint16_t>(next_generation_ + 1);
  return generation;
}

uint16_t PipelinedFetchSession::begin_request(
    const std::vector<ChunkPlacement>& placements,
    const std::vector<ChunkNodeBinding>& chunk_nodes,
    const std::vector<SlotDigest>& slot_digests) {
  std::lock_guard<std::mutex> lock(mu_);
  if (has_request_) {
    throw std::runtime_error(
        "PipelinedFetchSession: a pipelined fetch is already active");
  }

  const uint16_t generation = allocate_generation();
  RequestPlan plan = planner_.plan_request(placements, max_record_bytes_,
                                           max_write_bytes_, generation);
  if (plan.slot_count() > max_notification_slots_) {
    throw std::runtime_error("PipelinedFetchSession: plan requires " +
                             std::to_string(plan.slot_count()) +
                             " notification slots but the "
                             "queue pair was built for at most " +
                             std::to_string(max_notification_slots_));
  }
  validate_slots_in_window(plan, window_bytes_);

  std::map<uint32_t, std::string> nodes = chunk_node_map(chunk_nodes);
  for (uint32_t chunk_id : plan.chunk_ids()) {
    if (nodes.find(chunk_id) == nodes.end()) {
      throw std::invalid_argument(
          "PipelinedFetchSession: chunk " + std::to_string(chunk_id) +
          " appears in the plan but has no node binding");
    }
  }

  std::vector<std::string> digests =
      digests_for_plan(plan, planner_, placements, max_record_bytes_,
                       max_write_bytes_, slot_digests);

  has_request_ = true;
  active_generation_ = generation;
  plan_ = std::move(plan);
  readiness_ = LayerReadiness(plan_);
  chunk_to_node_ = std::move(nodes);
  slots_by_node_ = slots_owned_by_node(plan_, chunk_to_node_);
  digest_per_slot_ = std::move(digests);
  return active_generation_;
}

std::map<std::string, std::string>
PipelinedFetchSession::pipelined_fetch_commands() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_) {
    return {};
  }

  std::map<std::string, std::vector<SinkRequest>> sinks_by_node;
  for (uint32_t chunk_id : plan_.chunk_ids()) {
    const std::string& node_name = chunk_to_node_.at(chunk_id);
    for (const uint16_t slot_index : plan_.slots_for_chunk(chunk_id)) {
      const FetchSlot& slot = plan_.slot(slot_index);
      SinkRequest sink;
      sink.digest_hex = digest_per_slot_[slot_index];
      sink.offset = slot.offset;
      sink.length = slot.length;
      sink.slot = slot_index;
      sinks_by_node[node_name].push_back(sink);
    }
  }

  for (auto& entry : sinks_by_node) {
    std::sort(entry.second.begin(), entry.second.end(),
              [](const SinkRequest& left, const SinkRequest& right) {
                return left.slot < right.slot;
              });
  }

  std::map<std::string, std::string> commands;
  for (const auto& entry : sinks_by_node) {
    const uint64_t region = registry_.region_for(entry.first);
    commands[entry.first] = build_pipelined_fetch_command(
        namespace_name_, region, active_generation_, entry.second);
  }
  return commands;
}

void PipelinedFetchSession::on_node_reply(const std::string& node_name,
                                          const std::string& reply,
                                          uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_ || generation != active_generation_) {
    return;
  }

  const PipelinedFetchReply parsed = parse_pipelined_fetch_reply(reply);
  const size_t accounted =
      static_cast<size_t>(parsed.accepted) + parsed.failed_slots.size();
  if (accounted != parsed.requested) {
    throw std::runtime_error(
        "PipelinedFetchSession: node '" + node_name + "' reply accounts for " +
        std::to_string(accounted) + " slots but requested " +
        std::to_string(parsed.requested));
  }

  const auto owned = slots_by_node_.find(node_name);
  const std::set<uint16_t>* owned_slots =
      owned == slots_by_node_.end() ? nullptr : &owned->second;

  for (const uint16_t failed_slot : parsed.failed_slots) {
    if (owned_slots == nullptr ||
        owned_slots->find(failed_slot) == owned_slots->end()) {
      throw std::runtime_error(
          "PipelinedFetchSession: node '" + node_name + "' declined slot " +
          std::to_string(failed_slot) + " which it does not own");
    }
    readiness_.note_unservable(failed_slot);
  }
}

void PipelinedFetchSession::on_notifications(
    const std::vector<uint32_t>& immediates) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_) {
    return;
  }
  for (const uint32_t immediate : immediates) {
    readiness_.note_arrival(immediate);
  }
}

bool PipelinedFetchSession::is_layer_ready(uint32_t layer_id) const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_) {
    return false;
  }
  return readiness_.is_layer_ready(layer_id);
}

std::vector<uint32_t> PipelinedFetchSession::unservable_layers() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_) {
    return {};
  }
  return readiness_.unservable_layers();
}

std::vector<uint32_t> PipelinedFetchSession::ready_layers() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_) {
    return {};
  }
  return readiness_.ready_layers();
}

void PipelinedFetchSession::finish_request() {
  std::lock_guard<std::mutex> lock(mu_);
  require_active_request(has_request_);
  has_request_ = false;
  active_generation_ = 0;
  plan_ = RequestPlan(0);
  readiness_ = LayerReadiness(plan_);
  chunk_to_node_.clear();
  slots_by_node_.clear();
  digest_per_slot_.clear();
}

void PipelinedFetchSession::abandon_request() {
  std::lock_guard<std::mutex> lock(mu_);
  require_active_request(has_request_);
  last_abandoned_generation_ = active_generation_;
  has_request_ = false;
  active_generation_ = 0;
  plan_ = RequestPlan(0);
  readiness_ = LayerReadiness(plan_);
  chunk_to_node_.clear();
  slots_by_node_.clear();
  digest_per_slot_.clear();
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
