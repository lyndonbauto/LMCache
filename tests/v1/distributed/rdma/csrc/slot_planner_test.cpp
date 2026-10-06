// SPDX-License-Identifier: Apache-2.0
//
// Layer-geometry and window-fit tests for the RDMA slot planner.
//
// Needs no RDMA device. The slot schedule is built in Python
// (tests/v1/layerwise/test_fetch_planner.py) and compared against the write
// side by test_slot_plan_parity.py; this file covers the C++ geometry the
// fetch driver validates layouts with.
//
// The central assertion is that a layer occupies `kv_size` **disjoint** byte
// ranges rather than one. In the standard layout the K/V dimension is
// outermost -- K for every layer, then V for every layer -- so a layer's K and
// V are a whole layer dimension apart. Treating a layer as one range would
// deliver K and leave V holding whatever the window held before: right shape,
// right dtype, plausible values, no error raised.
//
// Usage: slot_planner_test    (takes no arguments; ignores any passed, so it
// can share the harness runner with the device-dependent tests)

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "slot_planner.h"

namespace {

using lmcache::connector::rdma::ByteRange;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::object_group_bytes;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::plane_bytes;
using lmcache::connector::rdma::SlotPlanner;
using lmcache::connector::rdma::window_bytes_per_chunk;
using lmcache::connector::rdma::window_fit_error;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

// A uniform attention group: `layers` layers starting at `first_layer`, with
// the documented default geometry (256 slots, 8 heads x 128, fp16).
KernelGroupLayout attention_group(uint32_t first_layer, uint32_t layers,
                                  uint32_t kv_size = 2,
                                  size_t num_slots = 256) {
  KernelGroupLayout group;
  for (uint32_t i = 0; i < layers; ++i) {
    group.layer_indices.push_back(first_layer + i);
  }
  group.kv_size = kv_size;
  group.num_slots = num_slots;
  group.hidden_dim = 8 * 128;
  group.element_size = 2;
  return group;
}

ObjectGroupLayout single_group_layout(uint32_t layers) {
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(attention_group(0, layers));
  return layout;
}

void test_a_layer_is_several_disjoint_planes() {
  std::cout << "\na layer occupies kv_size disjoint ranges, not one\n";

  const uint32_t kLayers = 32;
  SlotPlanner planner({single_group_layout(kLayers)});
  const KernelGroupLayout group = attention_group(0, kLayers);
  const size_t plane = plane_bytes(group);

  check(plane == 512 * 1024, "a plane is 512 KiB on the default geometry");

  const std::vector<ByteRange> planes = planner.layer_plane_ranges(0);
  check(planes.size() == 2, "layer 0 has two planes, one per K/V half");
  check(planes[0].offset == 0 && planes[0].length == plane,
        "layer 0's K plane is the first plane of the payload");
  // The V plane sits a whole layer dimension away, not adjacent.
  check(planes[1].offset == kLayers * plane && planes[1].length == plane,
        "layer 0's V plane is num_layers planes further on, not next to its K");
  check(planes[1].offset - planes[0].offset > plane,
        "the two planes are not contiguous, so a layer is not one range");

  // Mid-stack layers follow the same rule.
  const std::vector<ByteRange> mid = planner.layer_plane_ranges(7);
  check(mid[0].offset == 7 * plane, "layer 7's K plane is at position 7");
  check(mid[1].offset == (kLayers + 7) * plane,
        "layer 7's V plane is offset by the whole layer dimension");

  // No two layers overlap, and together they tile the payload exactly.
  std::vector<ByteRange> all;
  for (uint32_t layer : planner.layer_ids()) {
    for (const ByteRange& r : planner.layer_plane_ranges(layer)) {
      all.push_back(r);
    }
  }
  std::sort(all.begin(), all.end(), [](const ByteRange& a, const ByteRange& b) {
    return a.offset < b.offset;
  });
  bool tiles = all.size() == static_cast<size_t>(kLayers) * 2;
  size_t expected_offset = 0;
  for (const ByteRange& r : all) {
    tiles = tiles && r.offset == expected_offset;
    expected_offset += r.length;
  }
  check(tiles &&
            expected_offset == object_group_bytes(single_group_layout(kLayers)),
        "every layer's planes together tile the payload exactly once");
}

void test_the_contiguous_layout_variant_needs_no_special_case() {
  std::cout << "\nthe NL_X_NB_BS_HS variant, where a layer is contiguous\n";

  // That format drops the leading K/V dimension, so kv_size is 1 and the
  // layer dimension is outermost.
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(attention_group(0, 4, /*kv_size=*/1));
  SlotPlanner planner({layout});

  const size_t plane = plane_bytes(layout.kernel_groups[0]);
  const std::vector<ByteRange> planes = planner.layer_plane_ranges(2);
  check(planes.size() == 1, "a layer is a single range under this format");
  check(planes[0].offset == 2 * plane && planes[0].length == plane,
        "and it sits at its own layer position");
}

void test_several_kernel_groups_use_their_own_strides() {
  std::cout << "\nhybrid object group: two kernel groups, different geometry\n";

  // A sliding-window group of 8 layers with half the slots, then a
  // full-attention group of 4. Using one group's stride for the other's
  // layers is the hazard here.
  ObjectGroupLayout layout;
  layout.object_group_id = 0;
  layout.kernel_groups.push_back(
      attention_group(0, 8, /*kv_size=*/2, /*num_slots=*/128));
  layout.kernel_groups.push_back(
      attention_group(8, 4, /*kv_size=*/2, /*num_slots=*/256));
  SlotPlanner planner({layout});

  const size_t sw_plane = plane_bytes(layout.kernel_groups[0]);
  const size_t full_plane = plane_bytes(layout.kernel_groups[1]);
  const size_t sw_bytes = 2 * 8 * sw_plane;
  check(sw_plane * 2 == full_plane,
        "the two groups really do have different plane sizes");

  const std::vector<ByteRange> sw = planner.layer_plane_ranges(0);
  check(sw[0].length == sw_plane && sw[1].offset == 8 * sw_plane,
        "a windowed layer is strided by its own group's layer count");

  // The second group starts after the first group's whole tensor.
  const std::vector<ByteRange> full = planner.layer_plane_ranges(8);
  check(full[0].offset == sw_bytes,
        "the second kernel group begins where the first ends");
  check(full[0].length == full_plane,
        "and its planes are its own size, not the first group's");
  check(full[1].offset == sw_bytes + (4 * full_plane),
        "its V plane is strided by its own layer count, not the object's");

  check(object_group_bytes(layout) == sw_bytes + (2 * 4 * full_plane),
        "the payload is the two tensors concatenated");
}

void test_each_layer_resolves_to_its_own_object_group() {
  std::cout << "\nlayers of separate object groups\n";

  ObjectGroupLayout first;
  first.object_group_id = 0;
  first.kernel_groups.push_back(attention_group(0, 2));
  ObjectGroupLayout second;
  second.object_group_id = 1;
  second.kernel_groups.push_back(attention_group(2, 2));
  SlotPlanner planner({first, second});

  check(planner.object_group_of_layer(0) == 0 &&
            planner.object_group_of_layer(2) == 1,
        "each layer resolves to its own object group");
  check(planner.layer_plane_ranges(2)[0].offset == 0,
        "a second object group's payload is based at zero, not after the "
        "first's");
  check(planner.layer_ids() == std::vector<uint32_t>({0, 1, 2, 3}),
        "layer ids span every object group, ascending");
}

void test_invalid_layouts_are_rejected() {
  std::cout << "\ninvalid layouts\n";

  bool threw = false;
  try {
    SlotPlanner planner({});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "an empty layout is rejected");

  threw = false;
  try {
    ObjectGroupLayout layout;
    layout.kernel_groups.push_back(attention_group(0, 2, /*kv_size=*/0));
    SlotPlanner planner({layout});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a kv_size of zero is rejected");

  threw = false;
  try {
    ObjectGroupLayout layout;
    layout.kernel_groups.push_back(
        attention_group(0, 2, /*kv_size=*/2, /*num_slots=*/0));
    SlotPlanner planner({layout});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a zero slot count is rejected");

  threw = false;
  try {
    // The same global layer in two kernel groups: its plane offsets would be
    // ambiguous, so this must not be silently resolved to one of them.
    ObjectGroupLayout layout;
    layout.kernel_groups.push_back(attention_group(0, 2));
    layout.kernel_groups.push_back(attention_group(1, 2));
    SlotPlanner planner({layout});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a layer appearing in two kernel groups is rejected");

  SlotPlanner planner({single_group_layout(2)});
  threw = false;
  try {
    planner.layer_plane_ranges(99);
  } catch (const std::out_of_range&) {
    threw = true;
  }
  check(threw, "a layer absent from the layout is rejected");
}

void test_a_window_must_hold_one_whole_chunk() {
  std::cout << "a window must hold one whole chunk\n";
  // The default attention geometry over 32 layers is Llama-3-8B: 2 x 32 x
  // 256 tokens x 1024 x 2 bytes = 32 MiB per 256-token chunk.
  const std::vector<ObjectGroupLayout> llama3 = {single_group_layout(32)};
  constexpr size_t kMiB = 1u << 20;
  check(window_bytes_per_chunk(llama3, 4096) == 32 * kMiB,
        "one Llama-3-8B chunk is 32 MiB");

  const std::string refused = window_fit_error(llama3, 8 * kMiB, 4096);
  check(!refused.empty(), "the 8 MiB default window is refused");
  check(refused.find("33554432") != std::string::npos,
        "the refusal names the size needed");
  check(window_fit_error(llama3, 32 * kMiB, 4096).empty(),
        "a window of exactly one chunk fits");

  // Two object groups whose sizes are not page multiples: each object is
  // rounded up on its own, as L1 allocates them.
  ObjectGroupLayout small = single_group_layout(1);
  small.kernel_groups[0].num_slots = 1;
  small.kernel_groups[0].hidden_dim = 3;
  ObjectGroupLayout other = small;
  other.object_group_id = 1;
  const std::vector<ObjectGroupLayout> odd = {small, other};
  check(object_group_bytes(small) == 12, "an odd group is 12 bytes");
  check(window_bytes_per_chunk(odd, 4096) == 2 * 4096,
        "each object is rounded up to the L1 alignment separately");
  check(window_bytes_per_chunk(odd, 0) == 24, "no alignment means no rounding");
  check(!window_fit_error(odd, 4096, 4096).empty(),
        "rounding can make a chunk overflow a window");
}

}  // namespace

int main() {
  try {
    test_a_layer_is_several_disjoint_planes();
    test_the_contiguous_layout_variant_needs_no_special_case();
    test_several_kernel_groups_use_their_own_strides();
    test_each_layer_resolves_to_its_own_object_group();
    test_invalid_layouts_are_rejected();
    test_a_window_must_hold_one_whole_chunk();
  } catch (const std::exception& e) {
    std::cerr << "EXCEPTION: " << e.what() << "\n";
    return 1;
  }

  if (failures != 0) {
    std::cout << "\n" << failures << " check(s) FAILED\n";
    return 1;
  }
  std::cout << "\nPASS\n";
  return 0;
}
