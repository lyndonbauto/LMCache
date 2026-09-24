// SPDX-License-Identifier: Apache-2.0
#pragma once

// How a payload is cut into Aerospike records.
//
// Sharding used to be pure byte counting: divide the payload by a target
// record size and tile it. That is correct for opaque blobs, and wrong for a
// KV cache payload that a layer-pipelined reader has to serve one layer at a
// time, because the cut points land wherever the arithmetic puts them.
//
// == Why a record must not straddle a plane ==
//
// An object group's payload is a concatenation of per-kernel-group tensors,
// and within each the K/V plane is the outermost dimension:
// `[kg0 K][kg0 V][kg1 K][kg1 V]`. One model layer therefore owns one byte
// range inside each plane -- `kv_size` disjoint ranges, not one.
//
// If a record spans two planes, it belongs to two layers at once. That breaks
// the pipeline in two places: a layer is not ready until every record
// touching it has landed, so the layer inherits its neighbour's latency; and
// `FetchSlot` carries a single `layer_id`, which stops being well defined.
//
// Measured against the unified block sizes that Mamba/GDN hybrids force
// (544, 784 and 944 tokens -- none of them powers of two), byte-count
// sharding makes a layer wait for substantially more bytes than it owns:
//
//   tokens/chunk | bytes a layer waits for, over its own size
//   544          | +88%
//   784          | +31%
//   944          | +8%
//
// The cost is not wasted bandwidth -- every record is consumed by some layer,
// so aggregate bytes read equal bytes needed. It inflates *first-layer*
// latency, which is the part of the transfer that pipelining can never hide.
//
// == The rule ==
//
// A record is sized from the plane rather than from the record cap:
//
//   pieces_per_plane = ceil(plane_bytes / cap)
//   seg_bytes        = ceil(plane_bytes / pieces_per_plane)
//
// so a record holds either exactly one plane, or an equal fraction of one.
// The weaker rule "either size must divide the other" also avoids straddles
// and would permit one 1 MiB record holding two 512 KiB planes; it is not
// taken here, because sizing from the plane makes a straddle
// *unrepresentable* rather than merely unlikely. No alignment predicate to
// get wrong, no record shared between two layers' readiness counts.
//
// The cost is record count -- records are deliberately smaller than the cap
// permits, 64 rather than 32 on a 32-layer default model. The M0 sweep
// measured smaller records as *faster* at a fixed object size (1 MiB records
// at 1880 MB/s against 8 MiB at 1659 MB/s, since a larger record coarsens the
// unit of concurrency and reduces device fanout), and `max-record-size` is a
// cap rather than a fixed allocation, so an undersized record wastes no
// space.
//
// A consequence worth knowing: under this rule the record cap stops being a
// tuning knob for any payload whose planes already fit under it. It binds
// only when a single plane exceeds it.

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace lmcache {
namespace connector {

// One kernel group's share of a record layout: a run of equal-sized planes.
//
// A kernel group is uniform by construction, so its planes are all one size;
// kernel groups within an object group need not agree with each other.
// `planes` is `kv_size * num_layers` for the group.
struct PlaneRun {
  size_t plane_bytes = 0;
  uint32_t planes = 0;
};

inline bool operator==(const PlaneRun& a, const PlaneRun& b) {
  return a.plane_bytes == b.plane_bytes && a.planes == b.planes;
}

// A run as the writer cut it: its plane size, the record size it cut each
// plane into, and how many planes it holds. Persisted in the meta record, so
// the reader does not have to know the model to recover the layout.
struct ShardRun {
  size_t plane_b = 0;
  size_t seg_b = 0;
  uint32_t planes = 0;
};

// How a payload is divided into records.
//
// Segment sizes are uniform within a plane, so a plan whose planes are all
// one size stays three numbers and is recoverable from the meta record
// without storing a per-segment table. A plan whose kernel groups disagree
// adds one `ShardRun` per kernel group instead.
struct ShardPlan {
  // Number of records the payload occupies.
  uint32_t nseg = 1;
  // Bytes per record. The last record of each plane may be shorter, when
  // `plane_b` is not an exact multiple of this. Zero when `runs` is set.
  size_t seg_b = 0;
  // Bytes in one K/V plane, or 0 when the payload has no single plane size.
  // Non-zero is what makes a uniform plan plane-aligned; see segment_range().
  size_t plane_b = 0;
  // Per-kernel-group runs, in payload order. Empty unless the payload's
  // kernel groups disagree on their plane size; when set, it alone describes
  // the layout and `seg_b` and `plane_b` are 0.
  std::vector<ShardRun> runs;
};

// Byte range one record covers.
struct ShardRange {
  size_t offset = 0;
  size_t length = 0;
};

// Records per plane, or 1 when `plan` has no plane structure.
uint32_t pieces_per_plane(const ShardPlan& plan);

// Bytes in one record of a plane that is sharded plane-aligned.
//
//   pieces    = ceil(plane_bytes / max_record_bytes)
//   seg_bytes = ceil(plane_bytes / pieces)
//
// Exposed separately from make_shard_plan because the read side needs the
// same answer without a payload size. A pipelined fetch asks a server for one
// record per write, so the RDMA slot size has to be this, and deriving it
// from a copy of the formula is how the two drift.
//
// Depends only on the plane and the cap, not on how many planes the payload
// holds, which is what makes it shareable: the plane-aligned rule is "a record
// holds one plane, or an equal fraction of one", and that is per-plane by
// construction.
//
// Throws std::invalid_argument if either argument is 0.
size_t plane_segment_bytes(size_t plane_bytes, size_t max_record_bytes);

// Build a plan for a `payload_bytes` payload.
//
// `plane_bytes` is the alignment hint: pass the size of one K/V plane to get a
// plane-aligned plan, or 0 when the payload has no plane structure. The hint
// is honoured only when it divides the payload, since a payload that is not a
// whole number of planes means the planes are not all the same size -- the
// kernel groups in this object group disagree -- and one uniform stride
// cannot describe it. In that case the plan falls back to byte-count
// sharding, which the caller can detect because `plane_b` comes back 0
// despite a non-zero hint. Callers that care should count those, since silent
// misalignment is the failure this module exists to prevent.
//
// `target_segment_bytes` and `single_record_threshold_bytes` apply only to the
// byte-count path; a plane-aligned plan takes its size from the plane and the
// cap alone.
//
// Throws std::invalid_argument if `payload_bytes` or `max_record_bytes` is 0,
// and std::runtime_error if the payload cannot be sharded within
// `max_record_bytes`.
ShardPlan make_shard_plan(size_t payload_bytes, size_t target_segment_bytes,
                          size_t max_record_bytes,
                          size_t single_record_threshold_bytes,
                          size_t plane_bytes);

// Build a plan that keeps every record inside one plane of its own kernel
// group, for an object whose kernel groups are described by `runs`.
//
// This is the rule make_shard_plan() applies, applied per kernel group
// rather than with one plane size for the whole payload. It exists because
// that one number cannot describe a hybrid model: a Mamba/GDN object group
// holds kernel groups with different plane sizes, so a single hint fails to
// divide the payload and make_shard_plan() falls back to byte-count
// sharding, whose records straddle layers.
//
// Records are numbered in payload order: every piece of the first run's
// first plane, then its second plane, and so on, then the next run. When all
// runs share a plane size the result is exactly the uniform plan
// make_shard_plan() would build from that size -- same records, same
// indices, `runs` left empty -- so objects written either way are
// interchangeable and readers that predate runs can still read uniform
// models.
//
// Plane-aligned records are kept even when the whole object would fit in one
// record, for the same reason make_shard_plan() checks alignment before its
// single-record fast path: one record holding two planes belongs to two
// layers.
//
// Throws std::invalid_argument if `runs` is empty, a run has no planes or a
// zero plane size, or `max_record_bytes` is 0; std::runtime_error if the
// record count overflows uint32_t.
ShardPlan make_layered_shard_plan(const std::vector<PlaneRun>& runs,
                                  size_t max_record_bytes);

// Bytes an object described by `runs` occupies.
size_t layered_payload_bytes(const std::vector<PlaneRun>& runs);

// The record layout the writer should use for each payload size.
//
// The writer is handed a key and a byte count, not an object group, so it
// picks a layout by payload size. `object_groups` holds each object group's
// runs. A size that two object groups share with *different* runs is left
// out: the writer cannot tell which layout a payload of that size has, and
// guessing would put record boundaries inside another group's layers. Such
// payloads fall back to make_shard_plan(), and a layerwise reader must refuse
// them rather than assume alignment.
//
// Throws std::invalid_argument under the same conditions as
// make_layered_shard_plan() for any group.
std::map<size_t, std::vector<PlaneRun>> record_layouts_by_payload(
    const std::vector<std::vector<PlaneRun>>& object_groups);

// The plan the writer uses for a `payload_bytes` object.
//
// Uses the layered plan when `layouts` (from record_layouts_by_payload())
// has an entry for this size, and make_shard_plan() with `plane_bytes` as the
// uniform hint otherwise. Kept here rather than in the connector so that
// anything predicting the writer's records calls the same code the writer
// does.
//
// Throws what make_layered_shard_plan() or make_shard_plan() throws.
ShardPlan choose_shard_plan(
    size_t payload_bytes,
    const std::map<size_t, std::vector<PlaneRun>>& layouts,
    size_t target_segment_bytes, size_t max_record_bytes,
    size_t single_record_threshold_bytes, size_t plane_bytes);

// Encode `runs` for the meta record, as `plane_b:seg_b:planes` joined by
// commas, e.g. "1024:1024:4,4096:4096:2".
std::string encode_shard_runs(const std::vector<ShardRun>& runs);

// Inverse of encode_shard_runs().
//
// Throws std::invalid_argument if `encoded` is empty or malformed, or any run
// has a zero field -- a corrupt layout must fail the read, not produce ranges.
std::vector<ShardRun> decode_shard_runs(const std::string& encoded);

// Check that a plan's runs describe exactly `total_bytes` in `plan.nseg`
// records, as a reader must before trusting runs decoded from a meta record.
// A plan without runs passes trivially.
//
// Throws std::invalid_argument if the runs cover a different size or imply
// a different record count.
void require_consistent_runs(const ShardPlan& plan, size_t total_bytes);

// Byte range record `index` of `plan` covers within a `total_bytes` payload.
//
// Both the write and the read path must derive ranges from here rather than
// recomputing `index * seg_b`, which is only correct while records tile the
// payload uniformly.
//
// Throws std::out_of_range if `index` is not below `plan.nseg`.
ShardRange segment_range(const ShardPlan& plan, uint32_t index,
                         size_t total_bytes);

}  // namespace connector
}  // namespace lmcache
