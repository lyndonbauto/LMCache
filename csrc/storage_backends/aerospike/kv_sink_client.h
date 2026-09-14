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

// Per-node registration state. The region id is connection-lifetime: it must
// be invalidated when the slab is re-registered or the node restarts, because
// a stale id points at memory the server still believes it may write.
struct NodeRegistration {
  std::string node_name;
  uint64_t region = 0;
  PeerEndpoint peer;
  bool valid = false;
};

// Build the "kv-sink-register" info command for a local endpoint.
//
// `window_index` selects which registered window's rkey to publish.
// Throws std::out_of_range if `window_index` is not a registered window.
std::string build_register_command(const LocalEndpoint& local,
                                   uint32_t window_index);

// Build the "kv-sink-fetch" info command.
//
// Throws std::invalid_argument if `sinks` is empty.
std::string build_fetch_command(const std::string& ns, uint64_t region,
                                const std::vector<SinkRequest>& sinks);

// Look up one key in a key=value; delimited info reply.
//
// Returns an empty string when the key is absent, which callers must
// distinguish from a present-but-empty value themselves.
std::string find_info_field(const std::string& reply, const std::string& key);

// Parse a "kv-sink-register" reply into a node registration.
//
// Throws std::runtime_error if the reply omits `region`, `qpn`, or `gid`, or
// if a numeric field does not parse; the caller cannot proceed without them.
NodeRegistration parse_register_reply(const std::string& node_name,
                                      const std::string& reply);

// Parse a "kv-sink-fetch" reply.
//
// Throws std::runtime_error if the reply omits `n` or `ok`.
FetchReply parse_fetch_reply(const std::string& reply);

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

  // Drop every registration, e.g. after the slab is re-registered.
  void invalidate_all();

  // Number of nodes holding a valid registration.
  size_t valid_count() const;

 private:
  std::map<std::string, NodeRegistration> by_node_;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
