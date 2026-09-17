// SPDX-License-Identifier: Apache-2.0

#include "pipelined_fetch_session.h"

#include <stdexcept>
#include <utility>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

void require_active_request(bool has_request, bool abandoned) {
  if (!has_request || abandoned) {
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

std::vector<std::string> digests_for_plan(
    const RequestPlan& plan, const std::vector<SlotDigest>& slot_digests) {
  if (slot_digests.size() != plan.slot_count()) {
    throw std::invalid_argument(
        "PipelinedFetchSession: expected " + std::to_string(plan.slot_count()) +
        " slot digests, got " + std::to_string(slot_digests.size()));
  }
  std::vector<std::string> out(plan.slot_count());
  for (size_t i = 0; i < slot_digests.size(); ++i) {
    const SlotDigest& entry = slot_digests[i];
    if (entry.slot_index != static_cast<uint16_t>(i)) {
      throw std::invalid_argument("PipelinedFetchSession: slot_digests[" +
                                  std::to_string(i) + "] has index " +
                                  std::to_string(entry.slot_index) +
                                  " but must be " + std::to_string(i));
    }
    if (entry.digest_hex.empty()) {
      throw std::invalid_argument("PipelinedFetchSession: slot " +
                                  std::to_string(i) + " has an empty digest");
    }
    out[i] = entry.digest_hex;
  }
  return out;
}

}  // namespace

PipelinedFetchSession::PipelinedFetchSession(const SlotPlanner& planner,
                                             const NodeRegistry& registry,
                                             std::string namespace_name,
                                             size_t max_record_bytes,
                                             size_t max_write_bytes)
    : planner_(planner),
      registry_(registry),
      namespace_name_(std::move(namespace_name)),
      max_record_bytes_(max_record_bytes),
      max_write_bytes_(max_write_bytes),
      plan_(0),
      readiness_(plan_) {}

bool PipelinedFetchSession::has_active_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  return has_request_ && !abandoned_;
}

uint16_t PipelinedFetchSession::active_generation() const {
  std::lock_guard<std::mutex> lock(mu_);
  require_active_request(has_request_, abandoned_);
  return active_generation_;
}

uint16_t PipelinedFetchSession::allocate_generation() {
  // Monotonic per session. After 65536 requests the counter wraps to zero.
  // At most one request is active at a time, so a straggler immediate from an
  // abandoned or finished request is rejected by LayerReadiness when its
  // generation does not match the new plan. The pathological case -- wrap to a
  // generation still receiving writes from a previous lease of the same window
  // -- is the same best-effort hazard documented in layer_pipeline.h: wrong
  // slot indices become kUnknownSlot, and matching indices require the caller
  // to have abandoned the prior request before reusing the window.
  const uint16_t generation = next_generation_;
  next_generation_ = static_cast<uint16_t>(next_generation_ + 1);
  return generation;
}

uint16_t PipelinedFetchSession::begin_request(
    const std::vector<ChunkPlacement>& placements,
    const std::vector<ChunkNodeBinding>& chunk_nodes,
    const std::vector<SlotDigest>& slot_digests) {
  std::lock_guard<std::mutex> lock(mu_);
  if (has_request_ && !abandoned_) {
    throw std::runtime_error(
        "PipelinedFetchSession: a pipelined fetch is already active");
  }

  const uint16_t generation = allocate_generation();
  RequestPlan plan = planner_.plan_request(placements, max_record_bytes_,
                                           max_write_bytes_, generation);
  std::map<uint32_t, std::string> nodes = chunk_node_map(chunk_nodes);
  for (uint32_t chunk_id : plan.chunk_ids()) {
    if (nodes.find(chunk_id) == nodes.end()) {
      throw std::invalid_argument(
          "PipelinedFetchSession: chunk " + std::to_string(chunk_id) +
          " appears in the plan but has no node binding");
    }
  }

  std::vector<std::string> digests = digests_for_plan(plan, slot_digests);

  has_request_ = true;
  abandoned_ = false;
  active_generation_ = generation;
  plan_ = std::move(plan);
  readiness_ = LayerReadiness(plan_);
  chunk_to_node_ = std::move(nodes);
  digest_per_slot_ = std::move(digests);
  return active_generation_;
}

std::map<std::string, std::string>
PipelinedFetchSession::pipelined_fetch_commands() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_ || abandoned_) {
    return {};
  }

  std::map<std::string, std::vector<SinkRequest>> sinks_by_node;
  for (uint32_t chunk_id : plan_.chunk_ids()) {
    const auto node_it = chunk_to_node_.find(chunk_id);
    if (node_it == chunk_to_node_.end()) {
      throw std::runtime_error("PipelinedFetchSession: chunk " +
                               std::to_string(chunk_id) +
                               " has no owning node");
    }
    const std::string& node_name = node_it->second;
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

  std::map<std::string, std::string> commands;
  for (const auto& entry : sinks_by_node) {
    const uint64_t region = registry_.region_for(entry.first);
    commands[entry.first] = build_pipelined_fetch_command(
        namespace_name_, region, active_generation_, entry.second);
  }
  return commands;
}

void PipelinedFetchSession::on_node_reply(const std::string& /*node_name*/,
                                          const std::string& reply) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_ || abandoned_) {
    return;
  }

  const PipelinedFetchReply parsed = parse_pipelined_fetch_reply(reply);
  for (const uint16_t failed_slot : parsed.failed_slots) {
    readiness_.note_unservable(failed_slot);
  }
}

void PipelinedFetchSession::on_notifications(
    const std::vector<uint32_t>& immediates) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_ || abandoned_) {
    return;
  }
  for (const uint32_t immediate : immediates) {
    readiness_.note_arrival(immediate);
  }
}

bool PipelinedFetchSession::is_layer_ready(uint32_t layer_id) const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_ || abandoned_) {
    return false;
  }
  return readiness_.is_layer_ready(layer_id);
}

std::vector<uint32_t> PipelinedFetchSession::unservable_layers() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_ || abandoned_) {
    return {};
  }
  return readiness_.unservable_layers();
}

std::vector<uint32_t> PipelinedFetchSession::ready_layers() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!has_request_ || abandoned_) {
    return {};
  }
  return readiness_.ready_layers();
}

void PipelinedFetchSession::finish_request() {
  std::lock_guard<std::mutex> lock(mu_);
  require_active_request(has_request_, abandoned_);
  has_request_ = false;
  abandoned_ = false;
  active_generation_ = 0;
  plan_ = RequestPlan(0);
  readiness_ = LayerReadiness(plan_);
  chunk_to_node_.clear();
  digest_per_slot_.clear();
}

void PipelinedFetchSession::abandon_request() {
  std::lock_guard<std::mutex> lock(mu_);
  require_active_request(has_request_, abandoned_);
  abandoned_ = true;
  has_request_ = false;
  active_generation_ = 0;
  plan_ = RequestPlan(0);
  readiness_ = LayerReadiness(plan_);
  chunk_to_node_.clear();
  digest_per_slot_.clear();
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
