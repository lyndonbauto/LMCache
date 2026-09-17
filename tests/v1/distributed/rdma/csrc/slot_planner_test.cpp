// SPDX-License-Identifier: Apache-2.0
//
// Slot-schedule tests for the RDMA layer pipeline.
//
// Needs no RDMA device: this is the arithmetic that decides which writes are
// expected and where each one lands. The fabric is exercised by
// rdma_pipeline_test.cpp.
//
// The central assertion, and the reason this file exists, is that a layer
// occupies `kv_size` **disjoint** byte ranges rather than one. In the
// standard layout the K/V dimension is outermost -- K for every layer, then V
// for every layer -- so a layer's K and V are a whole layer dimension apart.
// A planner that emitted one slot per layer per chunk would deliver K, see
// that slot land, report the layer ready and hand the model a cache whose V
// half is still whatever the window held before. Right shape, right dtype,
// plausible values, no error raised. So the tests below check the ranges
// explicitly, check that they do not touch each other, and check that both
// must land before the layer completes.
//
// Usage: slot_planner_test    (takes no arguments; ignores any passed, so it
// can share the harness runner with the device-dependent tests)

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "layer_pipeline.h"
#include "slot_planner.h"

namespace {

using lmcache::connector::rdma::ByteRange;
using lmcache::connector::rdma::ChunkPlacement;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::LayerReadiness;
using lmcache::connector::rdma::object_group_bytes;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::participating_chunks;
using lmcache::connector::rdma::plane_bytes;
using lmcache::connector::rdma::RequestPlan;
using lmcache::connector::rdma::SlotPlanner;

constexpr uint16_t kGeneration = 0x51a7;
constexpr size_t kBigWrite = 1u << 30;  // larger than any plane here

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

std::vector<ChunkPlacement> placements_for(uint32_t object_group_id,
                                           const std::vector<uint32_t>& chunks,
                                           size_t object_bytes) {
  std::vector<ChunkPlacement> out;
  for (uint32_t chunk : chunks) {
    // Chunks are laid out back to back in the window, which is what LMCache
    // does when it leases a contiguous destination for a request.
    out.push_back(ChunkPlacement{chunk, object_group_id,
                                 static_cast<size_t>(chunk) * object_bytes});
  }
  return out;
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

void test_a_layer_needs_both_planes_before_it_is_ready() {
  std::cout << "\nreadiness requires every plane of the layer\n";

  SlotPlanner planner({single_group_layout(4)});
  const size_t object_bytes = object_group_bytes(single_group_layout(4));
  const RequestPlan plan = planner.plan_request(
      placements_for(0, {0}, object_bytes), kBigWrite, kGeneration);

  check(plan.expected_slots(0) == 2,
        "layer 0 expects two slots for one chunk, one per plane");

  // Land only the first of layer 0's two slots: the K half.
  LayerReadiness readiness(plan);
  bool found_first = false;
  for (uint16_t i = 0; i < plan.slot_count() && !found_first; ++i) {
    if (plan.slot(i).layer_id == 0) {
      readiness.note_arrival(plan.immediate_for(i));
      found_first = true;
    }
  }
  check(found_first && !readiness.is_layer_ready(0),
        "one plane landing does not make the layer ready -- this is the "
        "uninitialised-V failure the planner exists to prevent");

  for (uint16_t i = 0; i < plan.slot_count(); ++i) {
    if (plan.slot(i).layer_id == 0) {
      readiness.note_arrival(plan.immediate_for(i));
    }
  }
  check(readiness.is_layer_ready(0), "both planes landing completes it");
}

void test_a_plane_larger_than_one_write_becomes_several_slots() {
  std::cout << "\na plane larger than the maximum RDMA write\n";

  SlotPlanner planner({single_group_layout(2)});
  const size_t object_bytes = object_group_bytes(single_group_layout(2));
  const size_t plane = plane_bytes(attention_group(0, 2));

  // Three pieces per plane, the last one short.
  const size_t max_write = (plane / 3) + 1;
  const RequestPlan plan = planner.plan_request(
      placements_for(0, {0}, object_bytes), max_write, kGeneration);

  check(plan.expected_slots(0) == 2 * 3,
        "layer 0 needs kv_size x pieces_per_plane slots");

  // Every slot is within the write cap, and each plane's slots cover it
  // exactly without crossing into the neighbouring plane.
  bool bounded = true;
  size_t total = 0;
  for (uint16_t i = 0; i < plan.slot_count(); ++i) {
    bounded = bounded && plan.slot(i).length <= max_write;
    total += plan.slot(i).length;
  }
  check(bounded, "no slot exceeds the maximum write size");
  check(total == object_bytes,
        "the slots together cover the whole object exactly once");

  std::set<size_t> offsets;
  for (uint16_t i = 0; i < plan.slot_count(); ++i) {
    offsets.insert(plan.slot(i).offset);
  }
  check(offsets.size() == plan.slot_count(),
        "no two slots target the same address");
}

void test_the_schedule_is_layer_major_across_chunks() {
  std::cout << "\nthe schedule asks for layer 0 everywhere first\n";

  SlotPlanner planner({single_group_layout(3)});
  const size_t object_bytes = object_group_bytes(single_group_layout(3));
  const RequestPlan plan = planner.plan_request(
      placements_for(0, {0, 1, 2}, object_bytes), kBigWrite, kGeneration);

  // Slot order is the push order hint: layer ids must be non-decreasing, so
  // every chunk's layer 0 precedes any chunk's layer 1.
  bool non_decreasing = true;
  for (uint16_t i = 1; i < plan.slot_count(); ++i) {
    non_decreasing =
        non_decreasing && plan.slot(i - 1).layer_id <= plan.slot(i).layer_id;
  }
  check(non_decreasing, "slots are ordered layer-major, not chunk-major");

  check(plan.expected_slots(0) == 3 * 2,
        "layer 0 expects both planes of all three chunks");

  // Each chunk's slots land inside that chunk's own destination range.
  bool within_placement = true;
  for (uint16_t i = 0; i < plan.slot_count(); ++i) {
    const size_t base =
        static_cast<size_t>(plan.slot(i).chunk_id) * object_bytes;
    within_placement =
        within_placement && plan.slot(i).offset >= base &&
        plan.slot(i).offset + plan.slot(i).length <= base + object_bytes;
  }
  check(within_placement,
        "every slot writes inside the destination chosen for its chunk");
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

void test_a_windowed_group_only_covers_its_window() {
  std::cout << "\nsliding-window groups and separate object groups\n";

  // Group 0 is full attention over every chunk; group 1 is a sliding-window
  // group covering only the two trailing chunks.
  ObjectGroupLayout full;
  full.object_group_id = 0;
  full.kernel_groups.push_back(attention_group(0, 2));
  ObjectGroupLayout windowed;
  windowed.object_group_id = 1;
  windowed.kernel_groups.push_back(attention_group(2, 2));
  SlotPlanner planner({full, windowed});

  check(planner.object_group_of_layer(0) == 0 &&
            planner.object_group_of_layer(2) == 1,
        "each layer resolves to its own object group");

  const uint32_t kChunks = 5;
  check(participating_chunks(kChunks, 0).size() == kChunks,
        "a group with no window covers every chunk");
  const std::vector<uint32_t> window = participating_chunks(kChunks, 2);
  check(window.size() == 2 && window[0] == 3 && window[1] == 4,
        "a window of 2 covers the trailing chunks, not the leading ones");
  check(participating_chunks(kChunks, 99).size() == kChunks,
        "a window wider than the request is clamped to it");

  const size_t full_bytes = object_group_bytes(full);
  const size_t win_bytes = object_group_bytes(windowed);
  std::vector<ChunkPlacement> placements =
      placements_for(0, participating_chunks(kChunks, 0), full_bytes);
  for (const ChunkPlacement& p : placements_for(1, window, win_bytes)) {
    placements.push_back(p);
  }

  const RequestPlan plan =
      planner.plan_request(placements, kBigWrite, kGeneration);
  check(plan.expected_slots(0) == kChunks * 2,
        "the full-attention layer expects every chunk");
  check(plan.expected_slots(2) == 2 * 2,
        "the windowed layer expects only its window, so it can complete");

  // The windowed layer is ready without the chunks it never covered.
  LayerReadiness readiness(plan);
  for (uint16_t i = 0; i < plan.slot_count(); ++i) {
    if (plan.slot(i).layer_id == 2) {
      readiness.note_arrival(plan.immediate_for(i));
    }
  }
  check(readiness.is_layer_ready(2),
        "the windowed layer completes on its window alone");
  check(!readiness.is_layer_ready(0),
        "the full-attention layer still waits for the rest");
}

void test_a_group_with_no_placements_contributes_nothing() {
  std::cout << "\nobject groups the request is not fetching\n";

  ObjectGroupLayout first;
  first.object_group_id = 0;
  first.kernel_groups.push_back(attention_group(0, 2));
  ObjectGroupLayout second;
  second.object_group_id = 1;
  second.kernel_groups.push_back(attention_group(2, 2));
  SlotPlanner planner({first, second});

  // Only the first group is placed, as under CacheBlend where a leg reads a
  // subset of groups.
  const RequestPlan plan =
      planner.plan_request(placements_for(0, {0}, object_group_bytes(first)),
                           kBigWrite, kGeneration);
  check(plan.expected_slots(0) == 2, "the placed group's layers are scheduled");
  check(plan.expected_slots(2) == 0,
        "the unplaced group's layers get no slots");

  LayerReadiness readiness(plan);
  for (uint16_t i = 0; i < plan.slot_count(); ++i) {
    readiness.note_arrival(plan.immediate_for(i));
  }
  check(readiness.all_ready(), "the request completes on the placed group");
  check(!readiness.is_layer_ready(2),
        "a layer that was never fetched is never reported ready");
}

void test_invalid_layouts_and_requests_are_rejected() {
  std::cout << "\ninvalid layouts and requests\n";

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
  const size_t object_bytes = object_group_bytes(single_group_layout(2));

  threw = false;
  try {
    planner.plan_request(placements_for(0, {0}, object_bytes), 0, kGeneration);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a zero maximum write size is rejected");

  threw = false;
  try {
    std::vector<ChunkPlacement> repeated = {ChunkPlacement{0, 0, 0},
                                            ChunkPlacement{0, 0, object_bytes}};
    planner.plan_request(repeated, kBigWrite, kGeneration);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw,
        "placing one chunk twice for a group is rejected, since it would "
        "double the layer's expected count and it could never complete");

  threw = false;
  try {
    planner.layer_plane_ranges(99);
  } catch (const std::out_of_range&) {
    threw = true;
  }
  check(threw, "a layer absent from the layout is rejected");
}

}  // namespace

int main() {
  try {
    test_a_layer_is_several_disjoint_planes();
    test_the_contiguous_layout_variant_needs_no_special_case();
    test_a_layer_needs_both_planes_before_it_is_ready();
    test_a_plane_larger_than_one_write_becomes_several_slots();
    test_the_schedule_is_layer_major_across_chunks();
    test_several_kernel_groups_use_their_own_strides();
    test_a_windowed_group_only_covers_its_window();
    test_a_group_with_no_placements_contributes_nothing();
    test_invalid_layouts_and_requests_are_rejected();
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
