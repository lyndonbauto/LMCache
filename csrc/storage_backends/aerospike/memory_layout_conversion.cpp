// SPDX-License-Identifier: Apache-2.0

#include "memory_layout_conversion.h"

#include <algorithm>
#include <cctype>
#include <stdexcept>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

std::string lower_ascii(std::string value) {
  std::transform(
      value.begin(), value.end(), value.begin(),
      [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
  return value;
}

size_t element_size_from_dtype(const std::string& dtype) {
  const std::string normalized = lower_ascii(dtype);
  if (normalized.find("float16") != std::string::npos ||
      normalized.find("bfloat16") != std::string::npos ||
      normalized == "half") {
    return 2;
  }
  if (normalized.find("float32") != std::string::npos ||
      normalized.find("float") != std::string::npos) {
    return 4;
  }
  if (normalized.find("uint8") != std::string::npos ||
      normalized.find("byte") != std::string::npos) {
    return 1;
  }
  throw std::invalid_argument("memory layout conversion: unsupported dtype '" +
                              dtype + "'");
}

struct ParsedShape {
  uint32_t kv_size = 0;
  size_t num_layers = 0;
  size_t num_slots = 0;
  size_t hidden_dim = 0;
};

ParsedShape parse_kernel_shape(const std::vector<int64_t>& shape) {
  if (shape.size() == 4) {
    if (shape[0] <= 0 || shape[1] <= 0 || shape[2] <= 0 || shape[3] <= 0) {
      throw std::invalid_argument(
          "memory layout conversion: kernel shape dimensions must be "
          "positive");
    }
    ParsedShape parsed;
    parsed.kv_size = static_cast<uint32_t>(shape[0]);
    parsed.num_layers = static_cast<size_t>(shape[1]);
    parsed.num_slots = static_cast<size_t>(shape[2]);
    parsed.hidden_dim = static_cast<size_t>(shape[3]);
    return parsed;
  }
  if (shape.size() == 3) {
    if (shape[0] <= 0 || shape[1] <= 0 || shape[2] <= 0) {
      throw std::invalid_argument(
          "memory layout conversion: kernel shape dimensions must be "
          "positive");
    }
    ParsedShape parsed;
    parsed.kv_size = 1;
    parsed.num_layers = static_cast<size_t>(shape[0]);
    parsed.num_slots = static_cast<size_t>(shape[1]);
    parsed.hidden_dim = static_cast<size_t>(shape[2]);
    return parsed;
  }
  throw std::invalid_argument(
      "memory layout conversion: expected a 3D or 4D kernel shape");
}

std::vector<uint32_t> resolve_layer_indices(
    const std::vector<uint32_t>& provided, size_t num_layers,
    uint32_t& next_auto_layer) {
  if (!provided.empty()) {
    if (provided.size() != num_layers) {
      throw std::invalid_argument(
          "memory layout conversion: layer_indices length does not match "
          "tensor layer dimension");
    }
    return provided;
  }
  std::vector<uint32_t> assigned;
  assigned.reserve(num_layers);
  for (size_t i = 0; i < num_layers; ++i) {
    assigned.push_back(next_auto_layer);
    ++next_auto_layer;
  }
  return assigned;
}

}  // namespace

std::vector<ObjectGroupLayout> object_group_layouts_from_inputs(
    const std::map<uint32_t, ObjectGroupLayoutInput>& inputs) {
  if (inputs.empty()) {
    throw std::invalid_argument(
        "memory layout conversion: at least one object group is required");
  }

  std::vector<ObjectGroupLayout> layouts;
  layouts.reserve(inputs.size());
  for (const auto& entry : inputs) {
    ObjectGroupLayout layout;
    layout.object_group_id = entry.first;
    if (entry.second.kernel_groups.empty()) {
      throw std::invalid_argument("memory layout conversion: object group " +
                                  std::to_string(entry.first) +
                                  " has no kernel groups");
    }

    uint32_t next_auto_layer = 0;
    for (const KernelGroupLayoutInput& group_input :
         entry.second.kernel_groups) {
      if (group_input.shape.empty()) {
        throw std::invalid_argument(
            "memory layout conversion: kernel group shape is empty");
      }
      const ParsedShape parsed = parse_kernel_shape(group_input.shape);
      const std::vector<uint32_t> layer_indices = resolve_layer_indices(
          group_input.layer_indices, parsed.num_layers, next_auto_layer);

      KernelGroupLayout group;
      group.layer_indices = layer_indices;
      group.kv_size = parsed.kv_size;
      group.num_slots = parsed.num_slots;
      group.hidden_dim = parsed.hidden_dim;
      group.element_size = element_size_from_dtype(group_input.dtype);
      layout.kernel_groups.push_back(std::move(group));
    }
    layouts.push_back(std::move(layout));
  }
  return layouts;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
