// SPDX-License-Identifier: Apache-2.0
#pragma once

#ifdef LMCACHE_AEROSPIKE_RDMA

// Optional pipelined kv-sink fetch path for AerospikeNativeConnector.
//
// Lives in its own translation unit so a default Aerospike build without
// LMCACHE_AEROSPIKE_RDMA never links libibverbs. The TCP get/set path in
// connector.cpp does not include this header.

  #include "l1_rdma_registration.h"
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

  // Thread safety: safe to call from any thread; serialized internally.
  bool is_ready() const;

  // Register the L1 slab, open the verbs resources, and fan out
  // kv-sink-register. Idempotent once ready. Throws on fatal verbs or
  // cluster-wide info failure.
  //
  // Thread safety: call from one thread during connector startup.
  void initialize(aerospike* client);

  // Replace the slot planner used for subsequent requests.
  //
  // Thread safety: call before begin_request, from one thread.
  void set_object_group_layouts(std::vector<rdma::ObjectGroupLayout> layouts);

  // Session API — see PipelinedFetchSession. No-ops or returns empty/false
  // when not ready().
  uint16_t begin_request(const std::vector<rdma::ChunkPlacement>& placements,
                         const std::vector<rdma::ChunkNodeBinding>& chunk_nodes,
                         const std::vector<rdma::SlotDigest>& slot_digests);

  std::map<std::string, std::string> pipelined_fetch_commands() const;

  void on_node_reply(const std::string& node_name, const std::string& reply);

  void poll_notifications();

  bool has_active_request() const;

  bool is_layer_ready(uint32_t layer_id) const;

  std::vector<uint32_t> unservable_layers() const;

  void finish_request();

  void abandon_request();

 private:
  void ensure_session();

  L1RdmaRegistration registration_;
  std::string namespace_name_;
  size_t max_record_bytes_;

  mutable std::mutex mu_;
  bool initialized_ = false;
  bool ready_ = false;

  std::unique_ptr<rdma::RdmaContext> context_;
  rdma::NodeRegistry registry_;
  std::unique_ptr<rdma::SlotPlanner> planner_;
  std::unique_ptr<rdma::PipelinedFetchSession> session_;
};

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
