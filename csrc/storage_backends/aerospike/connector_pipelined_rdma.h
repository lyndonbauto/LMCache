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
  #include "pipelined_fetch_session.h"
  #include "rdma_context.h"
  #include "slot_planner.h"

  #include <aerospike/aerospike.h>

  #include <cstdint>
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
  // pair with notifications armed, layouts are configured, and a session
  // exists.
  bool is_ready() const;

  // Last initialization failure, when pipelined fetch is unavailable.
  //
  // Thread safety: takes `mu_`.
  std::string init_error_message() const;

  // Register the L1 slab, open verbs resources, and fan out kv-sink-register.
  //
  // Thread safety: call from one thread during connector startup. Throws on
  // fatal verbs failure after recording the reason for init_error_message().
  void initialize(aerospike* client);

  // Replace the slot planner used for subsequent requests.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error if a request is
  // active.
  void set_object_group_layouts(std::vector<rdma::ObjectGroupLayout> layouts);

  // Begin a pipelined fetch, issue each node's info command through
  // ``send_info``, and feed acknowledgements into the session.
  //
  // Thread safety: ``send_info`` runs without holding ``mu_``. When it throws
  // for a node, that node's slots are marked unservable. Any other failure
  // after ``begin_request`` abandons the active request before propagating.
  // Throws std::runtime_error when pipelined fetch is not ready.
  uint16_t issue_pipelined_fetch(
      const rdma::PipelinedNodeInfoSender& send_info,
      const std::vector<rdma::ChunkPlacement>& placements,
      const std::vector<rdma::ChunkNodeBinding>& chunk_nodes,
      const std::vector<rdma::SlotDigest>& slot_digests);

  // Thread safety: polls the completion queue outside `mu_`, then takes `mu_`
  // to feed the session.
  void poll_notifications();

  // Thread safety: takes `mu_`.
  bool has_active_request() const;

  // Thread safety: drains notifications outside `mu_`, then takes `mu_`.
  bool is_layer_ready(uint32_t layer_id, uint16_t request_generation = 0) const;

  // Thread safety: takes `mu_`.
  std::vector<uint32_t> unservable_layers() const;

  // Thread safety: takes `mu_`.
  void finish_request();

  // Thread safety: takes `mu_`.
  void abandon_request();

 private:
  void ensure_session();
  uint32_t notification_depth_cap() const;

  L1RdmaRegistration registration_;
  std::string namespace_name_;
  size_t max_record_bytes_;

  mutable std::mutex mu_;
  bool initialized_ = false;
  bool fabric_ready_ = false;
  std::string init_error_;

  uint16_t next_generation_ = 0;

  std::unique_ptr<rdma::RdmaContext> context_;
  rdma::NodeRegistry registry_;
  std::unique_ptr<rdma::SlotPlanner> planner_;
  std::unique_ptr<rdma::PipelinedFetchSession> session_;
};

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
