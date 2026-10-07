// SPDX-License-Identifier: Apache-2.0
#pragma once

// Bookkeeping for pipelined kv-sink fetches, with no I/O.
//
// A fetch reads one request's records straight into an L1 RDMA window: every
// record is a batch-read row carrying a sink destination, and the server
// writes its value there by RDMA before answering the row. So a row's result
// *is* its completion -- AEROSPIKE_OK means the bytes are in the window --
// and a layer is resident once every row carrying part of it has succeeded.
//
// This table owns that accounting, and nothing else:
//
//   - one active fetch per window, named by a non-zero generation;
//   - the fetch split into one batch per layer, in plan order, each tagged
//     with its position as the server-side priority, so layer 0 is placed
//     first even though every layer is in flight at once;
//   - per-slot results folded into per-layer readiness;
//   - results for a fetch that is no longer active dropped, so a batch still
//     in flight after abandon cannot be credited to the window's next fetch;
//   - per window, whether every batch begun in it has ended with no write
//     left on the wire, so an abandoned window can be reused before its
//     timeout-based quarantine ends.
//
// Issuing the batches is the driver's job (connector_sink_fetch.h). Keeping
// this class free of the client means every rule above runs in the logic
// harness, without a cluster or a device.

#include <cstddef>
#include <cstdint>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace lmcache {
namespace connector {
namespace sink {

// Reserved generation meaning "no fetch". Never allocated to a live request.
// Matches NO_GENERATION in lmcache/v1/layerwise/contract.py.
constexpr uint16_t kNoGeneration = 0;

// Most slots one fetch may carry. Bounded so a plan cannot grow the table
// without limit; the server applies no per-request cap of its own.
constexpr uint32_t kMaxSlotsPerRequest = 1u << 16;

// A plan with more slots than one fetch may carry.
//
// Distinct from other begin() failures because a smaller plan can still
// succeed, whereas an unready backend cannot.
class PlanTooLargeError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

// One record to read into the window, as the caller planned it.
struct SinkSlot {
  // Aerospike user key of the record, in the connector's namespace and set.
  std::string record_key;
  uint32_t layer_id = 0;
  // Relative to the base of the registered window range.
  size_t offset = 0;
  // The record's value size; the server refuses a record of any other size.
  size_t length = 0;
};

// How a batch from begin() ended, as far as writes into its window go.
enum class BatchEnd : uint8_t {
  // Every row the batch sent got a definite answer from the server, so none
  // of its writes can still land. Also a batch that was never sent.
  kSettled,
  // A row timed out, got no answer, or the batch failed outright: one of
  // its writes may still land.
  kMayStillWrite,
};

// The rows of one layer, sent as one batch read.
struct LayerBatch {
  uint16_t generation = kNoGeneration;
  // Unique for the table's lifetime, unlike the generation, which wraps.
  uint64_t token = 0;
  // The window the batch writes into.
  uint32_t window = 0;
  uint32_t layer_id = 0;
  // The layer's position in the plan: 0 for the first layer. Lower is
  // placed first by the server.
  uint32_t priority = 0;
  // Indices into the plan's slots, in plan order.
  std::vector<uint32_t> slot_indices;
};

// A fetch that begin() accepted: its generation and the batches to send.
struct BegunFetch {
  uint16_t generation = kNoGeneration;
  std::vector<LayerBatch> batches;
};

// Thread safety: every public method takes an internal lock and may be
// called from any thread.
class SinkFetchTable {
 public:
  // The registered range holds `window_count` windows of `window_bytes`.
  //
  // Throws std::invalid_argument if `window_count` or `window_bytes` is 0,
  // or `max_slots_per_request` is 0 or above kMaxSlotsPerRequest.
  SinkFetchTable(size_t window_bytes, uint32_t window_count,
                 uint32_t max_slots_per_request = kMaxSlotsPerRequest);

  // Begin a fetch of exactly `slots` in the window of its first slot.
  //
  // Returns the fetch's generation and one batch per layer, in the order the
  // layers first appear in `slots`.
  //
  // Throws PlanTooLargeError if there are more than max_slots_per_request()
  // slots; std::invalid_argument if `slots` is empty, a key is empty, a
  // length is 0, or a slot leaves the first slot's window; and
  // std::runtime_error if that window already has an active fetch.
  BegunFetch begin(const std::vector<SinkSlot>& slots);

  // Record the result of slot `slot_index` of fetch `generation`.
  //
  // A non-zero `token` must match the batch's token too, which is how a row
  // result from an abandoned fetch is told apart from one of its window's
  // next fetch after the generation wrapped. A result for a fetch that is
  // not active, or for a slot not in it, is ignored. A slot that failed
  // stays failed: a later success does not revive it.
  void on_slot_result(uint16_t generation, uint32_t slot_index, bool landed,
                      uint64_t token = 0);

  // Whether fetch `generation` is active, and, for a non-zero `token`, is
  // the fetch that batch belongs to.
  bool is_active(uint16_t generation, uint64_t token = 0) const;

  // Whether any window has an active fetch.
  bool has_active_request() const;

  // Whether every slot of `layer_id` in fetch `generation` landed. False
  // when that fetch is not active, for generation 0, and for a layer that is
  // not in the plan.
  bool is_layer_ready(uint32_t layer_id, uint16_t generation) const;

  // Layers of fetch `generation` with a failed slot, ascending. Empty when
  // that fetch is not active.
  std::vector<uint32_t> unservable_layers(uint16_t generation) const;

  // Drop fetch `generation`. Throws std::runtime_error if it is not active.
  void finish(uint16_t generation);

  // Drop fetch `generation`; no-op if it is not active.
  void abandon(uint16_t generation);

  // Record that a batch begun in `window` will not be sent again: it was
  // read, skipped because its fetch was no longer active, or failed. Every
  // batch begin() returns must be reported exactly once.
  //
  // Throws std::out_of_range if `window` is not a window of this table, and
  // std::logic_error if the window has no batch outstanding.
  void on_batch_done(uint32_t window, BatchEnd end);

  // Whether no batch begun in `window` is still outstanding, and none ended
  // with BatchEnd::kMayStillWrite since the window's last begin(). A window
  // that never had a fetch is settled.
  //
  // Throws std::out_of_range if `window` is not a window of this table.
  bool window_settled(uint32_t window) const;

  uint32_t max_slots_per_request() const { return max_slots_; }

 private:
  enum class SlotState : uint8_t { kPending, kLanded, kFailed };

  struct Fetch {
    uint16_t generation = kNoGeneration;
    uint64_t first_token = 0;
    uint64_t last_token = 0;
    std::vector<SlotState> slots;
    std::vector<uint32_t> slot_layer;
    std::map<uint32_t, uint32_t> expected_per_layer;
    std::map<uint32_t, uint32_t> landed_per_layer;
    std::map<uint32_t, uint32_t> failed_per_layer;
  };

  // Returns the active fetch with `generation`, or nullptr. Call with mu_.
  Fetch* find(uint16_t generation);
  const Fetch* find(uint16_t generation) const;
  uint16_t next_generation();

  const size_t window_bytes_;
  const uint32_t window_count_;
  const uint32_t max_slots_;

  mutable std::mutex mu_;
  // Active fetch per window; generation kNoGeneration when idle.
  std::vector<Fetch> windows_;
  // Per window, kept apart from Fetch because abandon() clears the fetch
  // while its batches are still queued or on the wire.
  std::vector<uint32_t> outstanding_batches_;
  std::vector<uint8_t> may_still_write_;
  uint16_t last_generation_ = kNoGeneration;
  uint64_t next_token_ = 1;
};

}  // namespace sink
}  // namespace connector
}  // namespace lmcache
