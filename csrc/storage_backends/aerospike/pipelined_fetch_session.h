// SPDX-License-Identifier: Apache-2.0
#pragma once

// End-to-end driver for one pipelined kv-sink fetch, with no I/O.
//
// layer_pipeline.h defines what a slot means and when a layer is ready.
// slot_planner.h turns placements into that schedule. kv_sink_client.h
// builds the per-node commands and parses acknowledgements. rdma_context.h
// polls the immediates that actually complete each write. None of those
// pieces issue info calls or touch a socket; this file stitches them
// together so the connector can stay a thin I/O shell around a fully
// testable state machine.
//
// == The trap this exists to avoid ==
//
// It is tempting to fold generation allocation, per-node command fanout,
// declined-slot handling, and notification draining into the connector
// because that is where aerospike_info_node lives. The result is a class
// that cannot be exercised without a cluster, which means the failure modes
// that only show up under multi-node numbering (one slot index space across
// every node, a late reply after abandon, a stale generation after wrap)
// never get a unit test and regress silently until someone runs Soft-RoCE.
//
// Keeping every byte of protocol state here -- and forbidding this class
// from calling aerospike_info_* -- forces those cases into the logic
// harness, where they belong.

#include "kv_sink_client.h"
#include "layer_pipeline.h"
#include "slot_planner.h"

#include <cstdint>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// Which cluster node holds each participating chunk.
struct ChunkNodeBinding {
  uint32_t chunk_id = 0;
  std::string node_name;
};

// Aerospike record digest for one logical slot in the active plan.
struct SlotDigest {
  uint32_t chunk_id = 0;
  uint32_t layer_id = 0;
  uint32_t plane = 0;
  uint32_t piece = 0;
  std::string digest_hex;
};

// Owns one in-flight pipelined fetch at a time: plan, readiness, commands.
//
// Thread safety: every public method takes `mu_` and may be called from any
// thread. `planner` and `registry` must not be mutated for this session's
// lifetime; they are not locked here.
class PipelinedFetchSession {
 public:
  // `planner` and `registry` must outlive this session and must not change
  // while the session exists. `namespace_name` is embedded in fetch commands.
  PipelinedFetchSession(const SlotPlanner& planner,
                        const NodeRegistry& registry,
                        std::string namespace_name, size_t max_record_bytes,
                        size_t max_write_bytes, size_t window_bytes,
                        uint32_t max_notification_slots);

  // Report whether a request is currently active.
  //
  // Thread safety: takes `mu_`.
  bool has_active_request() const;

  // Generation of the active request.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error when none is active.
  uint16_t active_generation() const;

  // Build a new request plan and readiness tracker.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error if a request is
  // already active, if `slot_count()` exceeds `max_notification_slots_`, if a
  // planned slot falls outside `window_bytes_`, if a digest is missing, or if
  // any exception SlotPlanner::plan_request may throw. Throws
  // std::invalid_argument if a chunk lacks a node binding.
  uint16_t begin_request(const std::vector<ChunkPlacement>& placements,
                         const std::vector<ChunkNodeBinding>& chunk_nodes,
                         const std::vector<SlotDigest>& slot_digests);

  // One kv-sink-fetch-pipelined command per node that owns a chunk in the
  // active plan. Empty when no request is active.
  //
  // Thread safety: takes `mu_`.
  std::map<std::string, std::string> pipelined_fetch_commands() const;

  // Feed one node's acknowledgement string from aerospike_info_node.
  //
  // Thread safety: takes `mu_`. Ignores the reply when `generation` does not
  // match the active request, when no request is active, or when the request
  // was finished or abandoned after that generation was issued. Throws
  // std::runtime_error if the reply is malformed, if accepted plus failed does
  // not equal requested, or if a failed slot is not owned by `node_name`.
  void on_node_reply(const std::string& node_name, const std::string& reply,
                     uint16_t generation);

  // Feed immediate data from RdmaContext::poll_notifications.
  //
  // Thread safety: takes `mu_`. Ignored when no request is active. Stale
  // generations are discarded by LayerReadiness.
  void on_notifications(const std::vector<uint32_t>& immediates);

  // Layer readiness for the active request.
  //
  // Thread safety: takes `mu_`.
  bool is_layer_ready(uint32_t layer_id) const;

  // Layers marked unservable on the active request.
  //
  // Thread safety: takes `mu_`.
  std::vector<uint32_t> unservable_layers() const;

  // Layers fully landed on the active request.
  //
  // Thread safety: takes `mu_`.
  std::vector<uint32_t> ready_layers() const;

  // Drop the active request after a successful completion.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error when none is active.
  void finish_request();

  // Abandon the active request. Replies for that generation are ignored
  // afterward.
  //
  // Thread safety: takes `mu_`. Throws std::runtime_error when none is active.
  void abandon_request();

 private:
  uint16_t allocate_generation();

  const SlotPlanner& planner_;
  const NodeRegistry& registry_;
  std::string namespace_name_;
  size_t max_record_bytes_;
  size_t max_write_bytes_;
  size_t window_bytes_;
  uint32_t max_notification_slots_;

  mutable std::mutex mu_;
  uint16_t next_generation_ = 0;
  uint16_t last_abandoned_generation_ = 0;

  bool has_request_ = false;
  uint16_t active_generation_ = 0;
  RequestPlan plan_;
  LayerReadiness readiness_;
  std::map<uint32_t, std::string> chunk_to_node_;
  std::map<std::string, std::set<uint16_t>> slots_by_node_;
  std::vector<std::string> digest_per_slot_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
