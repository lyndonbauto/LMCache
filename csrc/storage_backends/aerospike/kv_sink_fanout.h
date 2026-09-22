// SPDX-License-Identifier: Apache-2.0
#pragma once

// Per-node fanout for the kv-sink control plane.
//
// Kept separate from kv_sink_client.{h,cpp} so that the command/reply codec
// stays free of any Aerospike SDK dependency and remains trivially testable;
// only this translation unit needs libaerospike.
//
// Registration is inherently per-node: the region id in a kv-sink-register
// reply is scoped to whichever node answered. aerospike_info_any() picks an
// arbitrary node and so would yield a handle valid on exactly one unnamed
// node, which is why the fanout below uses aerospike_info_foreach().

#include <aerospike/aerospike.h>
#include <aerospike/as_policy.h>

#include <string>
#include <vector>

#include "kv_sink_client.h"
#include "rdma_context.h"

namespace lmcache {
namespace connector {
namespace rdma {

class RdmaContext;

// Why a node's registration failed, for reporting and retry decisions.
struct NodeRegistrationFailure {
  std::string node_name;
  std::string reason;
};

// Outcome of registering one local endpoint across a whole cluster.
//
// Partial success is a real state: one unreachable node should not prevent
// RDMA reception from the rest of the cluster, so failures are reported
// rather than thrown, and the caller decides.
struct ClusterRegistrationResult {
  uint32_t registered = 0;
  std::vector<NodeRegistrationFailure> failures;

  // Report whether every node that answered was registered successfully.
  bool fully_registered() const { return failures.empty() && registered > 0; }
};

// Send "kv-sink-register" to every node and record each node's own region
// handle in `registry`.
//
// The command text is identical for every node -- it describes *our* endpoint
// -- but each reply carries that node's own region id, peer GID, and peer QPN.
// Replaces any registration previously held for a node.
//
// A node whose reply is missing or malformed is recorded in the result's
// failure list; per-node errors do not abort the fanout, because a single
// unreachable node should not disable RDMA cluster-wide.
//
// Args:
//   as: connected Aerospike client.
//   policy: info policy, or nullptr for the client default.
//   local: our endpoint, valid after RdmaContext::create_queue_pair().
//   window_index: which registered window's rkey to publish.
//   registry: receives one region handle per node.
//
// Returns the per-node outcome. Throws std::runtime_error only when the
// cluster-wide info call itself fails (for example the client is not
// connected), and std::out_of_range if `window_index` is not registered.
ClusterRegistrationResult register_all_nodes(aerospike* as,
                                             const as_policy_info* policy,
                                             const LocalEndpoint& local,
                                             uint32_t window_index,
                                             NodeRegistry* registry);

// Register using one queue pair per node in `context`.
//
// For each cluster node the fanout creates a dedicated queue pair, sends that
// node's qpn/psn in kv-sink-register, records the reply, and leaves connection
// to the caller. `window_index` is fixed at 0 today; additional windows are
// deferred until the driver leases non-zero indices.
ClusterRegistrationResult register_all_nodes(aerospike* as,
                                             const as_policy_info* policy,
                                             RdmaContext* context,
                                             uint32_t window_index,
                                             NodeRegistry* registry);

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
