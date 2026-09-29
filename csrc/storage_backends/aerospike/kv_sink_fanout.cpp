// SPDX-License-Identifier: Apache-2.0
#include "kv_sink_fanout.h"

#include <aerospike/aerospike_info.h>
#include <aerospike/as_node.h>

#include <cstdlib>
#include <exception>
#include <set>
#include <stdexcept>

#include "rdma_context.h"

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
                                             NodeRegistry* registry) {
  if (as == nullptr) {
    throw std::runtime_error("register_all_nodes: null aerospike client");
  }
  if (registry == nullptr) {
    throw std::runtime_error("register_all_nodes: null node registry");
  }

  // Throws std::invalid_argument before any I/O if nothing is registered.
  const std::string command = build_register_command(local);

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

namespace {

struct PerNodeRegisterState {
  aerospike* as = nullptr;
  const as_policy_info* policy = nullptr;
  RdmaContext* context = nullptr;
  NodeRegistry* registry = nullptr;
  ClusterRegistrationResult* result = nullptr;
};

bool register_one_node_callback(const as_error* err, const as_node* node,
                                const char* /*req*/, char* /*res*/,
                                void* udata) {
  auto* state = static_cast<PerNodeRegisterState*>(udata);

  std::string node_name;
  if (node != nullptr) {
    node_name.assign(node->name);
  }

  try {
    if (err != nullptr && err->code != AEROSPIKE_OK) {
      state->result->failures.push_back(NodeRegistrationFailure{
          node_name, std::string("info request failed: ") + err->message});
      return true;
    }
    if (node == nullptr) {
      state->result->failures.push_back(
          NodeRegistrationFailure{node_name, "null node in fanout callback"});
      return true;
    }

    state->context->create_queue_pair_for_node(node_name);
    const LocalEndpoint local =
        state->context->local_endpoint_for_node(node_name);
    const std::string command = build_register_command(local);

    as_error node_err;
    char* response = nullptr;
    // The foreach callback hands out a const as_node* while
    // aerospike_info_node() takes a mutable one. The client does not modify
    // the node for an info request, so the cast is the C API's own
    // inconsistency rather than ours.
    const as_status status = aerospike_info_node(
        state->as, &node_err, state->policy, const_cast<as_node*>(node),
        command.c_str(), &response);
    if (status != AEROSPIKE_OK || response == nullptr) {
      state->result->failures.push_back(NodeRegistrationFailure{
          node_name,
          std::string("kv-sink-register failed: ") + node_err.message});
      return true;
    }

    NodeRegistration registration =
        parse_register_reply(node_name, std::string(response));
    state->registry->set(registration);
    ++state->result->registered;
    if (response != nullptr) {
      free(response);
    }
  } catch (const std::exception& e) {
    state->result->failures.push_back(
        NodeRegistrationFailure{node_name, e.what()});
  } catch (...) {
    state->result->failures.push_back(
        NodeRegistrationFailure{node_name, "unknown error during register"});
  }

  return true;
}

}  // namespace

ClusterRegistrationResult register_all_nodes(aerospike* as,
                                             const as_policy_info* policy,
                                             RdmaContext* context,
                                             NodeRegistry* registry) {
  if (as == nullptr) {
    throw std::runtime_error("register_all_nodes: null aerospike client");
  }
  if (context == nullptr) {
    throw std::runtime_error("register_all_nodes: null RdmaContext");
  }
  if (registry == nullptr) {
    throw std::runtime_error("register_all_nodes: null node registry");
  }

  ClusterRegistrationResult result;
  PerNodeRegisterState state;
  state.as = as;
  state.policy = policy;
  state.context = context;
  state.registry = registry;
  state.result = &result;

  as_error err;
  // Harmless discovery command; each callback issues its own per-node register.
  const as_status status = aerospike_info_foreach(
      as, &err, policy, "services", register_one_node_callback, &state);

  if (status != AEROSPIKE_OK && result.registered == 0 &&
      result.failures.empty()) {
    throw std::runtime_error(std::string("kv-sink-register fanout failed: ") +
                             err.message);
  }

  return result;
}

namespace {

struct PerNodeDeregisterState {
  aerospike* as = nullptr;
  const as_policy_info* policy = nullptr;
  const NodeRegistry* registry = nullptr;
  std::set<std::string> pending;
  ClusterDeregistrationResult* result = nullptr;
};

bool deregister_one_node_callback(const as_error* err, const as_node* node,
                                  const char* /*req*/, char* /*res*/,
                                  void* udata) {
  auto* state = static_cast<PerNodeDeregisterState*>(udata);
  if (node == nullptr || (err != nullptr && err->code != AEROSPIKE_OK)) {
    return true;
  }
  const std::string node_name(node->name);
  if (state->pending.erase(node_name) == 0) {
    return true;
  }

  try {
    const std::string command =
        build_deregister_command(state->registry->region_for(node_name));
    as_error node_err;
    char* response = nullptr;
    // See register_one_node_callback for the const_cast.
    const as_status status = aerospike_info_node(
        state->as, &node_err, state->policy, const_cast<as_node*>(node),
        command.c_str(), &response);
    const std::string reply = response != nullptr ? response : "";
    if (response != nullptr) {
      free(response);
    }
    if (status != AEROSPIKE_OK) {
      state->result->failures.push_back(NodeRegistrationFailure{
          node_name,
          std::string("kv-sink-deregister failed: ") + node_err.message});
    } else if (find_info_field(reply, "writes").empty()) {
      state->result->failures.push_back(NodeRegistrationFailure{
          node_name, "kv-sink-deregister refused: " + reply});
    } else {
      ++state->result->deregistered;
    }
  } catch (const std::exception& e) {
    state->result->failures.push_back(
        NodeRegistrationFailure{node_name, e.what()});
  } catch (...) {
    state->result->failures.push_back(
        NodeRegistrationFailure{node_name, "unknown error during deregister"});
  }
  return true;
}

}  // namespace

ClusterDeregistrationResult deregister_all_nodes(aerospike* as,
                                                 const as_policy_info* policy,
                                                 const NodeRegistry& registry) {
  if (as == nullptr) {
    throw std::runtime_error("deregister_all_nodes: null aerospike client");
  }

  ClusterDeregistrationResult result;
  const std::vector<std::string> names = registry.node_names();
  if (names.empty()) {
    return result;
  }
  PerNodeDeregisterState state;
  state.as = as;
  state.policy = policy;
  state.registry = &registry;
  state.pending.insert(names.begin(), names.end());
  state.result = &result;

  as_error err;
  const as_status status = aerospike_info_foreach(
      as, &err, policy, "services", deregister_one_node_callback, &state);
  if (status != AEROSPIKE_OK && result.deregistered == 0 &&
      result.failures.empty()) {
    throw std::runtime_error(std::string("kv-sink-deregister fanout failed: ") +
                             err.message);
  }
  for (const std::string& node_name : state.pending) {
    result.failures.push_back(
        NodeRegistrationFailure{node_name, "node is no longer in the cluster"});
  }
  return result;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
