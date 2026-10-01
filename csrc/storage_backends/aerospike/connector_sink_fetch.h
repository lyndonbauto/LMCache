// SPDX-License-Identifier: Apache-2.0
#pragma once

#ifdef LMCACHE_AEROSPIKE_RDMA

// Pipelined kv-sink fetch for AerospikeNativeConnector.
//
// The L1 RDMA windows are registered once as an as_sink (the patched
// Aerospike C client's aerospike_sink_create), which gives every node an
// RDMA path into them. A fetch is then ordinary batch reads: one batch per
// layer, each row naming its record, its offset in the window, its length and
// the layer's priority. The server writes the value into the window by RDMA
// and answers the row only once the write has completed, so an OK row is a
// landed slot. See docs/design/v1/distributed/l2_adapters/aerospike_rdma.md.
//
// Lives in its own translation unit so a default Aerospike build without
// LMCACHE_AEROSPIKE_RDMA needs neither the patched client nor libibverbs.

  #include "l1_rdma_registration.h"
  #include "sink_fetch_table.h"
  #include "slot_planner.h"

  #include <aerospike/aerospike.h>
  #include <aerospike/as_policy.h>
  #include <aerospike/as_sink.h>

  #include <condition_variable>
  #include <cstdint>
  #include <deque>
  #include <memory>
  #include <mutex>
  #include <string>
  #include <thread>
  #include <vector>

namespace lmcache {
namespace connector {

class AerospikeSinkFetchDriver {
 public:
  AerospikeSinkFetchDriver(L1RdmaRegistration registration,
                           std::string namespace_name, std::string set_name);

  // Stops the workers. Does not release the sink, which needs a connected
  // client: call shutdown() first.
  ~AerospikeSinkFetchDriver();

  AerospikeSinkFetchDriver(const AerospikeSinkFetchDriver&) = delete;
  AerospikeSinkFetchDriver& operator=(const AerospikeSinkFetchDriver&) = delete;

  // Create the sink over the window range, registering it with every node,
  // and start the batch workers.
  //
  // Thread safety: call once, from connector startup, before any other
  // method. Throws std::runtime_error, after recording the reason for
  // init_error_message(), if the registration is malformed or the sink
  // cannot be created.
  void initialize(aerospike* client);

  // True when the sink exists, layouts are set and fit one window, and
  // shutdown() has not run.
  //
  // Thread safety: takes `mu_`.
  bool is_ready() const;

  // Why pipelined fetch is unavailable: the initialization failure, a
  // layout that does not fit a window, or shutdown. Empty when ready.
  //
  // Thread safety: takes `mu_`.
  std::string init_error_message() const;

  // A node of the cluster, for callers that key placement by node. Rows are
  // routed by the client's partition map, so any node will do.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error when not ready or
  // the cluster has no nodes.
  std::string node_name() const;

  // Set the model's layouts; pipelined fetch is unavailable until they are.
  // If one chunk of `layouts` does not fit a window, it stays unavailable
  // until layouts that fit are set, and init_error_message() says why.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error if a fetch is
  // active, and std::invalid_argument if a layout is malformed (see
  // rdma::SlotPlanner's constructor).
  void set_object_group_layouts(std::vector<rdma::ObjectGroupLayout> layouts);

  // Begin a fetch of exactly `slots` in the window of its first slot, queue
  // its layer batches, and return its generation without waiting for them.
  //
  // Thread safety: safe to call concurrently; up to one fetch per window.
  // Throws std::runtime_error when not ready or the window is busy,
  // sink::PlanTooLargeError when the plan exceeds max_slots_per_request(),
  // and std::invalid_argument for a malformed slot (see
  // sink::SinkFetchTable::begin), including a length above UINT32_MAX.
  uint16_t issue(std::vector<sink::SinkSlot> slots);

  // Most slots one fetch may carry, or 0 when not ready.
  uint32_t max_slots_per_request() const;

  // Whether every slot of `layer_id` in fetch `generation` landed.
  bool is_layer_ready(uint32_t layer_id, uint16_t generation) const;

  // Layers of fetch `generation` with a slot that will not land.
  std::vector<uint32_t> unservable_layers(uint16_t generation) const;

  // Drop fetch `generation` after it completed. Throws std::runtime_error
  // if it is not active.
  void finish_request(uint16_t generation);

  // Drop fetch `generation` without waiting; no-op if it is not active. Its
  // queued batches are skipped; batches already sent run to their timeout
  // and their results are discarded.
  void abandon_request(uint16_t generation);

  // Stop the workers, waiting for batches in flight, then deregister the
  // sink from every node. No-op if initialization never succeeded or it
  // already ran.
  //
  // Thread safety: call while the client is still connected. Never throws.
  void shutdown();

 private:
  struct QueuedBatch {
    sink::LayerBatch batch;
    std::shared_ptr<const std::vector<sink::SinkSlot>> slots;
  };

  void start_workers();
  void stop_workers();
  void run_worker();
  void execute(const QueuedBatch& queued);
  // Read `slot_indices` of `queued` in one batch; one result per index.
  std::vector<as_status> read_rows(const QueuedBatch& queued,
                                   const std::vector<uint32_t>& slot_indices);
  // Re-register the sink where a node lost it, unless another worker
  // already did since `seen_epoch`.
  void refresh_sink(uint64_t seen_epoch);

  const L1RdmaRegistration registration_;
  const std::string namespace_name_;
  const std::string set_name_;

  // Set by initialize(), then read-only until shutdown(), which runs after
  // every worker has stopped.
  aerospike* client_ = nullptr;
  as_sink* sink_ = nullptr;
  as_policy_batch batch_policy_;
  std::unique_ptr<sink::SinkFetchTable> table_;

  mutable std::mutex mu_;
  std::string init_error_;
  std::string layout_error_;
  bool layouts_set_ = false;
  bool shut_down_ = false;

  std::mutex refresh_mu_;
  uint64_t refresh_epoch_ = 0;

  std::mutex queue_mu_;
  std::condition_variable queue_cv_;
  std::deque<QueuedBatch> queue_;
  bool stopping_ = false;
  std::vector<std::thread> workers_;
};

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
