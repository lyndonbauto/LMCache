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

using lmcache::connector::choose_shard_plan;
using lmcache::connector::decode_shard_runs;
using lmcache::connector::encode_shard_runs;
using lmcache::connector::layered_payload_bytes;
using lmcache::connector::make_layered_shard_plan;
using lmcache::connector::make_shard_plan;
using lmcache::connector::pieces_per_plane;
using lmcache::connector::PlaneRun;
using lmcache::connector::record_layouts_by_payload;
using lmcache::connector::require_consistent_runs;
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

// Payload-order plane boundaries of an object laid out as `runs`.
std::vector<ShardRange> planes_of(const std::vector<PlaneRun>& runs) {
  std::vector<ShardRange> planes;
  size_t offset = 0;
  for (const PlaneRun& run : runs) {
    for (uint32_t i = 0; i < run.planes; ++i) {
      planes.push_back({offset, run.plane_bytes});
      offset += run.plane_bytes;
    }
  }
  return planes;
}

// Report whether every record of `plan` lies inside exactly one of `planes`.
bool every_record_inside_one_plane(const ShardPlan& plan,
                                   const std::vector<ShardRange>& planes,
                                   size_t total) {
  for (uint32_t i = 0; i < plan.nseg; ++i) {
    const ShardRange r = segment_range(plan, i, total);
    bool inside = false;
    for (const ShardRange& p : planes) {
      if (r.offset >= p.offset && r.offset + r.length <= p.offset + p.length) {
        inside = true;
        break;
      }
    }
    if (!inside || r.length == 0) {
      return false;
    }
  }
  return true;
}

// Report whether two plans produce the same records at the same indices.
bool same_records(const ShardPlan& a, const ShardPlan& b, size_t total) {
  if (a.nseg != b.nseg) {
    return false;
  }
  for (uint32_t i = 0; i < a.nseg; ++i) {
    const ShardRange ra = segment_range(a, i, total);
    const ShardRange rb = segment_range(b, i, total);
    if (ra.offset != rb.offset || ra.length != rb.length) {
      return false;
    }
  }
  return true;
}

// The Mamba/GDN shape the single plane hint cannot describe: an attention
// kernel group and a state kernel group with different plane sizes in the
// same object.
const std::vector<PlaneRun> kHybridRuns = {
    {/*plane_bytes=*/544 * 8 * 128 * 2, /*planes=*/2 * 8},
    {/*plane_bytes=*/96 * 1024, /*planes=*/24},
};

void test_hybrid_kernel_groups_get_plane_aligned_records() {
  std::cout << "\nhybrid kernel groups, one plane size each\n";

  const size_t total = layered_payload_bytes(kHybridRuns);
  const std::vector<ShardRange> planes = planes_of(kHybridRuns);

  const ShardPlan hint_only =
      make_shard_plan(total, kMiB, kMiB, kMiB, kHybridRuns[0].plane_bytes);
  check(hint_only.plane_b == 0 &&
            !every_record_inside_one_plane(hint_only, planes, total),
        "a single plane hint cannot align this object, so it straddles "
        "layers -- the bug being fixed");

  const ShardPlan layered = make_layered_shard_plan(kHybridRuns, kMiB);
  check(layered.runs.size() == 2, "the plan keeps one run per kernel group");
  check(every_record_inside_one_plane(layered, planes, total),
        "every record lies inside one plane of its own kernel group");
  check(records_tile_payload_exactly(layered, total),
        "the records cover the payload exactly once, in order");

  // 1.0625 MiB attention planes split in two; 96 KiB state planes fit whole.
  check(layered.runs[0].seg_b == kHybridRuns[0].plane_bytes / 2,
        "an attention plane over the cap splits into two equal records");
  check(layered.runs[1].seg_b == kHybridRuns[1].plane_bytes,
        "a state plane under the cap is one record");
  check(layered.nseg == (2 * 16) + 24, "record count is the sum over runs");

  const ShardRange first_state = segment_range(layered, 2 * 16, total);
  check(first_state.offset == kHybridRuns[0].plane_bytes * 16 &&
            first_state.length == kHybridRuns[1].plane_bytes,
        "the second run's first record starts where the first run ends");

  bool threw = false;
  try {
    segment_range(layered, layered.nseg, total);
  } catch (const std::out_of_range&) {
    threw = true;
  }
  check(threw, "a record index beyond a layered plan is rejected");
}

void test_a_short_last_piece_in_a_later_run() {
  std::cout << "\na short last piece inside the second run\n";

  // 1001-byte planes against a 300-byte cap cut 251/251/251/248, as in the
  // uniform case, but here they sit after a run of a different size.
  const std::vector<PlaneRun> runs = {{64, 3}, {1001, 2}};
  const size_t total = layered_payload_bytes(runs);
  const ShardPlan plan = make_layered_shard_plan(runs, 300);

  check(plan.nseg == 3 + (4 * 2), "three whole planes then eight pieces");
  const ShardRange last_of_first = segment_range(plan, 3 + 3, total);
  check(last_of_first.offset == 192 + 753 && last_of_first.length == 248,
        "the fourth piece of the first 1001-byte plane is 248 bytes");
  const ShardRange first_of_second = segment_range(plan, 3 + 4, total);
  check(first_of_second.offset == 192 + 1001,
        "the next plane starts on its boundary");
  check(every_record_inside_one_plane(plan, planes_of(runs), total),
        "no record crosses a plane");
}

void test_uniform_runs_are_identical_to_the_uniform_plan() {
  std::cout << "\nuniform models are unchanged\n";

  // Two kernel groups that happen to share a plane size, plus a plane that
  // needs an uneven split: the layered plan must write exactly the records
  // the existing plane-hint path writes, so objects stay readable by readers
  // that predate runs and existing objects stay readable by new ones.
  const size_t plane = 1001;
  const std::vector<PlaneRun> runs = {{plane, 4}, {plane, 2}};
  const size_t total = layered_payload_bytes(runs);

  const ShardPlan layered = make_layered_shard_plan(runs, 300);
  const ShardPlan hinted = make_shard_plan(total, 300, 300, 300, plane);
  check(layered.runs.empty(), "no runs are recorded for a uniform model");
  check(layered.plane_b == hinted.plane_b && layered.seg_b == hinted.seg_b,
        "the uniform form matches the plane-hint plan field for field");
  check(same_records(layered, hinted, total),
        "and every record index covers the same bytes");

  // A small single-plane-size object still gets one record per plane rather
  // than one record holding them all.
  const ShardPlan small = make_layered_shard_plan({{256, 2}}, kMiB);
  check(small.nseg == 2 && small.seg_b == 256,
        "a small multi-plane object is not collapsed into one record");
}

void test_payload_size_picks_the_layout() {
  std::cout << "\npicking a layout by payload size\n";

  const std::vector<PlaneRun> hybrid = {{1024, 4}, {512, 2}};
  const std::vector<PlaneRun> window = {{2048, 2}};
  const auto layouts = record_layouts_by_payload({hybrid, window, hybrid});
  check(layouts.size() == 2, "each distinct payload size has a layout");
  check(layouts.count(5120) == 1 && layouts.at(5120).size() == 2,
        "the hybrid group's 5120-byte payload maps to its two runs");
  check(layouts.count(4096) == 1, "the window group's payload maps too");

  // 4096 bytes laid out two ways: the writer cannot tell which a payload of
  // that size is, so it must not pick either.
  const std::vector<PlaneRun> clash = {{1024, 2}, {512, 4}};
  const auto ambiguous = record_layouts_by_payload({window, clash, hybrid});
  check(ambiguous.count(4096) == 0,
        "a size two groups share with different runs has no layout");
  check(ambiguous.count(5120) == 1, "other sizes are unaffected");

  const ShardPlan chosen =
      choose_shard_plan(5120, layouts, kMiB, kMiB, kMiB, /*plane_bytes=*/0);
  check(chosen.runs.size() == 2 && chosen.nseg == 6,
        "a known payload size is written with its layered plan");
  const ShardPlan unknown =
      choose_shard_plan(4096, ambiguous, kMiB, kMiB, kMiB, /*plane_bytes=*/0);
  check(unknown.runs.empty() && unknown.plane_b == 0 && unknown.nseg == 1,
        "an ambiguous size falls back to the byte-count plan");

  bool threw = false;
  try {
    record_layouts_by_payload({{{1024, 0}}});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a run with no planes is rejected");
}

void test_runs_round_trip_through_the_meta_record() {
  std::cout << "\nencoding runs for the meta record\n";

  const ShardPlan plan = make_layered_shard_plan(kHybridRuns, kMiB);
  const std::string encoded = encode_shard_runs(plan.runs);
  check(encoded == "1114112:557056:16,98304:98304:24",
        "runs encode as plane_b:seg_b:planes, comma separated");

  ShardPlan decoded;
  decoded.nseg = plan.nseg;
  decoded.runs = decode_shard_runs(encoded);
  const size_t total = layered_payload_bytes(kHybridRuns);
  check(same_records(decoded, plan, total),
        "a reader recovers every record's range from the meta record alone");

  bool accepted = true;
  try {
    require_consistent_runs(decoded, total);
  } catch (const std::invalid_argument&) {
    accepted = false;
  }
  check(accepted, "runs written by the writer pass the reader's check");

  // What a reader must refuse: runs decoded intact that describe a different
  // object, from a corrupt or mismatched meta record.
  struct Corrupt {
    std::string what;
    ShardPlan plan;
    size_t total;
  };
  ShardPlan wrong_count = decoded;
  wrong_count.nseg += 1;
  ShardPlan wrong_seg = decoded;
  wrong_seg.runs[1].seg_b = kHybridRuns[1].plane_bytes / 2;
  const std::vector<Corrupt> corrupt = {
      {"a size the runs do not cover", decoded, total + 1},
      {"a record count the runs do not imply", wrong_count, total},
      {"a record size that changes the count", wrong_seg, total},
  };
  for (const Corrupt& c : corrupt) {
    bool threw = false;
    try {
      require_consistent_runs(c.plan, c.total);
    } catch (const std::invalid_argument&) {
      threw = true;
    }
    check(threw, "the reader refuses runs with " + c.what);
  }

  for (const std::string bad :
       {"", "1:1", "1:1:1:1", "0:1:1", "1:0:1", "1:1:0", "2:3:1", "a:1:1",
        "1:1:1,", ",1:1:1", "1:-1:1", "4:4:99999999999",
        "99999999999999999999999:1:1"}) {
    bool threw = false;
    try {
      decode_shard_runs(bad);
    } catch (const std::invalid_argument&) {
      threw = true;
    }
    check(threw, "malformed runs '" + bad + "' are rejected");
  }
}

void test_invalid_layered_input_is_rejected() {
  std::cout << "\ninvalid layered input\n";

  const std::vector<std::vector<PlaneRun>> bad_runs = {
      {}, {{0, 1}}, {{1024, 0}}};
  for (const std::vector<PlaneRun>& runs : bad_runs) {
    bool threw = false;
    try {
      make_layered_shard_plan(runs, kMiB);
    } catch (const std::invalid_argument&) {
      threw = true;
    }
    check(threw, "runs with no planes or a zero plane size are rejected");
  }

  bool threw = false;
  try {
    make_layered_shard_plan({{1024, 1}}, 0);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a zero record cap is rejected");
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
    test_hybrid_kernel_groups_get_plane_aligned_records();
    test_a_short_last_piece_in_a_later_run();
    test_uniform_runs_are_identical_to_the_uniform_plan();
    test_payload_size_picks_the_layout();
    test_runs_round_trip_through_the_meta_record();
    test_invalid_layered_input_is_rejected();
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
