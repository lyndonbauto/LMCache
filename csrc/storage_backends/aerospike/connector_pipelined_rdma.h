// SPDX-License-Identifier: Apache-2.0
#pragma once

#ifdef LMCACHE_AEROSPIKE_RDMA

// Optional pipelined kv-sink fetch path for AerospikeNativeConnector.
//
// Lives in its own translation unit so a default Aerospike build without
// LMCACHE_AEROSPIKE_RDMA never links libibverbs. The TCP get/set path in
// connector.cpp does not include this header.

  #include "l1_rdma_registration.h"
  #include "pipelined_fetch_issue.h"
  #include "pipelined_fetch_pool.h"
  #include "pipelined_fetch_session.h"
  #include "rdma_context.h"
  #include "slot_planner.h"

  #include <aerospike/aerospike.h>

  #include <cstdint>
  #include <functional>
  #include <map>
  #include <memory>
  #include <mutex>
  #include <string>
  #include <vector>

namespace lmcache {
namespace connector {

class AerospikePipelinedRdmaDriver {
 public:
  AerospikePipelinedRdmaDriver(L1RdmaRegistration registration,
                               std::string namespace_name,
                               size_t max_record_bytes);

  // Thread safety: takes `mu_`.
  //
  // True when verbs registration succeeded, every node has a connected queue
  // pair with notifications armed, layouts are configured, and the fetch
  // pool exists.
  bool is_ready() const;

  // Why pipelined fetch is unavailable: the last initialization failure, or
  // else a registered layout whose chunk does not fit in one window.
  //
  // Thread safety: takes `mu_`.
  std::string init_error_message() const;

  // Register the L1 slab, open verbs resources, and fan out kv-sink-register.
  //
  // Thread safety: call from one thread during connector startup. Throws on
  // fatal verbs failure after recording the reason for init_error_message().
  void initialize(aerospike* client);

  // Replace the slot planner used for subsequent requests. If one chunk of
  // `layouts` does not fit in a window, pipelined fetch stays unavailable
  // until layouts that fit are set; init_error_message() says why.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error if any request is
  // active.
  void set_object_group_layouts(std::vector<rdma::ObjectGroupLayout> layouts);

  // Begin a pipelined fetch in the window its placements are in, issue each
  // node's info command through ``send_info``, and feed acknowledgements into
  // the pool. Up to one fetch per window runs at a time.
  //
  // Thread safety: ``send_info`` runs without holding ``mu_``, so fetches in
  // other windows proceed meanwhile. When it throws for a node, that node's
  // slots are marked unservable. Any other failure after the request began
  // abandons that request before propagating. Throws std::runtime_error when
  // pipelined fetch is not ready or the window is busy.
  uint16_t issue_pipelined_fetch(
      const rdma::PipelinedNodeInfoSender& send_info,
      const std::vector<rdma::ChunkPlacement>& placements,
      const std::vector<rdma::ChunkNodeBinding>& chunk_nodes,
      const std::vector<rdma::SlotDigest>& slot_digests);

  // Same as issue_pipelined_fetch(), for slots planned by the caller; see
  // rdma::PipelinedFetchSession::begin_request_from_slots.
  //
  // Thread safety: as issue_pipelined_fetch(). Throws
  // rdma::PlanTooLargeError when the plan exceeds max_slots_per_request().
  uint16_t issue_planned_fetch(const rdma::PipelinedNodeInfoSender& send_info,
                               const std::vector<rdma::PlannedSlot>& slots);

  // Most slots one request may carry: one window's share of the device's
  // notification depth, or 0 when pipelined fetch is not ready.
  //
  // Thread safety: takes `mu_`.
  uint32_t max_slots_per_request() const;

  // Thread safety: polls the completion queue outside `mu_`, then takes `mu_`
  // to route each immediate to its window's request.
  void poll_notifications();

  // Whether any window has an active request.
  //
  // Thread safety: takes `mu_`.
  bool has_active_request() const;

  // Whether every slot of `layer_id` in request `request_generation` landed.
  // False when not ready, when that request is not active, and for
  // generation 0.
  //
  // Thread safety: drains notifications outside `mu_`, then takes `mu_`.
  bool is_layer_ready(uint32_t layer_id, uint16_t request_generation) const;

  // Declined or lost layers of request `generation`; empty if not active.
  //
  // Thread safety: takes `mu_`.
  std::vector<uint32_t> unservable_layers(uint16_t generation) const;

  // Drop request `generation` after it completed.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error if it is not
  // active.
  void finish_request(uint16_t generation);

  // Drop request `generation` without waiting; no-op if it is not active.
  //
  // Thread safety: takes `mu_`.
  void abandon_request(uint16_t generation);

 private:
  // Begin a request with `begin` under `mu_`, then send its commands without
  // the lock and feed replies back; abandons the request if anything throws.
  uint16_t begin_and_send(
      const rdma::PipelinedNodeInfoSender& send_info,
      const std::function<uint16_t(rdma::PipelinedFetchPool&)>& begin);

  void ensure_pool();
  uint32_t desired_notification_depth() const;

  L1RdmaRegistration registration_;
  std::string namespace_name_;
  size_t max_record_bytes_;

  mutable std::mutex mu_;
  bool initialized_ = false;
  bool fabric_ready_ = false;
  std::string init_error_;
  std::string layout_error_;

  // Carried across pool replacements so a late write never meets a reused
  // generation.
  std::vector<uint16_t> generation_counters_;

  std::unique_ptr<rdma::RdmaContext> context_;
  rdma::NodeRegistry registry_;
  std::unique_ptr<rdma::SlotPlanner> planner_;
  std::unique_ptr<rdma::PipelinedFetchPool> pool_;

  uint32_t max_notification_slots_ = 0;
};

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
