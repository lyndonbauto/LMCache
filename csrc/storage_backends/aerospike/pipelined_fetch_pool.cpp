// SPDX-License-Identifier: Apache-2.0

#include "pipelined_fetch_pool.h"

#include "layer_pipeline.h"

#include <algorithm>
#include <map>
#include <stdexcept>

namespace lmcache {
namespace connector {
namespace rdma {

PipelinedFetchPool::PipelinedFetchPool(
    const SlotPlanner& planner, const NodeRegistry& registry,
    const std::string& namespace_name, size_t max_record_bytes,
    size_t max_write_bytes, size_t window_bytes, uint32_t window_count,
    uint32_t notification_depth)
    : window_bytes_(window_bytes),
      max_slots_per_request_(
          window_count == 0 ? 0 : notification_depth / window_count) {
  if (window_count == 0 || window_count > kMaxFetchWindows) {
    throw std::invalid_argument(
        "PipelinedFetchPool: window_count must be 1 to " +
        std::to_string(kMaxFetchWindows) + ", got " +
        std::to_string(window_count));
  }
  if (window_bytes == 0) {
    throw std::invalid_argument("PipelinedFetchPool: window_bytes is 0");
  }
  if (max_slots_per_request_ == 0) {
    throw std::invalid_argument(
        "PipelinedFetchPool: a notification depth of " +
        std::to_string(notification_depth) + " cannot give each of " +
        std::to_string(window_count) + " windows a receive slot");
  }
  sessions_.reserve(window_count);
  for (uint32_t w = 0; w < window_count; ++w) {
    auto session = std::make_unique<PipelinedFetchSession>(
        planner, registry, namespace_name, max_record_bytes, max_write_bytes,
        window_bytes, max_slots_per_request_, window_count);
    session->set_generation_class(static_cast<uint16_t>(w + 1),
                                  static_cast<uint16_t>(window_count));
    sessions_.push_back(std::move(session));
  }
}

uint32_t PipelinedFetchPool::window_of_generation(uint16_t generation) const {
  return (static_cast<uint32_t>(generation) + window_count() - 1) %
         window_count();
}

bool PipelinedFetchPool::has_active_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  return std::any_of(sessions_.begin(), sessions_.end(),
                     [](const std::unique_ptr<PipelinedFetchSession>& s) {
                       return s->has_active_request();
                     });
}

bool PipelinedFetchPool::is_active(uint16_t generation) const {
  std::lock_guard<std::mutex> lock(mu_);
  return active_session(generation) != nullptr;
}

uint16_t PipelinedFetchPool::begin_request(
    const std::vector<ChunkPlacement>& placements,
    const std::vector<ChunkNodeBinding>& chunk_nodes,
    const std::vector<SlotDigest>& slot_digests) {
  if (placements.empty()) {
    throw std::invalid_argument(
        "PipelinedFetchPool: a request needs at least one placement");
  }
  const size_t lowest =
      std::min_element(placements.begin(), placements.end(),
                       [](const ChunkPlacement& a, const ChunkPlacement& b) {
                         return a.dest_offset < b.dest_offset;
                       })
          ->dest_offset;
  std::lock_guard<std::mutex> lock(mu_);
  return idle_session(lowest / window_bytes_)
      .begin_request(placements, chunk_nodes, slot_digests);
}

uint16_t PipelinedFetchPool::begin_request_from_slots(
    const std::vector<PlannedSlot>& slots) {
  if (slots.empty()) {
    throw std::invalid_argument(
        "PipelinedFetchPool: a planned request needs at least one slot");
  }
  std::lock_guard<std::mutex> lock(mu_);
  return idle_session(slots.front().offset / window_bytes_)
      .begin_request_from_slots(slots);
}

std::vector<std::pair<std::string, std::string>>
PipelinedFetchPool::pipelined_fetch_commands(uint16_t generation) const {
  std::lock_guard<std::mutex> lock(mu_);
  PipelinedFetchSession* session = active_session(generation);
  return session == nullptr ? std::vector<std::pair<std::string, std::string>>{}
                            : session->pipelined_fetch_commands();
}

void PipelinedFetchPool::on_node_reply(const std::string& node_name,
                                       const std::string& command,
                                       const std::string& reply,
                                       uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  PipelinedFetchSession* session = active_session(generation);
  if (session != nullptr) {
    session->on_node_reply(node_name, command, reply, generation);
  }
}

void PipelinedFetchPool::on_notifications(
    const std::vector<uint32_t>& immediates) {
  std::map<uint32_t, std::vector<uint32_t>> by_window;
  for (const uint32_t immediate : immediates) {
    const uint16_t generation = immediate_generation(immediate);
    if (generation == kNoGeneration) {
      continue;
    }
    by_window[window_of_generation(generation)].push_back(immediate);
  }
  std::lock_guard<std::mutex> lock(mu_);
  for (const auto& entry : by_window) {
    sessions_[entry.first]->on_notifications(entry.second);
  }
}

bool PipelinedFetchPool::is_layer_ready(uint32_t layer_id,
                                        uint16_t generation) const {
  std::lock_guard<std::mutex> lock(mu_);
  PipelinedFetchSession* session = active_session(generation);
  return session != nullptr && session->is_layer_ready(layer_id, generation);
}

std::vector<uint32_t> PipelinedFetchPool::unservable_layers(
    uint16_t generation) const {
  std::lock_guard<std::mutex> lock(mu_);
  PipelinedFetchSession* session = active_session(generation);
  return session == nullptr ? std::vector<uint32_t>{}
                            : session->unservable_layers();
}

void PipelinedFetchPool::finish_request(uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  PipelinedFetchSession* session = active_session(generation);
  if (session == nullptr) {
    throw std::runtime_error("PipelinedFetchPool: generation " +
                             std::to_string(generation) +
                             " is not an active fetch");
  }
  session->finish_request();
}

void PipelinedFetchPool::abandon_request(uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  PipelinedFetchSession* session = active_session(generation);
  if (session != nullptr) {
    session->abandon_request();
  }
}

std::vector<uint16_t> PipelinedFetchPool::generation_counters() const {
  std::lock_guard<std::mutex> lock(mu_);
  std::vector<uint16_t> counters;
  counters.reserve(sessions_.size());
  for (const auto& session : sessions_) {
    counters.push_back(session->next_generation());
  }
  return counters;
}

void PipelinedFetchPool::restore_generation_counters(
    const std::vector<uint16_t>& counters) {
  std::lock_guard<std::mutex> lock(mu_);
  if (counters.size() != sessions_.size()) {
    throw std::invalid_argument(
        "PipelinedFetchPool: " + std::to_string(counters.size()) +
        " generation counters for " + std::to_string(sessions_.size()) +
        " windows");
  }
  for (size_t w = 0; w < sessions_.size(); ++w) {
    sessions_[w]->restore_generation_counter(counters[w]);
  }
}

PipelinedFetchSession& PipelinedFetchPool::idle_session(size_t window) {
  if (window >= sessions_.size()) {
    throw std::invalid_argument(
        "PipelinedFetchPool: the first slot is in window " +
        std::to_string(window) + ", past the " +
        std::to_string(sessions_.size()) + " registered windows");
  }
  PipelinedFetchSession& session = *sessions_[window];
  if (session.has_active_request()) {
    throw std::runtime_error("PipelinedFetchPool: window " +
                             std::to_string(window) +
                             " already has an active fetch");
  }
  return session;
}

PipelinedFetchSession* PipelinedFetchPool::active_session(
    uint16_t generation) const {
  if (generation == kNoGeneration) {
    return nullptr;
  }
  PipelinedFetchSession& session = *sessions_[window_of_generation(generation)];
  if (!session.has_active_request() ||
      session.active_generation() != generation) {
    return nullptr;
  }
  return &session;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
