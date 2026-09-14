// SPDX-License-Identifier: Apache-2.0
#include "kv_sink_fanout.h"

#include <aerospike/aerospike_info.h>
#include <aerospike/as_node.h>

#include <exception>
#include <stdexcept>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

// State threaded through the C callback via its udata pointer.
struct FanoutState {
  NodeRegistry* registry = nullptr;
  ClusterRegistrationResult* result = nullptr;
};

// Record a per-node failure without aborting the rest of the fanout.
void record_failure(FanoutState* state, const std::string& node_name,
                    const std::string& reason) {
  state->result->failures.push_back(NodeRegistrationFailure{node_name, reason});
}

// Callback invoked once per node by aerospike_info_foreach.
//
// This crosses a C boundary, so it must never let an exception escape: the
// SDK is not exception-safe and unwinding through it is undefined. Every
// failure is therefore recorded and reported through `udata`.
//
// Per the SDK contract for aerospike_info_foreach, `res` is owned by the
// library and must NOT be freed here. This differs from
// aerospike_info_node()/aerospike_info_any(), whose responses the caller does
// free -- as discover_record_cap() does.
bool on_node_reply(const as_error* err, const as_node* node, const char* req,
                   char* res, void* udata) {
  (void)req;
  auto* state = static_cast<FanoutState*>(udata);

  std::string node_name;
  if (node != nullptr) {
    node_name.assign(node->name);
  }

  try {
    if (err != nullptr && err->code != AEROSPIKE_OK) {
      record_failure(state, node_name,
                     std::string("info request failed: ") + err->message);
      return true;
    }
    if (res == nullptr) {
      record_failure(state, node_name, "empty kv-sink-register reply");
      return true;
    }

    NodeRegistration registration =
        parse_register_reply(node_name, std::string(res));
    state->registry->set(registration);
    ++state->result->registered;
  } catch (const std::exception& e) {
    record_failure(state, node_name, e.what());
  } catch (...) {
    record_failure(state, node_name, "unknown error parsing reply");
  }

  // Keep going: one bad node must not hide the rest of the cluster.
  return true;
}

}  // namespace

ClusterRegistrationResult register_all_nodes(aerospike* as,
                                             const as_policy_info* policy,
                                             const LocalEndpoint& local,
                                             uint32_t window_index,
                                             NodeRegistry* registry) {
  if (as == nullptr) {
    throw std::runtime_error("register_all_nodes: null aerospike client");
  }
  if (registry == nullptr) {
    throw std::runtime_error("register_all_nodes: null node registry");
  }

  // Throws std::out_of_range before any I/O if the window is not registered.
  const std::string command = build_register_command(local, window_index);

  ClusterRegistrationResult result;
  FanoutState state;
  state.registry = registry;
  state.result = &result;

  as_error err;
  const as_status status = aerospike_info_foreach(
      as, &err, policy, command.c_str(), on_node_reply, &state);

  // A cluster-wide failure (for example, not connected) is fatal and
  // distinct from an individual node rejecting the command.
  if (status != AEROSPIKE_OK && result.registered == 0 &&
      result.failures.empty()) {
    throw std::runtime_error(std::string("kv-sink-register fanout failed: ") +
                             err.message);
  }

  return result;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
