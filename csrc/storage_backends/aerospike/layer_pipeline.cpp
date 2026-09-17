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

uint16_t RequestPlan::add_slot(uint32_t layer_id, uint32_t chunk_id,
                               size_t offset, size_t length) {
  if (slots_.size() >= kMaxSlotsPerRequest) {
    throw std::length_error(
        "RequestPlan: a single request cannot exceed " +
        std::to_string(kMaxSlotsPerRequest) +
        " slots; the immediate data has no room for the index");
  }
  if (length == 0) {
    throw std::invalid_argument(
        "RequestPlan: a slot of zero length would never raise a completion, "
        "so its layer could never be reported ready");
  }

  const uint16_t slot_index = static_cast<uint16_t>(slots_.size());
  slots_.push_back(FetchSlot{layer_id, chunk_id, offset, length});
  ++expected_per_layer_[layer_id];
  slots_per_chunk_[chunk_id].push_back(slot_index);
  return slot_index;
}

const FetchSlot& RequestPlan::slot(uint16_t slot_index) const {
  if (static_cast<size_t>(slot_index) >= slots_.size()) {
    throw std::out_of_range("RequestPlan: no slot at index " +
                            std::to_string(slot_index));
  }
  return slots_[slot_index];
}

std::vector<uint32_t> RequestPlan::layer_ids() const {
  std::vector<uint32_t> out;
  out.reserve(expected_per_layer_.size());
  for (const auto& entry : expected_per_layer_) {
    out.push_back(entry.first);
  }
  return out;
}

std::vector<uint32_t> RequestPlan::chunk_ids() const {
  std::vector<uint32_t> out;
  out.reserve(slots_per_chunk_.size());
  for (const auto& entry : slots_per_chunk_) {
    out.push_back(entry.first);
  }
  return out;
}

std::vector<uint16_t> RequestPlan::slots_for_chunk(uint32_t chunk_id) const {
  const auto found = slots_per_chunk_.find(chunk_id);
  if (found == slots_per_chunk_.end()) {
    return {};
  }
  return found->second;
}

uint32_t RequestPlan::expected_slots(uint32_t layer_id) const {
  const auto found = expected_per_layer_.find(layer_id);
  return found == expected_per_layer_.end() ? 0 : found->second;
}

uint32_t RequestPlan::immediate_for(uint16_t slot_index) const {
  if (static_cast<size_t>(slot_index) >= slots_.size()) {
    throw std::out_of_range("RequestPlan: no slot at index " +
                            std::to_string(slot_index));
  }
  return encode_immediate(generation_, slot_index);
}

LayerReadiness::LayerReadiness(const RequestPlan& plan)
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

bool LayerReadiness::note_unservable(uint16_t slot_index) {
  if (static_cast<size_t>(slot_index) >= slot_seen_.size()) {
    return false;
  }
  if (slot_seen_[slot_index]) {
    return false;
  }

  // Marking the slot seen without incrementing landed_slots_ is what makes
  // the layer permanently unready: its landed count can now never reach its
  // expected count, so is_layer_ready and all_ready stay false without
  // needing to consult the unservable table. The mark also makes a late
  // arrival for this slot report as a duplicate rather than as progress.
  slot_seen_[slot_index] = true;
  ++unservable_per_layer_[slot_layer_[slot_index]];
  return true;
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

std::vector<uint32_t> LayerReadiness::unservable_layers() const {
  std::vector<uint32_t> out;
  out.reserve(unservable_per_layer_.size());
  for (const auto& entry : unservable_per_layer_) {
    out.push_back(entry.first);
  }
  return out;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
