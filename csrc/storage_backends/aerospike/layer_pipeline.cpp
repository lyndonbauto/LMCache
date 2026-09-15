// SPDX-License-Identifier: Apache-2.0

#include "layer_pipeline.h"

#include <stdexcept>
#include <string>

namespace lmcache {
namespace connector {
namespace rdma {

uint32_t encode_immediate(uint16_t generation, uint16_t slot_index) {
  return (static_cast<uint32_t>(generation) << kGenerationShift) |
         static_cast<uint32_t>(slot_index);
}

uint16_t immediate_generation(uint32_t immediate) {
  return static_cast<uint16_t>(immediate >> kGenerationShift);
}

uint16_t immediate_slot_index(uint32_t immediate) {
  return static_cast<uint16_t>(immediate & kSlotIndexMask);
}

uint16_t FetchPlan::add_slot(uint32_t layer_id, size_t offset, size_t length) {
  if (slots_.size() >= kMaxSlotsPerFetch) {
    throw std::length_error(
        "FetchPlan: a single fetch cannot exceed " +
        std::to_string(kMaxSlotsPerFetch) +
        " slots; the immediate data has no room for the index");
  }
  if (length == 0) {
    throw std::invalid_argument(
        "FetchPlan: a slot of zero length would never raise a completion, so "
        "its layer could never be reported ready");
  }

  const uint16_t slot_index = static_cast<uint16_t>(slots_.size());
  slots_.push_back(FetchSlot{layer_id, offset, length});
  ++expected_per_layer_[layer_id];
  return slot_index;
}

const FetchSlot& FetchPlan::slot(uint16_t slot_index) const {
  if (static_cast<size_t>(slot_index) >= slots_.size()) {
    throw std::out_of_range("FetchPlan: no slot at index " +
                            std::to_string(slot_index));
  }
  return slots_[slot_index];
}

std::vector<uint32_t> FetchPlan::layer_ids() const {
  std::vector<uint32_t> out;
  out.reserve(expected_per_layer_.size());
  for (const auto& entry : expected_per_layer_) {
    out.push_back(entry.first);
  }
  return out;
}

uint32_t FetchPlan::expected_slots(uint32_t layer_id) const {
  const auto found = expected_per_layer_.find(layer_id);
  return found == expected_per_layer_.end() ? 0 : found->second;
}

uint32_t FetchPlan::immediate_for(uint16_t slot_index) const {
  if (static_cast<size_t>(slot_index) >= slots_.size()) {
    throw std::out_of_range("FetchPlan: no slot at index " +
                            std::to_string(slot_index));
  }
  return encode_immediate(generation_, slot_index);
}

LayerReadiness::LayerReadiness(const FetchPlan& plan)
    : generation_(plan.generation()),
      expected_slots_(plan.slot_count()),
      slot_seen_(plan.slot_count(), false) {
  slot_layer_.reserve(plan.slot_count());
  for (size_t i = 0; i < plan.slot_count(); ++i) {
    const uint32_t layer_id = plan.slot(static_cast<uint16_t>(i)).layer_id;
    slot_layer_.push_back(layer_id);
    ++expected_per_layer_[layer_id];
    landed_per_layer_.emplace(layer_id, 0);
  }
}

ArrivalStatus LayerReadiness::note_arrival(uint32_t immediate) {
  if (immediate_generation(immediate) != generation_) {
    return ArrivalStatus::kStaleGeneration;
  }

  const uint16_t slot_index = immediate_slot_index(immediate);
  if (static_cast<size_t>(slot_index) >= slot_seen_.size()) {
    return ArrivalStatus::kUnknownSlot;
  }
  if (slot_seen_[slot_index]) {
    return ArrivalStatus::kDuplicate;
  }

  slot_seen_[slot_index] = true;
  ++landed_slots_;

  const uint32_t layer_id = slot_layer_[slot_index];
  const uint32_t landed = ++landed_per_layer_[layer_id];
  if (landed == expected_per_layer_[layer_id]) {
    return ArrivalStatus::kLayerComplete;
  }
  return ArrivalStatus::kAccepted;
}

bool LayerReadiness::is_layer_ready(uint32_t layer_id) const {
  const auto expected = expected_per_layer_.find(layer_id);
  if (expected == expected_per_layer_.end()) {
    return false;
  }
  const auto landed = landed_per_layer_.find(layer_id);
  return landed != landed_per_layer_.end() &&
         landed->second == expected->second;
}

std::vector<uint32_t> LayerReadiness::ready_layers() const {
  std::vector<uint32_t> out;
  for (const auto& entry : expected_per_layer_) {
    const auto landed = landed_per_layer_.find(entry.first);
    if (landed != landed_per_layer_.end() && landed->second == entry.second) {
      out.push_back(entry.first);
    }
  }
  return out;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
