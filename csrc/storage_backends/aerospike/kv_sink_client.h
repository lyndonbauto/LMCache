// SPDX-License-Identifier: Apache-2.0
#pragma once

// Client for the Aerospike "kv-sink" info-command control plane.
//
// Both operations are asinfo commands with key=value; delimited requests and
// replies, following the idiom already used by
// AerospikeNativeConnector::discover_record_cap().
//
// Registration is inherently **per node**: the reply hands back a region id
// scoped to whichever node answered. So this client fans out with the
// node-specific info call and holds one region handle per node.
// aerospike_info_any() sends to an arbitrary node and must not be used here.

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "rdma_context.h"

namespace lmcache {
namespace connector {
namespace rdma {

// One destination for a single fetched chunk. The offset is chosen by LMCache
// and never by the server.
struct SinkRequest {
  // Record digest, hex encoded, as the real Aerospike client computes it.
  std::string digest_hex;
  // Byte offset from the base of the registered region.
  size_t offset = 0;
  size_t length = 0;
  // Slot index this write reports as, from the request's RequestPlan. Used
  // only by build_pipelined_fetch_command; the all-or-nothing fetch has no
  // per-write signal and ignores it.
  uint16_t slot = 0;
};

// Outcome of a "kv-sink-fetch". Because the server fences on its own send
// completion queue before replying, this reply *is* the completion: when it
// says ok, the bytes are already in L1.
struct FetchReply {
  uint32_t requested = 0;
  uint32_t ok = 0;
  uint64_t bytes = 0;
  uint32_t in_place = 0;
  std::vector<std::string> results;

  // Report whether every requested chunk landed.
  bool all_ok() const { return requested > 0 && ok == requested; }
};

// Outcome of a "kv-sink-fetch-pipelined".
//
// Unlike FetchReply this is **not** a completion. The server replies before
// its writes land, and completion arrives as immediate data on the client's
// receive queue; see layer_pipeline.h. The reply says only what the server
// undertook to do.
//
// `failed_slots` is the load-bearing field. A slot the server cannot serve --
// a missing record, a read error -- must be named here, because a slot that is
// merely absent from the reply is indistinguishable from one still in flight:
// its layer would never reach its expected count and the request would hang
// until the deadline. Feed each one to LayerReadiness::note_unservable.
struct PipelinedFetchReply {
  uint32_t requested = 0;
  uint32_t accepted = 0;
  uint64_t bytes = 0;
  std::vector<uint16_t> failed_slots;

  // Report whether the server undertook every requested write.
  bool all_accepted() const {
    return requested > 0 && accepted == requested && failed_slots.empty();
  }
};

// Historical server cap on sinks per kv-sink-fetch-pipelined command, used when
// a register reply omits max_sinks. Treating absence as unlimited would rebuild
// the single-command fanout that every node rejected before the token existed.
constexpr uint32_t kDefaultMaxSinksPerPipelinedCommand = 256;

// Per-node registration state. The region id is connection-lifetime: it must
// be invalidated when the slab is re-registered or the node restarts, because
// a stale id points at memory the server still believes it may write.
struct NodeRegistration {
  std::string node_name;
  uint64_t region = 0;
  PeerEndpoint peer;
  // From kv-sink-register `max_sinks`, or kDefaultMaxSinksPerPipelinedCommand
  // when the server omits the field (pre-advertisement builds).
  uint32_t max_sinks_per_command = kDefaultMaxSinksPerPipelinedCommand;
  bool valid = false;
};

// Build the "kv-sink-register" info command for a local endpoint.
//
// Publishes the whole registered window range: its rkey, its start address,
// and window_count * window_bytes as its size.
//
// Throws std::invalid_argument if `local` has no registered range.
std::string build_register_command(const LocalEndpoint& local);

// Build the "kv-sink-fetch" info command.
//
// Throws std::invalid_argument if `sinks` is empty.
std::string build_fetch_command(const std::string& ns, uint64_t region,
                                const std::vector<SinkRequest>& sinks);

// Build the "kv-sink-fetch-pipelined" info command for one node's sinks.
//
//   kv-sink-fetch-pipelined:namespace=<ns>;region=<id>;gen=<generation>;
//     sinks=<digest>@<off>:<len>#<slot>,...
//
// A distinct command rather than a flag on kv-sink-fetch, so a server that
// does not implement it fails outright instead of silently performing an
// all-or-nothing fetch whose per-slot signals would never arrive -- which the
// client would wait on until its deadline.
//
// `generation` is sent once for the whole command and the server forms each
// immediate as `(generation << 16) | slot`. Every fetch command issued for one
// request carries the same generation, so making it a single field means a
// server cannot get it wrong for an individual write.
//
// `sinks` should be ordered as the plan ordered them -- layer-major, earliest
// layer first -- because that order is what the server is asked to push in.
//
// Throws std::invalid_argument if `sinks` is empty or if two sinks carry the
// same slot index, since the second arrival would be counted as a duplicate
// and its layer would never complete.
std::string build_pipelined_fetch_command(
    const std::string& ns, uint64_t region, uint16_t generation,
    const std::vector<SinkRequest>& sinks);

// Look up one key in a key=value; delimited info reply.
//
// Returns an empty string when the key is absent, which callers must
// distinguish from a present-but-empty value themselves.
std::string find_info_field(const std::string& reply, const std::string& key);

// Parse a "kv-sink-register" reply into a node registration.
//
// `max_sinks` is optional; when absent, max_sinks_per_command is set to
// kDefaultMaxSinksPerPipelinedCommand. Throws std::runtime_error if the reply
// omits `region`, `qpn`, or `gid`, or if a numeric field does not parse; the
// caller cannot proceed without them.
NodeRegistration parse_register_reply(const std::string& node_name,
                                      const std::string& reply);

// Parse a "kv-sink-fetch" reply.
//
// Throws std::runtime_error if the reply omits `n` or `ok`.
FetchReply parse_fetch_reply(const std::string& reply);

// Parse a "kv-sink-fetch-pipelined" reply.
//
//   n=<count>;accepted=<count>;failed=<slot>,<slot>,...;bytes=<total>
//
// `failed` and `bytes` may be absent or empty, which means no slot failed and
// no byte count was reported. Remember the reply is an acknowledgement and not
// a completion.
//
// Throws std::runtime_error if the reply omits `n` or `accepted`, if a numeric
// field does not parse, or if a failed slot index does not fit 16 bits.
PipelinedFetchReply parse_pipelined_fetch_reply(const std::string& reply);

// Slot indices encoded in a built kv-sink-fetch-pipelined command, in wire
// order (the order sinks appear in the sinks= field).
std::vector<uint16_t> pipelined_command_slot_indices(
    const std::string& command);

// Holds one region handle per node for the lifetime of a connection.
//
// Thread safety: not synchronized. Register during initialization from a
// single thread; treat as read-only afterwards, or guard externally.
class NodeRegistry {
 public:
  // Record a node's registration, replacing any previous one.
  void set(const NodeRegistration& registration);

  // Look up a node's region handle.
  //
  // Throws std::runtime_error when the node was never registered or its
  // registration has been invalidated, rather than returning a sentinel that
  // could be mistaken for region 0.
  uint64_t region_for(const std::string& node_name) const;

  // Maximum sinks this node accepts in one kv-sink-fetch-pipelined command.
  //
  // Throws std::runtime_error when the node was never registered or its
  // registration has been invalidated.
  uint32_t max_sinks_per_command_for(const std::string& node_name) const;

  // Drop every registration, e.g. after the slab is re-registered.
  void invalidate_all();

  // Number of nodes holding a valid registration.
  size_t valid_count() const;

  // Peer endpoint learned from kv-sink-register for one node.
  //
  // Throws std::runtime_error when the node was never registered or its
  // registration has been invalidated.
  PeerEndpoint peer_endpoint_for(const std::string& node_name) const;

  // Names of nodes holding a valid registration.
  std::vector<std::string> node_names() const;

 private:
  std::map<std::string, NodeRegistration> by_node_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
