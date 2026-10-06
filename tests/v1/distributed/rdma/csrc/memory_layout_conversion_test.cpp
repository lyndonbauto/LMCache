// SPDX-License-Identifier: Apache-2.0
//
// Tests for converting registered Python layouts into slot-planner geometry.
//
// The element size used to come from parsing the dtype's name, which sized
// every "float*" dtype at 4 bytes: FP8 KV planes were overestimated fourfold
// and the window-fit check could refuse a window that holds the model. Python
// now sends torch's own element size, and these tests pin that it is used as
// given and that a zero size is refused rather than planning empty planes.
//
// Usage: memory_layout_conversion_test    (takes no arguments)

#include <cstdint>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "memory_layout_conversion.h"

namespace {

using lmcache::connector::rdma::KernelGroupLayoutInput;
using lmcache::connector::rdma::object_group_layouts_from_inputs;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::ObjectGroupLayoutInput;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

KernelGroupLayoutInput kernel_group(std::vector<int64_t> shape,
                                    size_t element_size) {
  KernelGroupLayoutInput input;
  input.shape = std::move(shape);
  input.element_size = element_size;
  return input;
}

std::map<uint32_t, ObjectGroupLayoutInput> one_group(
    std::vector<KernelGroupLayoutInput> kernel_groups) {
  ObjectGroupLayoutInput group;
  group.kernel_groups = std::move(kernel_groups);
  return {{0, group}};
}

bool refuses(const std::map<uint32_t, ObjectGroupLayoutInput>& inputs) {
  try {
    object_group_layouts_from_inputs(inputs);
  } catch (const std::invalid_argument&) {
    return true;
  }
  return false;
}

void test_element_sizes_are_used_as_given() {
  std::cout << "element sizes are used as given\n";
  for (const size_t element_size : {size_t{1}, size_t{2}, size_t{4},
                                    size_t{8}}) {
    const std::vector<ObjectGroupLayout> layouts = object_group_layouts_from_inputs(
        one_group({kernel_group({2, 3, 16, 32}, element_size)}));
    check(layouts.size() == 1 && layouts[0].kernel_groups.size() == 1 &&
              layouts[0].kernel_groups[0].element_size == element_size,
          "element size " + std::to_string(element_size) + " is kept");
  }
}

void test_hybrid_kernel_groups_keep_their_own_sizes() {
  std::cout << "hybrid kernel groups keep their own sizes\n";
  const std::vector<ObjectGroupLayout> layouts = object_group_layouts_from_inputs(
      one_group({kernel_group({2, 3, 16, 32}, 1), kernel_group({2, 8, 64}, 4)}));
  const auto& groups = layouts[0].kernel_groups;
  check(groups.size() == 2, "two kernel groups");
  check(groups[0].element_size == 1 && groups[0].kv_size == 2 &&
            groups[0].num_slots == 16 && groups[0].hidden_dim == 32,
        "4D FP8 group keeps its shape and 1-byte elements");
  check(groups[1].element_size == 4 && groups[1].kv_size == 1 &&
            groups[1].num_slots == 8 && groups[1].hidden_dim == 64,
        "3D fp32 group keeps its shape and 4-byte elements");
  check(groups[1].layer_indices == std::vector<uint32_t>{3, 4},
        "layers are numbered on from the previous group");
}

void test_zero_element_size_is_refused() {
  std::cout << "a zero element size is refused\n";
  check(refuses(one_group({kernel_group({2, 3, 16, 32}, 0)})),
        "zero element size throws invalid_argument");
  check(refuses(one_group(
            {kernel_group({2, 3, 16, 32}, 2), kernel_group({2, 8, 64}, 0)})),
        "a zero size in any kernel group throws");
}

}  // namespace

int main() {
  test_element_sizes_are_used_as_given();
  test_hybrid_kernel_groups_keep_their_own_sizes();
  test_zero_element_size_is_refused();
  if (failures != 0) {
    std::cout << failures << " check(s) failed\n";
    return 1;
  }
  std::cout << "all checks passed\n";
  return 0;
}
