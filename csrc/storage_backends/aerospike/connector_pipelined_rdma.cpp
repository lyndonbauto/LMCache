// SPDX-License-Identifier: Apache-2.0

#ifdef LMCACHE_AEROSPIKE_RDMA

  #include "connector_pipelined_rdma.h"

  #include "kv_sink_fanout.h"

  #include <algorithm>
  #include <stdexcept>
  #include <utility>

namespace lmcache {
namespace connector {
namespace {

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
  return fabric_ready_ && session_ != nullptr;
}

std::string AerospikePipelinedRdmaDriver::init_error_message() const {
  std::lock_guard<std::mutex> lock(mu_);
  return init_error_;
}

uint32_t AerospikePipelinedRdmaDriver::notification_depth_cap() const {
  if (max_record_bytes_ == 0 || registration_.window_bytes == 0) {
    return 1;
  }
  const size_t slots =
      (registration_.window_bytes + max_record_bytes_ - 1) / max_record_bytes_;
  return static_cast<uint32_t>(
      std::min(slots, static_cast<size_t>(rdma::kMaxSlotsPerRequest)));
}

void AerospikePipelinedRdmaDriver::initialize(aerospike* client) {
  std::lock_guard<std::mutex> lock(mu_);
  if (fabric_ready_ && session_ != nullptr) {
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

  try {
    const rdma::Transport transport =
        rdma::transport_from_string(registration_.transport);
    context_ = std::make_unique<rdma::RdmaContext>(
        registration_.device_name,
        static_cast<uint8_t>(registration_.gid_index), transport);
    context_->enable_layer_notifications(notification_depth_cap());
    context_->register_l1(reinterpret_cast<void*>(registration_.base),
                          registration_.size,
                          window_plan_from_registration(registration_));

    const rdma::ClusterRegistrationResult result = rdma::register_all_nodes(
        client, nullptr, context_.get(), 0, &registry_);
    if (result.registered == 0) {
      throw std::runtime_error(
          "Aerospike pipelined RDMA: kv-sink-register fanout registered no "
          "nodes");
    }

    for (const std::string& node_name : registry_.node_names()) {
      const rdma::PeerEndpoint peer = registry_.peer_endpoint_for(node_name);
      context_->connect_peer(node_name, peer);
    }
    context_->arm_notifications();

    fabric_ready_ = true;
    init_error_.clear();
    initialized_ = true;
    ensure_session();
  } catch (const std::exception& e) {
    init_error_ = e.what();
    context_.reset();
    fabric_ready_ = false;
    session_.reset();
    throw;
  } catch (...) {
    init_error_ = "unknown error during pipelined RDMA initialization";
    context_.reset();
    fabric_ready_ = false;
    session_.reset();
    throw;
  }
}

void AerospikePipelinedRdmaDriver::set_object_group_layouts(
    std::vector<rdma::ObjectGroupLayout> layouts) {
  std::lock_guard<std::mutex> lock(mu_);
  if (session_ && session_->has_active_request()) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: cannot replace layouts during an active "
        "fetch");
  }
  session_.reset();
  planner_ = std::make_unique<rdma::SlotPlanner>(std::move(layouts));
  ensure_session();
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
  const uint16_t generation =
      session_->begin_request(placements, chunk_nodes, slot_digests);
  next_generation_ = static_cast<uint16_t>(generation + 1);
  return generation;
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
                                                 const std::string& reply,
                                                 uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return;
  }
  session_->on_node_reply(node_name, reply, generation);
}

void AerospikePipelinedRdmaDriver::poll_notifications() {
  rdma::RdmaContext* context = nullptr;
  rdma::PipelinedFetchSession* session = nullptr;
  {
    std::lock_guard<std::mutex> lock(mu_);
    context = context_.get();
    session = session_.get();
  }
  if (context == nullptr || session == nullptr) {
    return;
  }
  const std::vector<uint32_t> events =
      context->poll_notifications(context->notification_depth());
  std::lock_guard<std::mutex> lock(mu_);
  if (session_) {
    session_->on_notifications(events);
  }
}

bool AerospikePipelinedRdmaDriver::has_active_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!session_) {
    return false;
  }
  return session_->has_active_request();
}

bool AerospikePipelinedRdmaDriver::is_layer_ready(uint32_t layer_id) const {
  rdma::RdmaContext* context = nullptr;
  {
    std::lock_guard<std::mutex> lock(mu_);
    context = context_.get();
  }
  if (context != nullptr) {
    const std::vector<uint32_t> events =
        context->poll_notifications(context->notification_depth());
    std::lock_guard<std::mutex> lock(mu_);
    if (session_) {
      session_->on_notifications(events);
    }
  }
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

void AerospikePipelinedRdmaDriver::ensure_session() {
  if (!planner_ || !fabric_ready_) {
    session_.reset();
    return;
  }
  session_ = std::make_unique<rdma::PipelinedFetchSession>(
      *planner_, registry_, namespace_name_, max_record_bytes_,
      max_record_bytes_, registration_.window_bytes, notification_depth_cap());
  session_->restore_generation_counter(next_generation_);
}

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
