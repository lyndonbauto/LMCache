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

namespace lmcache {
namespace connector {

// How a payload is divided into records.
//
// Segment sizes are uniform within a plane, so the plan stays three numbers
// and the segment layout is recoverable from the meta record without storing
// a per-segment table.
struct ShardPlan {
  // Number of records the payload occupies.
  uint32_t nseg = 1;
  // Bytes per record. The last record of each plane may be shorter, when
  // `plane_b` is not an exact multiple of this.
  size_t seg_b = 0;
  // Bytes in one K/V plane, or 0 when the payload has no plane structure and
  // records tile it uniformly. Non-zero is what makes the plan
  // plane-aligned; see segment_range().
  size_t plane_b = 0;
};

// Byte range one record covers.
struct ShardRange {
  size_t offset = 0;
  size_t length = 0;
};

// Records per plane, or 1 when `plan` has no plane structure.
uint32_t pieces_per_plane(const ShardPlan& plan);

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
