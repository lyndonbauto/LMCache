// SPDX-License-Identifier: Apache-2.0
#pragma once

// Runs one pipelined fetch per RDMA window at the same time, with no I/O.
//
// A PipelinedFetchSession holds one request. Concurrent retrieves each lease
// their own window, so this pool keeps one session per window and routes
// every call to the right one:
//
// - a new request goes to the session of the window its first slot is in;
// - everything after that is keyed by generation. Window w's session
//   allocates only generations g with (g - 1) % window_count == w, so the
//   generation alone names the window, and an immediate on the shared
//   completion queue reaches its session without a lookup table;
// - each session may carry at most notification_depth / window_count slots.
//
// == Why the receive budget is split statically ==
//
// Every node's queue pair reports into one completion queue of
// notification_depth entries, and overflowing it is a fatal device error,
// not a refusal. A write can still land after its fetch was abandoned, so
// the budget has to cover those writes too. With a fixed share per window,
// a window's late writes fit in its own share, and the leaser does not hand
// that window out again until they are over (the quarantine). So the total
// in flight never exceeds the depth, whatever mix of fetches is running.

#include "pipelined_fetch_session.h"

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// Most windows one pool runs. Keeps at least 255 generations per window
// before one is reused.
constexpr uint32_t kMaxFetchWindows = 256;

class PipelinedFetchPool {
 public:
  // One session per window. `planner` and `registry` must outlive the pool
  // and must not change while it exists.
  //
  // Throws std::invalid_argument if `window_count` is 0 or above
  // kMaxFetchWindows, if `window_bytes` is 0, or if `notification_depth`
  // leaves a window no receive slot.
  PipelinedFetchPool(const SlotPlanner& planner, const NodeRegistry& registry,
                     const std::string& namespace_name, size_t max_record_bytes,
                     size_t max_write_bytes, size_t window_bytes,
                     uint32_t window_count, uint32_t notification_depth);

  // Thread safety: immutable after construction.
  uint32_t window_count() const {
    return static_cast<uint32_t>(sessions_.size());
  }

  // Most slots one request may carry: this window's share of the depth.
  //
  // Thread safety: immutable after construction.
  uint32_t max_slots_per_request() const { return max_slots_per_request_; }

  // Window whose session allocated `generation`. Meaningless for
  // kNoGeneration.
  //
  // Thread safety: immutable after construction.
  uint32_t window_of_generation(uint16_t generation) const;

  // Whether any window has an active request.
  //
  // Thread safety: takes `mu_`.
  bool has_active_request() const;

  // Whether `generation` is the active request of its window.
  //
  // Thread safety: takes `mu_`.
  bool is_active(uint16_t generation) const;

  // Begin a request planned from chunk placements in the window of the
  // lowest placement offset. See PipelinedFetchSession::begin_request.
  //
  // Thread safety: takes `mu_`. Throws std::invalid_argument if `placements`
  // is empty or starts past the last window, std::runtime_error if that
  // window already has an active request, and whatever the session throws.
  uint16_t begin_request(const std::vector<ChunkPlacement>& placements,
                         const std::vector<ChunkNodeBinding>& chunk_nodes,
                         const std::vector<SlotDigest>& slot_digests);

  // Begin a caller-planned request in the window of its first slot. See
  // PipelinedFetchSession::begin_request_from_slots.
  //
  // Thread safety: takes `mu_`. Throws std::invalid_argument if `slots` is
  // empty or its first slot is past the last window, std::runtime_error if
  // that window already has an active request, and whatever the session
  // throws, including PlanTooLargeError.
  uint16_t begin_request_from_slots(const std::vector<PlannedSlot>& slots);

  // Commands for the request `generation`; empty if it is not active.
  //
  // Thread safety: takes `mu_`.
  std::vector<std::pair<std::string, std::string>> pipelined_fetch_commands(
      uint16_t generation) const;

  // Feed one command's acknowledgement to the request `generation`. Ignored
  // if it is no longer active.
  //
  // Thread safety: takes `mu_`. Throws as
  // PipelinedFetchSession::on_node_reply.
  void on_node_reply(const std::string& node_name, const std::string& command,
                     const std::string& reply, uint16_t generation);

  // Route each immediate to the session of its generation's window. The
  // session drops it unless that generation is active there.
  //
  // Thread safety: takes `mu_`.
  void on_notifications(const std::vector<uint32_t>& immediates);

  // Whether every slot of `layer_id` in request `generation` landed. False
  // if the request is not active, and for kNoGeneration.
  //
  // Thread safety: takes `mu_`.
  bool is_layer_ready(uint32_t layer_id, uint16_t generation) const;

  // Declined or lost layers of request `generation`; empty if not active.
  //
  // Thread safety: takes `mu_`.
  std::vector<uint32_t> unservable_layers(uint16_t generation) const;

  // Drop request `generation` after it completed.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error if it is not
  // active.
  void finish_request(uint16_t generation);

  // Drop request `generation` without waiting. No-op if it is not active,
  // so error paths can unwind without checking first.
  //
  // Thread safety: takes `mu_`.
  void abandon_request(uint16_t generation);

  // Every window's next generation, in window order.
  //
  // Thread safety: takes `mu_`.
  std::vector<uint16_t> generation_counters() const;

  // Continue each window's generations from `counters`, as returned by
  // generation_counters() of a pool with the same window count, so a
  // replacement pool never reuses a generation a late write may still carry.
  //
  // Thread safety: takes `mu_`. Throws std::invalid_argument on a size
  // mismatch.
  void restore_generation_counters(const std::vector<uint16_t>& counters);

 private:
  // Session of `window`, after checking it is idle. Holds `mu_`.
  PipelinedFetchSession& idle_session(size_t window);
  // Session owning `generation` if that request is active there, else null.
  // Holds `mu_`.
  PipelinedFetchSession* active_session(uint16_t generation) const;

  size_t window_bytes_;
  uint32_t max_slots_per_request_;
  std::vector<std::unique_ptr<PipelinedFetchSession>> sessions_;
  mutable std::mutex mu_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
