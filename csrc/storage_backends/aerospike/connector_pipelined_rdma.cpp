// SPDX-License-Identifier: Apache-2.0

#ifdef LMCACHE_AEROSPIKE_RDMA

  #include "connector_pipelined_rdma.h"

  #include "kv_sink_fanout.h"

  #include <stdexcept>
  #include <utility>

namespace lmcache {
namespace connector {
namespace {

constexpr uint32_t kDefaultNotificationDepth = 4096;

rdma::WindowPlan window_plan_from_registration(
    const L1RdmaRegistration& registration) {
  rdma::WindowPlan plan;
  plan.window_count = registration.window_count;
  plan.window_bytes = registration.window_bytes;
  return plan;
}

}  // namespace

AerospikePipelinedRdmaDriver::AerospikePipelinedRdmaDriver(
    L1RdmaRegistration registration, std::string namespace_name,
    size_t max_record_bytes)
    : registration_(std::move(registration)),
      namespace_name_(std::move(namespace_name)),
      max_record_bytes_(max_record_bytes) {}

bool AerospikePipelinedRdmaDriver::is_ready() const {
  std::lock_guard<std::mutex> lock(mu_);
  return ready_;
}

void AerospikePipelinedRdmaDriver::initialize(aerospike* client) {
  std::lock_guard<std::mutex> lock(mu_);
  if (ready_) {
    return;
  }
  if (!registration_.is_enabled()) {
    return;
  }
  if (initialized_) {
    return;
  }
  if (registration_.base == 0 || registration_.size == 0) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: L1 base and size must be set before init");
  }
  if (registration_.window_count == 0 || registration_.window_bytes == 0) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: window_count and window_bytes must be "
        "non-zero");
  }

  const rdma::Transport transport =
      rdma::transport_from_string(registration_.transport);
  context_ = std::make_unique<rdma::RdmaContext>(
      registration_.device_name, static_cast<uint8_t>(registration_.gid_index),
      transport);
  context_->enable_layer_notifications(kDefaultNotificationDepth);
  context_->register_l1(reinterpret_cast<void*>(registration_.base),
                        registration_.size,
                        window_plan_from_registration(registration_));
  context_->create_queue_pair();

  const rdma::ClusterRegistrationResult result = rdma::register_all_nodes(
      client, nullptr, context_->local_endpoint(), 0, &registry_);
  if (result.registered == 0) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: kv-sink-register fanout registered no "
        "nodes");
  }

  ready_ = registry_.valid_count() > 0;
  initialized_ = true;
  ensure_session();
}

void AerospikePipelinedRdmaDriver::set_object_group_layouts(
    std::vector<rdma::ObjectGroupLayout> layouts) {
  std::lock_guard<std::mutex> lock(mu_);
  planner_ = std::make_unique<rdma::SlotPlanner>(std::move(layouts));
  ensure_session();
}

void AerospikePipelinedRdmaDriver::ensure_session() {
  if (!planner_ || !ready_) {
    session_.reset();
    return;
  }
  session_ = std::make_unique<rdma::PipelinedFetchSession>(
      *planner_, registry_, namespace_name_, max_record_bytes_,
      registration_.window_bytes);
}

uint16_t AerospikePipelinedRdmaDriver::begin_request(
    const std::vector<rdma::ChunkPlacement>& placements,
    const std::vector<rdma::ChunkNodeBinding>& chunk_nodes,
    const std::vector<rdma::SlotDigest>& slot_digests) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: pipelined fetch is not ready");
  }
  return session_->begin_request(placements, chunk_nodes, slot_digests);
}

std::map<std::string, std::string>
AerospikePipelinedRdmaDriver::pipelined_fetch_commands() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return {};
  }
  return session_->pipelined_fetch_commands();
}

void AerospikePipelinedRdmaDriver::on_node_reply(const std::string& node_name,
                                                 const std::string& reply) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return;
  }
  session_->on_node_reply(node_name, reply);
}

void AerospikePipelinedRdmaDriver::poll_notifications() {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_ || !context_) {
    return;
  }
  const std::vector<uint32_t> events =
      context_->poll_notifications(kDefaultNotificationDepth);
  session_->on_notifications(events);
}

bool AerospikePipelinedRdmaDriver::has_active_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return false;
  }
  return session_->has_active_request();
}

bool AerospikePipelinedRdmaDriver::is_layer_ready(uint32_t layer_id) const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return false;
  }
  return session_->is_layer_ready(layer_id);
}

std::vector<uint32_t> AerospikePipelinedRdmaDriver::unservable_layers() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return {};
  }
  return session_->unservable_layers();
}

void AerospikePipelinedRdmaDriver::finish_request() {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return;
  }
  session_->finish_request();
}

void AerospikePipelinedRdmaDriver::abandon_request() {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return;
  }
  session_->abandon_request();
}

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
