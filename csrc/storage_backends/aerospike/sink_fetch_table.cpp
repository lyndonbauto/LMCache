// SPDX-License-Identifier: Apache-2.0

#include "sink_fetch_table.h"

#include <utility>

namespace lmcache {
namespace connector {
namespace sink {

SinkFetchTable::SinkFetchTable(size_t window_bytes, uint32_t window_count,
                               uint32_t max_slots_per_request)
    : window_bytes_(window_bytes),
      window_count_(window_count),
      max_slots_(max_slots_per_request),
      windows_(window_count) {
  if (window_bytes == 0 || window_count == 0) {
    throw std::invalid_argument(
        "sink fetch: window_bytes and window_count must be non-zero");
  }
  if (max_slots_per_request == 0 ||
      max_slots_per_request > kMaxSlotsPerRequest) {
    throw std::invalid_argument(
        "sink fetch: max_slots_per_request must be in [1, " +
        std::to_string(kMaxSlotsPerRequest) + "], got " +
        std::to_string(max_slots_per_request));
  }
}

BegunFetch SinkFetchTable::begin(const std::vector<SinkSlot>& slots) {
  if (slots.empty()) {
    throw std::invalid_argument("sink fetch: a plan needs at least one slot");
  }
  if (slots.size() > max_slots_) {
    throw PlanTooLargeError(
        "sink fetch: plan has " + std::to_string(slots.size()) +
        " slots but one fetch carries at most " + std::to_string(max_slots_));
  }

  const size_t window = slots.front().offset / window_bytes_;
  if (window >= window_count_) {
    throw std::invalid_argument(
        "sink fetch: slot 0 at offset " + std::to_string(slots.front().offset) +
        " is past the last of " + std::to_string(window_count_) + " windows");
  }
  const size_t window_begin = window * window_bytes_;
  const size_t window_end = window_begin + window_bytes_;

  Fetch fetch;
  fetch.slots.assign(slots.size(), SlotState::kPending);
  fetch.slot_layer.reserve(slots.size());

  // Layers in the order they first appear, with their slots in plan order.
  std::vector<uint32_t> layer_order;
  std::map<uint32_t, std::vector<uint32_t>> slots_per_layer;
  for (size_t i = 0; i < slots.size(); ++i) {
    const SinkSlot& slot = slots[i];
    if (slot.record_key.empty()) {
      throw std::invalid_argument("sink fetch: slot " + std::to_string(i) +
                                  " has an empty record key");
    }
    if (slot.length == 0) {
      throw std::invalid_argument("sink fetch: slot " + std::to_string(i) +
                                  " has length 0");
    }
    if (slot.offset < window_begin || slot.offset > window_end ||
        slot.length > window_end - slot.offset) {
      throw std::invalid_argument("sink fetch: slot " + std::to_string(i) +
                                  " [" + std::to_string(slot.offset) + ", +" +
                                  std::to_string(slot.length) +
                                  ") leaves window " + std::to_string(window) +
                                  " [" + std::to_string(window_begin) + ", " +
                                  std::to_string(window_end) + ")");
    }
    auto [it, inserted] = slots_per_layer.try_emplace(slot.layer_id);
    if (inserted) {
      layer_order.push_back(slot.layer_id);
    }
    it->second.push_back(static_cast<uint32_t>(i));
    fetch.slot_layer.push_back(slot.layer_id);
    ++fetch.expected_per_layer[slot.layer_id];
  }

  std::lock_guard<std::mutex> lock(mu_);
  if (windows_[window].generation != kNoGeneration) {
    throw std::runtime_error("sink fetch: window " + std::to_string(window) +
                             " already has fetch generation " +
                             std::to_string(windows_[window].generation));
  }

  BegunFetch begun;
  begun.generation = next_generation();
  fetch.generation = begun.generation;
  fetch.first_token = next_token_;
  begun.batches.reserve(layer_order.size());
  for (uint32_t layer_id : layer_order) {
    LayerBatch batch;
    batch.generation = begun.generation;
    batch.token = next_token_++;
    batch.layer_id = layer_id;
    // Tokens rise across fetches, so every layer of an older fetch is placed
    // before any layer of a newer one; truncation wraps after 2^32 batches.
    batch.priority = static_cast<uint32_t>(batch.token);
    batch.slot_indices = std::move(slots_per_layer[layer_id]);
    begun.batches.push_back(std::move(batch));
  }
  fetch.last_token = next_token_ - 1;
  windows_[window] = std::move(fetch);
  return begun;
}

void SinkFetchTable::on_slot_result(uint16_t generation, uint32_t slot_index,
                                    bool landed, uint64_t token) {
  std::lock_guard<std::mutex> lock(mu_);
  Fetch* fetch = find(generation);
  if (fetch == nullptr || slot_index >= fetch->slots.size()) {
    return;
  }
  if (token != 0 && (token < fetch->first_token || token > fetch->last_token)) {
    return;
  }
  SlotState& state = fetch->slots[slot_index];
  if (state != SlotState::kPending) {
    return;
  }
  const uint32_t layer_id = fetch->slot_layer[slot_index];
  if (landed) {
    state = SlotState::kLanded;
    ++fetch->landed_per_layer[layer_id];
  } else {
    state = SlotState::kFailed;
    ++fetch->failed_per_layer[layer_id];
  }
}

bool SinkFetchTable::is_active(uint16_t generation, uint64_t token) const {
  std::lock_guard<std::mutex> lock(mu_);
  const Fetch* fetch = find(generation);
  if (fetch == nullptr) {
    return false;
  }
  return token == 0 ||
         (token >= fetch->first_token && token <= fetch->last_token);
}

bool SinkFetchTable::has_active_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  for (const Fetch& fetch : windows_) {
    if (fetch.generation != kNoGeneration) {
      return true;
    }
  }
  return false;
}

bool SinkFetchTable::is_layer_ready(uint32_t layer_id,
                                    uint16_t generation) const {
  std::lock_guard<std::mutex> lock(mu_);
  const Fetch* fetch = find(generation);
  if (fetch == nullptr) {
    return false;
  }
  const auto expected = fetch->expected_per_layer.find(layer_id);
  if (expected == fetch->expected_per_layer.end()) {
    return false;
  }
  const auto landed = fetch->landed_per_layer.find(layer_id);
  return landed != fetch->landed_per_layer.end() &&
         landed->second == expected->second;
}

std::vector<uint32_t> SinkFetchTable::unservable_layers(
    uint16_t generation) const {
  std::lock_guard<std::mutex> lock(mu_);
  std::vector<uint32_t> layers;
  const Fetch* fetch = find(generation);
  if (fetch == nullptr) {
    return layers;
  }
  layers.reserve(fetch->failed_per_layer.size());
  for (const auto& [layer_id, count] : fetch->failed_per_layer) {
    layers.push_back(layer_id);
  }
  return layers;
}

void SinkFetchTable::finish(uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  Fetch* fetch = find(generation);
  if (fetch == nullptr) {
    throw std::runtime_error("sink fetch: generation " +
                             std::to_string(generation) + " is not active");
  }
  *fetch = Fetch();
}

void SinkFetchTable::abandon(uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  Fetch* fetch = find(generation);
  if (fetch != nullptr) {
    *fetch = Fetch();
  }
}

SinkFetchTable::Fetch* SinkFetchTable::find(uint16_t generation) {
  if (generation == kNoGeneration) {
    return nullptr;
  }
  for (Fetch& fetch : windows_) {
    if (fetch.generation == generation) {
      return &fetch;
    }
  }
  return nullptr;
}

const SinkFetchTable::Fetch* SinkFetchTable::find(uint16_t generation) const {
  return const_cast<SinkFetchTable*>(this)->find(generation);
}

// Skips 0 and every generation still active in another window, so two live
// fetches never share one.
uint16_t SinkFetchTable::next_generation() {
  while (true) {
    ++last_generation_;
    if (last_generation_ == kNoGeneration) {
      continue;
    }
    if (find(last_generation_) == nullptr) {
      return last_generation_;
    }
  }
}

}  // namespace sink
}  // namespace connector
}  // namespace lmcache
