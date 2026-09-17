// SPDX-License-Identifier: Apache-2.0
//
// Sharding tests for the Aerospike layer-pipelined reception path.
//
// Unlike the other harnesses here this one needs no RDMA device and no
// Aerospike cluster -- it is pure arithmetic. That is deliberate: the shard
// plan decides which records a layer must wait for, and getting it wrong does
// not fail loudly. It shows up as a layer that reports ready late, or as a
// record belonging to two layers at once, neither of which any byte-level
// equivalence test would catch.
//
// The load-bearing test is `bytes a layer waits for`. A layer owns `kv_size`
// disjoint byte ranges (the K/V plane is the outermost dimension, so K for
// every layer precedes V for every layer). Byte-count sharding cuts records
// wherever the division lands, so a layer's records spill into its
// neighbours' planes and the layer cannot be declared ready until those have
// landed too. This test measures that inflation and pins it against the
// figures recorded in layerwise_transfer_data_model.md for the unified block
// sizes Mamba/GDN hybrids force.
//
// Usage: shard_plan_test    (takes no arguments; ignores any passed, so it
// can share the harness runner with the device-dependent tests)

#include <cstddef>
#include <cstdint>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "shard_plan.h"

namespace {

using lmcache::connector::make_shard_plan;
using lmcache::connector::pieces_per_plane;
using lmcache::connector::segment_range;
using lmcache::connector::ShardPlan;
using lmcache::connector::ShardRange;

constexpr size_t kMiB = 1024 * 1024;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

// One kernel group's geometry, in the terms the authoritative layout uses.
//
// The payload is a tensor of shape (kv_size, num_layers, num_slots,
// hidden_dim), so one "plane" -- one layer's bytes within one of the K/V
// halves -- is num_slots * hidden_dim * element_size, and the whole payload is
// kv_size * num_layers planes.
struct Geometry {
  size_t num_layers = 0;
  size_t kv_size = 0;
  size_t num_slots = 0;
  size_t hidden_dim = 0;
  size_t element_size = 0;

  size_t plane_bytes() const { return num_slots * hidden_dim * element_size; }

  size_t payload_bytes() const { return kv_size * num_layers * plane_bytes(); }

  // Byte offset of layer `layer`'s plane in K/V half `kv`.
  size_t plane_offset(size_t kv, size_t layer) const {
    return ((kv * num_layers) + layer) * plane_bytes();
  }
};

// Total bytes that must land before `layer` is complete under `plan`.
//
// Counts each distinct record once: a record touching two of the layer's own
// planes is not counted twice, and a record shared with a neighbouring layer
// is counted in full, because the layer cannot proceed until all of it has
// arrived.
size_t bytes_awaited_for_layer(const ShardPlan& plan, const Geometry& geom,
                               size_t layer) {
  const size_t total = geom.payload_bytes();
  std::set<uint32_t> touched;
  for (size_t kv = 0; kv < geom.kv_size; ++kv) {
    const size_t start = geom.plane_offset(kv, layer);
    const size_t end = start + geom.plane_bytes();
    for (uint32_t i = 0; i < plan.nseg; ++i) {
      const ShardRange r = segment_range(plan, i, total);
      if (r.length == 0) {
        continue;
      }
      const bool overlaps = r.offset < end && start < r.offset + r.length;
      if (overlaps) {
        touched.insert(i);
      }
    }
  }
  size_t bytes = 0;
  for (uint32_t i : touched) {
    bytes += segment_range(plan, i, total).length;
  }
  return bytes;
}

// Report whether any record spans a plane boundary.
bool any_record_straddles_a_plane(const ShardPlan& plan, const Geometry& geom) {
  const size_t total = geom.payload_bytes();
  const size_t plane = geom.plane_bytes();
  for (uint32_t i = 0; i < plan.nseg; ++i) {
    const ShardRange r = segment_range(plan, i, total);
    if (r.length == 0) {
      continue;
    }
    // A record is confined to one plane when its first and last bytes fall in
    // the same plane.
    if (r.offset / plane != (r.offset + r.length - 1) / plane) {
      return true;
    }
  }
  return false;
}

// Report whether the plan's records tile [0, total) exactly once, in order.
bool records_tile_payload_exactly(const ShardPlan& plan, size_t total) {
  size_t expected_offset = 0;
  for (uint32_t i = 0; i < plan.nseg; ++i) {
    const ShardRange r = segment_range(plan, i, total);
    if (r.offset != expected_offset) {
      return false;
    }
    expected_offset += r.length;
  }
  return expected_offset == total;
}

// Percentage by which `awaited` exceeds `owned`, rounded to the nearest whole
// percent.
long inflation_percent(size_t awaited, size_t owned) {
  const double ratio =
      static_cast<double>(awaited) / static_cast<double>(owned);
  return static_cast<long>((ratio - 1.0) * 100.0 + 0.5);
}

// The unified block sizes vLLM forces for Mamba/GDN hybrids, with the
// inflation that layerwise_transfer_data_model.md records for each under
// byte-count sharding against 1 MiB records.
struct HybridCase {
  size_t tokens_per_chunk;
  long documented_byte_count_inflation_percent;
};

void test_documented_hybrid_block_sizes() {
  std::cout << "\nunified hybrid block sizes, against 1 MiB records\n";

  const std::vector<HybridCase> cases = {{544, 88}, {784, 31}, {944, 8}};

  for (const HybridCase& c : cases) {
    Geometry geom;
    geom.num_layers = 32;
    geom.kv_size = 2;
    geom.num_slots = c.tokens_per_chunk;
    geom.hidden_dim = 8 * 128;  // 8 KV heads, head dim 128
    geom.element_size = 2;      // fp16

    const size_t total = geom.payload_bytes();
    const size_t owned = geom.kv_size * geom.plane_bytes();
    const std::string label = std::to_string(c.tokens_per_chunk) + " tokens";

    const ShardPlan byte_count =
        make_shard_plan(total, kMiB, kMiB, kMiB, /*plane_bytes=*/0);
    const long measured =
        inflation_percent(bytes_awaited_for_layer(byte_count, geom, 0), owned);
    check(measured == c.documented_byte_count_inflation_percent,
          label + ": byte-count sharding inflates layer 0 by +" +
              std::to_string(measured) + "%, documented +" +
              std::to_string(c.documented_byte_count_inflation_percent) + "%");
    check(any_record_straddles_a_plane(byte_count, geom),
          label +
              ": byte-count sharding does straddle planes, which is the "
              "problem being solved");

    const ShardPlan aligned =
        make_shard_plan(total, kMiB, kMiB, kMiB, geom.plane_bytes());
    check(aligned.plane_b == geom.plane_bytes(),
          label + ": the plane hint is honoured");
    check(bytes_awaited_for_layer(aligned, geom, 0) == owned,
          label +
              ": plane-aligned makes layer 0 wait for exactly its own "
              "bytes, no more");
    check(!any_record_straddles_a_plane(aligned, geom),
          label + ": no plane-aligned record spans a plane boundary");
    check(records_tile_payload_exactly(aligned, total),
          label +
              ": plane-aligned records still cover the payload exactly "
              "once");
    check(aligned.seg_b <= kMiB,
          label + ": no plane-aligned record exceeds the cap");
  }
}

void test_documented_worked_example() {
  std::cout << "\nthe 256-token worked example\n";

  Geometry geom;
  geom.num_layers = 32;
  geom.kv_size = 2;
  geom.num_slots = 256;
  geom.hidden_dim = 8 * 128;
  geom.element_size = 2;

  check(geom.plane_bytes() == 512 * 1024, "a plane is 512 KiB");
  check(geom.kv_size * geom.plane_bytes() == kMiB, "a layer is 1 MiB");
  check(geom.payload_bytes() == 32 * kMiB, "the object is 32 MiB");

  // Under an 8 MiB cap a plane fits comfortably, so the rule gives one record
  // per plane: 64 records rather than the 32 a cap-driven split would pick.
  const ShardPlan aligned = make_shard_plan(
      geom.payload_bytes(), 8 * kMiB, 8 * kMiB, 8 * kMiB, geom.plane_bytes());
  check(aligned.nseg == 64, "one record per plane gives 64 records");
  check(aligned.seg_b == 512 * 1024, "each record is exactly one plane");
  check(pieces_per_plane(aligned) == 1, "a plane needs one record");
  check(bytes_awaited_for_layer(aligned, geom, 7) ==
            geom.kv_size * geom.plane_bytes(),
        "a mid-stack layer waits for its two planes and nothing else");

  // The cap stops being a tuning knob once planes fit under it.
  const ShardPlan raised_cap = make_shard_plan(
      geom.payload_bytes(), 8 * kMiB, 32 * kMiB, 8 * kMiB, geom.plane_bytes());
  check(raised_cap.seg_b == aligned.seg_b && raised_cap.nseg == aligned.nseg,
        "raising the record cap changes nothing when a plane already fits");
}

void test_a_multi_plane_payload_is_not_collapsed_into_one_record() {
  std::cout << "\nthe single-record fast path does not defeat alignment\n";

  // Two 256 KiB planes, well under a 1 MiB threshold. Byte-count sharding
  // stores this as one record holding both planes, which is precisely the
  // layout the plane rule rejects.
  const size_t plane = 256 * 1024;
  const size_t payload = 2 * plane;

  const ShardPlan byte_count =
      make_shard_plan(payload, kMiB, kMiB, kMiB, /*plane_bytes=*/0);
  check(byte_count.nseg == 1,
        "without a hint a small payload is still one record");

  const ShardPlan aligned = make_shard_plan(payload, kMiB, kMiB, kMiB, plane);
  check(aligned.nseg == 2,
        "with a hint it becomes one record per plane, not one record holding "
        "two");
  check(aligned.seg_b == plane, "each record is exactly one plane");

  // A payload that is a single whole plane legitimately stays one record.
  const ShardPlan one_plane = make_shard_plan(plane, kMiB, kMiB, kMiB, plane);
  check(one_plane.nseg == 1, "a one-plane payload is a single record");
}

void test_fallback_when_planes_are_not_uniform() {
  std::cout << "\nfallback when the hint cannot describe the payload\n";

  // A payload that is not a whole number of planes means the kernel groups in
  // this object group have different plane sizes, which one uniform stride
  // cannot express.
  const size_t plane = 300 * 1024;
  const size_t payload = 2 * plane + 7;

  const ShardPlan plan = make_shard_plan(payload, kMiB, kMiB, kMiB, plane);
  check(plan.plane_b == 0,
        "a hint that does not divide the payload is refused, and the refusal "
        "is visible as plane_b == 0");
  check(records_tile_payload_exactly(plan, payload),
        "the fallback plan still covers the payload exactly once");
}

void test_a_plane_larger_than_the_cap_is_split_evenly() {
  std::cout << "\na plane larger than the record cap\n";

  // 1.53 MiB plane against 1 MiB records: two pieces of 0.765 MiB each.
  const size_t plane = 784 * 8 * 128 * 2;
  const size_t payload = 4 * plane;

  const ShardPlan plan = make_shard_plan(payload, kMiB, kMiB, kMiB, plane);
  check(pieces_per_plane(plan) == 2, "the plane is split into two records");
  check(plan.seg_b == plane / 2, "the two records are equal halves of a plane");
  check(plan.nseg == 8, "four planes at two records each");
  check(!any_record_straddles_a_plane(plan, Geometry{4, 1, 784, 8 * 128, 2}),
        "neither half crosses into the next plane");
}

void test_a_plane_that_does_not_divide_evenly_ends_in_a_short_record() {
  std::cout << "\na plane whose size is not a multiple of the record size\n";

  // 1001-byte planes against a 300-byte cap: 4 pieces, so 251 bytes each, and
  // the fourth piece of every plane holds the remaining 248.
  const size_t plane = 1001;
  const size_t payload = 3 * plane;

  const ShardPlan plan = make_shard_plan(payload, 300, 300, 300, plane);
  check(plan.plane_b == plane, "the hint is honoured");
  check(pieces_per_plane(plan) == 4, "the plane needs four records");
  check(plan.seg_b == 251, "the records are 251 bytes");

  const ShardRange last_of_first_plane = segment_range(plan, 3, payload);
  check(last_of_first_plane.offset == 753 && last_of_first_plane.length == 248,
        "the last record of a plane is short rather than spilling into the "
        "next plane");

  const ShardRange first_of_second_plane = segment_range(plan, 4, payload);
  check(first_of_second_plane.offset == plane,
        "the next plane's first record starts on the plane boundary");
  check(records_tile_payload_exactly(plan, payload),
        "short records still tile the payload exactly once");
}

void test_invalid_input_is_rejected() {
  std::cout << "\ninvalid input\n";

  bool threw = false;
  try {
    make_shard_plan(0, kMiB, kMiB, kMiB, 0);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a zero-byte payload is rejected");

  threw = false;
  try {
    make_shard_plan(kMiB, kMiB, 0, kMiB, 0);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a zero record cap is rejected");

  const ShardPlan plan = make_shard_plan(4 * kMiB, kMiB, kMiB, kMiB, 0);
  threw = false;
  try {
    segment_range(plan, plan.nseg, 4 * kMiB);
  } catch (const std::out_of_range&) {
    threw = true;
  }
  check(threw, "a record index beyond the plan is rejected");
}

}  // namespace

int main() {
  try {
    test_documented_hybrid_block_sizes();
    test_documented_worked_example();
    test_a_multi_plane_payload_is_not_collapsed_into_one_record();
    test_fallback_when_planes_are_not_uniform();
    test_a_plane_larger_than_the_cap_is_split_evenly();
    test_a_plane_that_does_not_divide_evenly_ends_in_a_short_record();
    test_invalid_input_is_rejected();
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
