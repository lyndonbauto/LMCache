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

// Aerospike record digest for one slot in the active plan.
struct SlotDigest {
  uint16_t slot_index = 0;
  std::string digest_hex;
};

// Owns one in-flight pipelined fetch at a time: plan, readiness, commands.
//
// Thread safety: every public method is safe to call from any thread; an
// internal mutex serializes mutation. Const queries (is_layer_ready,
// unservable_layers, pipelined_fetch_commands while a request is active)
// take the same lock. The connector should drive replies and notifications
// from one thread if it wants to avoid lock contention, but the contract
// does not require it.
//
// No I/O: produces command strings and consumes reply/notification data.
class PipelinedFetchSession {
 public:
  // `planner` and `registry` must outlive this session. `namespace` is the
  // Aerospike namespace embedded in kv-sink-fetch-pipelined commands.
  PipelinedFetchSession(const SlotPlanner& planner,
                        const NodeRegistry& registry,
                        std::string namespace_name, size_t max_record_bytes,
                        size_t max_write_bytes);

  // Report whether a request is currently active (not abandoned).
  bool has_active_request() const;

  // Generation of the active request. Throws std::runtime_error when none.
  uint16_t active_generation() const;

  // Build a new request plan and readiness tracker.
  //
  // `chunk_nodes` maps every participating chunk id to the node that holds
  // it. `slot_digests` must name every slot index the plan will contain,
  // in ascending slot order (one entry per slot, index i at position i).
  //
  // Throws std::runtime_error if a request is already active,
  // std::invalid_argument if a chunk lacks a node or digests do not match
  // the plan, or any exception SlotPlanner::plan_request may throw.
  uint16_t begin_request(const std::vector<ChunkPlacement>& placements,
                         const std::vector<ChunkNodeBinding>& chunk_nodes,
                         const std::vector<SlotDigest>& slot_digests);

  // One kv-sink-fetch-pipelined command per node that owns a chunk in the
  // active plan. Empty when no request is active.
  //
  // Throws std::runtime_error if a chunk's node was never registered.
  std::map<std::string, std::string> pipelined_fetch_commands() const;

  // Feed one node's acknowledgement string from aerospike_info_node.
  //
  // No-op when no request is active or the request was abandoned, so a late
  // reply cannot mutate live state. Throws std::runtime_error if the reply
  // is malformed.
  void on_node_reply(const std::string& node_name, const std::string& reply);

  // Feed immediate data from RdmaContext::poll_notifications.
  //
  // Ignored when no request is active or the request was abandoned.
  // Immediates whose generation does not match the active plan are reported
  // by LayerReadiness and discarded.
  void on_notifications(const std::vector<uint32_t>& immediates);

  // Layer readiness for the active request. False when no request is active.
  bool is_layer_ready(uint32_t layer_id) const;

  // Layers marked unservable on the active request. Empty when none.
  std::vector<uint32_t> unservable_layers() const;

  // Layers fully landed on the active request. Empty when none.
  std::vector<uint32_t> ready_layers() const;

  // Drop the active request after a successful completion.
  //
  // Throws std::runtime_error when no request is active.
  void finish_request();

  // Abandon the active request. Late replies and notifications are ignored
  // afterward. Throws std::runtime_error when no request is active.
  void abandon_request();

 private:
  uint16_t allocate_generation();

  const SlotPlanner& planner_;
  const NodeRegistry& registry_;
  std::string namespace_name_;
  size_t max_record_bytes_;
  size_t max_write_bytes_;

  mutable std::mutex mu_;
  uint16_t next_generation_ = 0;

  bool has_request_ = false;
  bool abandoned_ = false;
  uint16_t active_generation_ = 0;
  RequestPlan plan_;
  LayerReadiness readiness_;
  std::map<uint32_t, std::string> chunk_to_node_;
  std::vector<std::string> digest_per_slot_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
