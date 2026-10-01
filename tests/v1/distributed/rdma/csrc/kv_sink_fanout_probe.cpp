// SPDX-License-Identifier: Apache-2.0
//
// T-RDMA-05: per-node kv-sink-register fanout against a real kv-sink cluster.
//
// The adapter's pipelined driver refuses a cluster of more than one node
// before it registers anything (N1), so the fanout it would use is driven
// here directly: the production RdmaContext registers a window range, the
// production register_all_nodes() sends kv-sink-register to every node, and
// each node's reply (region, peer QPN, sink limit) is printed and its queue
// pair driven to RTS. Pass: every node in the cluster registered, with no
// failures.
//
// With --restart-gate PATH the probe then waits for PATH to exist (the
// caller restarts a node meanwhile) and checks that deregistration fails on
// the restarted node, which no longer knows its region. It then registers
// again, first on the same context (recorded only: per-node queue pairs are
// created once) and then on a fresh context, which must reach every node
// (T-FLT-04, kv-sink half).
//
//   kv_sink_fanout_probe --host 127.0.0.1 --port 3400 --device rxe0 --gid 1
//       [--windows 4] [--window-bytes 1048576] [--restart-gate PATH]
//
// Exit 0 on pass, 1 on a failed check, 2 on a usage or setup error.

#include <aerospike/aerospike.h>
#include <aerospike/as_cluster.h>
#include <aerospike/as_node.h>

#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <memory>
#include <string>
#include <vector>

#include "kv_sink_client.h"
#include "kv_sink_fanout.h"
#include "rdma_context.h"

namespace rdma = lmcache::connector::rdma;

namespace {

uint32_t cluster_size(aerospike* as) {
  as_nodes* nodes = as_nodes_reserve(as->cluster);
  const uint32_t size = nodes->size;
  as_nodes_release(nodes);
  return size;
}

// Print every node's registration; returns the number of nodes printed.
size_t print_registry(const char* phase, const rdma::NodeRegistry& registry) {
  const std::vector<std::string> names = registry.node_names();
  for (const std::string& name : names) {
    const rdma::PeerEndpoint peer = registry.peer_endpoint_for(name);
    std::printf("%s node=%s region=%llu peer_qpn=%u peer_gid=%s max_sinks=%u\n",
                phase, name.c_str(),
                static_cast<unsigned long long>(registry.region_for(name)),
                peer.qpn, peer.gid_hex.c_str(),
                registry.max_sinks_per_command_for(name));
  }
  return names.size();
}

bool check_registration(const char* phase,
                        const rdma::ClusterRegistrationResult& result,
                        uint32_t nodes) {
  for (const rdma::NodeRegistrationFailure& failure : result.failures) {
    std::printf("%s FAILURE node=%s reason=%s\n", phase,
                failure.node_name.c_str(), failure.reason.c_str());
  }
  const bool ok = result.registered == nodes && result.failures.empty();
  std::printf("%s registered=%u of %u nodes, failures=%zu: %s\n", phase,
              result.registered, nodes, result.failures.size(),
              ok ? "PASS" : "FAIL");
  return ok;
}

bool connect_all(const char* phase, rdma::RdmaContext* context,
                 const rdma::NodeRegistry& registry) {
  bool ok = true;
  for (const std::string& name : registry.node_names()) {
    try {
      context->connect_peer(name, registry.peer_endpoint_for(name));
      std::printf("%s node=%s queue pair RTS\n", phase, name.c_str());
    } catch (const std::exception& e) {
      std::printf("%s node=%s connect FAILED: %s\n", phase, name.c_str(),
                  e.what());
      ok = false;
    }
  }
  return ok;
}

bool deregister(const char* phase, aerospike* as,
                const rdma::NodeRegistry& registry) {
  const rdma::ClusterDeregistrationResult result =
      rdma::deregister_all_nodes(as, nullptr, registry);
  for (const rdma::NodeRegistrationFailure& failure : result.failures) {
    std::printf("%s deregister FAILURE node=%s reason=%s\n", phase,
                failure.node_name.c_str(), failure.reason.c_str());
  }
  std::printf("%s deregistered=%u failures=%zu\n", phase, result.deregistered,
              result.failures.size());
  return result.failures.empty();
}

}  // namespace

int main(int argc, char** argv) {
  std::string host = "127.0.0.1", device = "rxe0", gate;
  int port = 3400, gid = 1;
  uint32_t windows = 4;
  size_t window_bytes = 1 << 20;
  for (int i = 1; i + 1 < argc; i += 2) {
    const std::string flag = argv[i], value = argv[i + 1];
    if (flag == "--host")
      host = value;
    else if (flag == "--port")
      port = std::atoi(value.c_str());
    else if (flag == "--device")
      device = value;
    else if (flag == "--gid")
      gid = std::atoi(value.c_str());
    else if (flag == "--windows")
      windows = std::strtoul(value.c_str(), nullptr, 10);
    else if (flag == "--window-bytes")
      window_bytes = std::strtoull(value.c_str(), nullptr, 10);
    else if (flag == "--restart-gate")
      gate = value;
    else {
      std::fprintf(stderr, "unknown flag %s\n", flag.c_str());
      return 2;
    }
  }

  as_config config;
  as_config_init(&config);
  as_config_add_host(&config, host.c_str(), static_cast<uint16_t>(port));
  aerospike as;
  aerospike_init(&as, &config);
  as_error err;
  if (aerospike_connect(&as, &err) != AEROSPIKE_OK) {
    std::fprintf(stderr, "connect %s:%d: %s\n", host.c_str(), port,
                 err.message);
    return 2;
  }
  const uint32_t nodes = cluster_size(&as);
  std::printf("cluster seed %s:%d: %u nodes\n", host.c_str(), port, nodes);

  const size_t bytes = windows * window_bytes;
  void* slab = std::aligned_alloc(4096, bytes);
  std::memset(slab, 0, bytes);
  bool ok = true;
  try {
    auto context = std::make_unique<rdma::RdmaContext>(
        device, static_cast<uint8_t>(gid), rdma::Transport::kRc);
    context->register_l1(slab, bytes, rdma::WindowPlan{windows, window_bytes});
    rdma::NodeRegistry registry;
    ok &= check_registration(
        "register-1",
        rdma::register_all_nodes(&as, nullptr, context.get(), &registry),
        nodes);
    ok &= print_registry("register-1", registry) == nodes;
    ok &= connect_all("register-1", context.get(), registry);

    if (!gate.empty()) {
      std::printf("waiting for %s\n", gate.c_str());
      std::fflush(stdout);
      while (access(gate.c_str(), F_OK) != 0) {
        usleep(200 * 1000);
      }
      std::printf("after restart: cluster has %u nodes\n", cluster_size(&as));
      // The restarted node lost its region, so only it refuses.
      const bool all_released = deregister("after-restart", &as, registry);
      std::printf("after-restart: restarted node forgot its region: %s\n",
                  all_released ? "NO" : "yes");
      ok &= !all_released;
      registry.invalidate_all();
      // A context's queue pairs are per node and created once, so the same
      // context cannot register again (recorded, not a check) ...
      const rdma::ClusterRegistrationResult same =
          rdma::register_all_nodes(&as, nullptr, context.get(), &registry);
      check_registration("register-2-same-context", same, nodes);
      // ... and re-registration needs a fresh context and memory region.
      context = std::make_unique<rdma::RdmaContext>(
          device, static_cast<uint8_t>(gid), rdma::Transport::kRc);
      context->register_l1(slab, bytes,
                           rdma::WindowPlan{windows, window_bytes});
      registry.invalidate_all();
      ok &= check_registration(
          "register-2-fresh-context",
          rdma::register_all_nodes(&as, nullptr, context.get(), &registry),
          nodes);
      ok &= print_registry("register-2-fresh-context", registry) == nodes;
      ok &= connect_all("register-2-fresh-context", context.get(), registry);
    }
    ok &= deregister("final", &as, registry);
  } catch (const std::exception& e) {
    std::printf("ERROR %s\n", e.what());
    ok = false;
  }
  std::free(slab);
  aerospike_close(&as, &err);
  aerospike_destroy(&as);
  std::printf("RESULT %s\n", ok ? "PASS" : "FAIL");
  return ok ? 0 : 1;
}
