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
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#ifdef LMCACHE_AEROSPIKE_RDMA
  #include "connector_sink_fetch.h"
  #include "memory_layout_conversion.h"
#endif

namespace lmcache {
namespace connector {

// One slot of a caller-planned pipelined fetch; see
// AerospikeNativeConnector::issue_pipelined_fetch_by_slots.
struct PlannedSlotKey {
  uint32_t node_index = 0;
  std::string record_key;
  size_t dest_offset = 0;
  size_t length = 0;
  uint32_t layer_id = 0;
};

struct WorkerAerospikeConn {
  aerospike* client = nullptr;
  std::string ns;
  std::string set_name;
  // Loads: TTL follows the namespace's default-read-touch-ttl-pct.
  as_policy_read read_policy;
  // Existence checks and delete's meta read: never extend a TTL. A lookup
  // reads only the meta record, so touching it would let the meta outlive
  // its segments.
  as_policy_read lookup_policy;
  as_policy_write write_policy;
  as_policy_remove remove_policy;
  // Batched existence checks; never extend a TTL, as lookup_policy.
  as_policy_batch batch_policy;
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

  // Register how each object group's payload is laid out, one list of plane
  // runs per object group with one run per kernel group in payload order.
  //
  // Writes whose size matches a registered layout are cut so that every
  // record stays inside one plane of its own kernel group; see
  // make_layered_shard_plan(). This is what lets hybrid models, whose kernel
  // groups differ in plane size, be fetched a layer at a time -- the single
  // set_plane_bytes() hint cannot describe them. A later call adds to the
  // layouts already registered rather than replacing them, so a second model
  // sharing this connector cannot re-cut the first's records; a size two
  // layouts share with different runs is not aligned at all.
  //
  // Thread safety: safe to call while stores are in flight. Each record
  // persists the layout it was written with, so this affects only
  // subsequent writes and never makes an existing record unreadable.
  //
  // Throws std::invalid_argument if any run has no planes or a zero plane
  // size; nothing is registered in that case.
  void set_record_layouts(
      const std::vector<std::vector<PlaneRun>>& object_groups);

  // Digest of the record stored under `user_key` in this connector's
  // namespace and set, as 40 lowercase hex characters.
  //
  // This is the client's own RIPEMD-160 over the Aerospike key, so it names
  // exactly the record a get of that key would read. Callers hand over keys
  // and let this produce the digest, instead of re-deriving it elsewhere.
  //
  // Thread safety: safe to call concurrently; touches no shared state.
  //
  // Throws std::invalid_argument if `user_key` is empty.
  std::string record_digest_hex(const std::string& user_key) const;

  // Name of the node that currently masters the record stored under
  // `user_key`, read from the client's partition map.
  //
  // Records of one chunk hash to independent partitions, so a planner must
  // ask per record rather than per chunk. The answer is a snapshot: a
  // migration may move the record before it is fetched.
  //
  // Thread safety: safe to call concurrently; the client guards its
  // partition map.
  //
  // Throws std::invalid_argument if `user_key` is empty, and
  // std::runtime_error if the client is not connected or no node masters
  // the record's partition.
  std::string record_node(const std::string& user_key) const;

  // Largest record this connector writes, in bytes: the server's record cap
  // less a safety margin. Layer-aligned writes cut planes against this, so a
  // reader naming records must plan with the same value.
  //
  // Thread safety: safe to call concurrently; fixed at construction.
  size_t max_record_bytes() const;

#ifdef LMCACHE_AEROSPIKE_RDMA
  // Report whether pipelined fetch is ready: the L1 windows are registered
  // as a kv-sink with every node and layouts fit a window. False when RDMA
  // was not enabled at build time or in config, or the sink was not created.
  //
  // Thread safety: safe to call concurrently.
  bool pipelined_fetch_ready() const;

  // Last pipelined RDMA initialization failure, when pipelined fetch is
  // unavailable. Empty when initialization succeeded or RDMA was not enabled.
  //
  // Thread safety: safe to call concurrently.
  std::string pipelined_fetch_init_error() const;

  // A node of the cluster, for placers that name a node per slot. Sink rows
  // are routed by the client's partition map, so the name does not choose
  // which node serves a slot.
  //
  // Thread safety: safe to call concurrently. Throws std::runtime_error if
  // pipelined fetch was not enabled or did not initialize.
  std::string pipelined_fetch_node_name() const;

  // Replace slot-planner layouts for subsequent pipelined fetches.
  //
  // Thread safety: safe to call concurrently; serialized on the driver lock.
  void set_object_group_layouts(
      const std::map<uint32_t, rdma::ObjectGroupLayoutInput>& layouts);

  // Begin a pipelined fetch whose slots the caller has already planned, and
  // return its generation.
  //
  // `slots[i]`'s record (by user key) is read with a kv-sink batch row that
  // places its `length`-byte value at `dest_offset` from the registration
  // base, and it counts toward `layer_id`'s readiness. Each layer goes out
  // as one batch, prioritized by the order its first slot appears. Returns
  // once the batches are queued; slots land asynchronously.
  //
  // `node_index` must name an entry of `node_names` but does not route the
  // row: the client sends each row to its partition's master.
  //
  // Thread safety: safe to call concurrently.
  //
  // Throws std::invalid_argument if a node index is out of range, a key is
  // empty, a length is zero, or a slot leaves the first slot's window;
  // sink::PlanTooLargeError if there are more slots than
  // pipelined_max_slots_per_request(); std::runtime_error if pipelined fetch
  // is not ready or the window of the first slot already has a fetch.
  //
  // Up to one fetch per window runs at a time; every later call about this
  // fetch quotes the returned generation.
  uint16_t issue_pipelined_fetch_by_slots(
      const std::vector<std::string>& node_names,
      const std::vector<PlannedSlotKey>& slots);

  // Most slots one pipelined fetch may carry, or 0 when pipelined fetch is
  // not ready.
  //
  // Thread safety: safe to call concurrently.
  uint32_t pipelined_max_slots_per_request() const;

  // Whether every slot of `layer_id` in fetch `request_generation` landed.
  // False when pipelined fetch is not ready, that fetch is not active, the
  // generation is 0, or the layer is not complete.
  //
  // Thread safety: safe to call concurrently.
  bool is_pipelined_layer_ready(uint32_t layer_id,
                                uint16_t request_generation) const;

  // Layers of fetch `generation` with a row that failed: record missing or
  // not a single-blob record, timeout, or a node that lost the sink twice.
  // Empty when pipelined fetch is not ready or that fetch is not active.
  //
  // A caller cannot distinguish "still in flight" from "will never arrive"
  // with is_pipelined_layer_ready alone: both report false. Without this the
  // only available response to a declined slot is to keep polling until the
  // deadline, which turns a recoverable miss into a stall.
  //
  // Thread safety: safe to call concurrently.
  std::vector<uint32_t> pipelined_unservable_layers(uint16_t generation) const;

  // Finish fetch `generation` after it completed. Throws std::runtime_error
  // if it is not active.
  //
  // Thread safety: safe to call concurrently.
  void finish_pipelined_fetch(uint16_t generation);

  // Abandon fetch `generation` without waiting; no-op if it is not active.
  // Rows already sent may still be written until the fetch timeout, so the
  // caller must not reuse the window before then.
  //
  // Thread safety: safe to call concurrently.
  void abandon_pipelined_fetch(uint16_t generation);
#endif

 protected:
  WorkerAerospikeConn create_connection() override;
  void do_single_get(WorkerAerospikeConn& conn, const std::string& key,
                     void* buf, size_t len, size_t chunk_size) override;
  void do_single_set(WorkerAerospikeConn& conn, const std::string& key,
                     const void* buf, size_t len, size_t chunk_size) override;
  bool do_single_exists(WorkerAerospikeConn& conn,
                        const std::string& key) override;
  // Look up a tile of keys with Aerospike batch reads of the meta records'
  // headers: one request per node per sub-batch of at most
  // kMaxBatchExistsKeys keys, instead of one round trip per key.
  //
  // A key exists exactly when do_single_exists() would say so. Throws
  // std::runtime_error if the batch or any key fails with anything other
  // than "record not found", as the per-key path does.
  void do_batch_exists(WorkerAerospikeConn& conn, const Request& req) override;
  bool do_single_delete(WorkerAerospikeConn& conn,
                        const std::string& key) override;
  void shutdown_connections() override;
  void on_workers_stopped() override;

 private:
  // Largest batch sent in one aerospike_batch_read(). The server refuses a
  // batch above its batch-max-requests (unlimited by default, historically
  // 5000), so larger tiles are split.
  static constexpr size_t kMaxBatchExistsKeys = 5000;

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

  // Every object-group layout registered through set_record_layouts(), and
  // the per-payload-size lookup derived from them. Guarded by
  // record_layouts_mu_, which plan() takes on every write.
  mutable std::mutex record_layouts_mu_;
  std::vector<std::vector<PlaneRun>> registered_record_layouts_;
  std::map<size_t, std::vector<PlaneRun>> record_layouts_;

  // Description of the L1 slab and window pool to register for RDMA
  // reception. Default-constructed (and therefore inert) unless the L2
  // adapter factory enabled RDMA.
  L1RdmaRegistration l1_rdma_registration_;

#ifdef LMCACHE_AEROSPIKE_RDMA
  void try_initialize_pipelined_rdma();
  std::unique_ptr<AerospikeSinkFetchDriver> pipelined_rdma_;
  std::string pipelined_init_error_;
#endif

  aerospike as_;
  std::mutex close_mu_;
  bool connected_ = false;
  bool closed_native_ = false;
};

}  // namespace connector
}  // namespace lmcache
