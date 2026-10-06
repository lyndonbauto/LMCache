// SPDX-License-Identifier: Apache-2.0
#pragma once

#include "slot_planner.h"

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// One kernel group's layout as published from Python registration.
struct KernelGroupLayoutInput {
  std::vector<int64_t> shape;
  // Bytes per element of the kernel group's dtype, as torch reports it.
  size_t element_size = 0;
  // Global layer indices for this kernel group, in tensor layer order. When
  // empty, consecutive indices are assigned from zero within the object group.
  std::vector<uint32_t> layer_indices;
};

// Per-object-group layout input for ``set_object_group_layouts``.
struct ObjectGroupLayoutInput {
  std::vector<KernelGroupLayoutInput> kernel_groups;
};

// Convert registered Python layouts into ``SlotPlanner`` geometry.
//
// Throws std::invalid_argument when shapes, element sizes, or layer indices
// disagree, when an element size is zero, or when a shape is not a supported
// KV tensor rank.
std::vector<ObjectGroupLayout> object_group_layouts_from_inputs(
    const std::map<uint32_t, ObjectGroupLayoutInput>& inputs);

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
