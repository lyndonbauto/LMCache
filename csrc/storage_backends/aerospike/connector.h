// SPDX-License-Identifier: Apache-2.0
#pragma once

#include "../connector_base.h"
#include "l1_rdma_registration.h"
#include "shard_plan.h"

#include <aerospike/aerospike.h>
#include <aerospike/as_policy.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#ifdef LMCACHE_AEROSPIKE_RDMA
  #include "connector_pipelined_rdma.h"
#endif

namespace lmcache {
namespace connector {

struct WorkerAerospikeConn {
  aerospike* client = nullptr;
  std::string ns;
  std::string set_name;
  as_policy_read read_policy;
  as_policy_write write_policy;
  as_policy_remove remove_policy;
};

// Native Aerospike storage backend.
//
// Records use a meta + segment layout: every cache key maps to a meta record
// (``<key>|m``) carrying the shard plan, and payloads larger than the
// discovered Aerospike record-size cap are split across segment records
// (``<key>|s|<i>``). Payloads that fit a single record are stored inline in
// the meta record. The connector key is used verbatim as the Aerospike user
// key base (the framework's ObjectKey-to-string format).
//
// `plane_bytes` opts into plane-aligned sharding, which keeps every record
// confined to one model layer so a layer-pipelined reader can serve one layer
// without waiting on its neighbours. See shard_plan.h for the rule and what
// it costs.
class AerospikeNativeConnector : public ConnectorBase<WorkerAerospikeConn> {
 public:
  AerospikeNativeConnector(
      std::string hosts, std::string ns, std::string set_name, int num_workers,
      uint32_t read_timeout_ms = 1000, uint32_t write_timeout_ms = 2000,
      uint32_t default_ttl_seconds = 86400, size_t target_segment_bytes = 0,
      size_t max_record_bytes = 0, std::string username = "",
      std::string password = "",
      L1RdmaRegistration l1_rdma_registration = L1RdmaRegistration(),
      size_t plane_bytes = 0);
  ~AerospikeNativeConnector() override;

  void close() override;

  // Set the size of one K/V plane, in bytes, or 0 to shard by byte count.
  //
  // LMCache only knows the layout once a worker has registered its KV cache,
  // which is after this connector is constructed, so the plane size arrives
  // here rather than through the constructor. Safe to call while stores are
  // in flight: each record persists the plane size it was written with, so a
  // change affects only subsequent writes and never makes an existing record
  // unreadable.
  void set_plane_bytes(size_t plane_bytes);

#ifdef LMCACHE_AEROSPIKE_RDMA
  // Report whether pipelined kv-sink-fetch is initialized and at least one
  // node registered. False when RDMA was not enabled at build time or in
  // config, or when kv-sink-register has not succeeded.
  //
  // Thread safety: safe to call concurrently.
  bool pipelined_fetch_ready() const;

  // Layer readiness for the active pipelined fetch. False when pipelined
  // fetch is not ready, no request is active, or the layer is not complete.
  //
  // Thread safety: safe to call concurrently.
  bool is_pipelined_layer_ready(uint32_t layer_id) const;

  // Drain RDMA write-with-immediate notifications into the active session.
  //
  // Thread safety: safe to call concurrently; serialized with other pipelined
  // methods on the internal driver lock.
  void poll_pipelined_fetch_notifications();
#endif

 protected:
  WorkerAerospikeConn create_connection() override;
  void do_single_get(WorkerAerospikeConn& conn, const std::string& key,
                     void* buf, size_t len, size_t chunk_size) override;
  void do_single_set(WorkerAerospikeConn& conn, const std::string& key,
                     const void* buf, size_t len, size_t chunk_size) override;
  bool do_single_exists(WorkerAerospikeConn& conn,
                        const std::string& key) override;
  bool do_single_delete(WorkerAerospikeConn& conn,
                        const std::string& key) override;
  void shutdown_connections() override;
  void on_workers_stopped() override;

 private:
  static std::vector<std::pair<std::string, int>> parse_hosts(
      const std::string& hosts);
  static std::string meta_user_key(const std::string& cache_key);
  static std::string segment_user_key(const std::string& cache_key,
                                      uint32_t index);
  static void throw_status(const char* op, as_status status,
                           const as_error& err);

  ShardPlan plan(size_t payload_bytes) const;
  size_t discover_record_cap();
  void configure_policies();
  void put_payload_record(WorkerAerospikeConn& conn,
                          const std::string& user_key, const void* buf,
                          size_t len);
  void put_meta_record(WorkerAerospikeConn& conn, const std::string& user_key,
                       const ShardPlan& plan, size_t total_bytes,
                       const void* inline_buf);
  bool read_payload_record(WorkerAerospikeConn& conn,
                           const std::string& user_key, void* buf, size_t len);

  std::string hosts_;
  std::string ns_;
  std::string set_name_;
  uint32_t read_timeout_ms_;
  uint32_t write_timeout_ms_;
  uint32_t default_ttl_seconds_;
  size_t target_segment_bytes_;
  size_t max_record_bytes_;
  size_t single_record_threshold_bytes_;
  // Size of one K/V plane, or 0 to shard by byte count. See shard_plan.h.
  // Atomic because set_plane_bytes() may land while worker threads are
  // sharding a payload in plan().
  std::atomic<size_t> plane_bytes_;

  // Description of the L1 slab and window pool to register for RDMA
  // reception. Default-constructed (and therefore inert) unless the L2
  // adapter factory enabled RDMA.
  L1RdmaRegistration l1_rdma_registration_;

#ifdef LMCACHE_AEROSPIKE_RDMA
  void try_initialize_pipelined_rdma();
  std::unique_ptr<AerospikePipelinedRdmaDriver> pipelined_rdma_;
#endif

  aerospike as_;
  std::mutex close_mu_;
  bool connected_ = false;
  bool closed_native_ = false;
};

}  // namespace connector
}  // namespace lmcache
