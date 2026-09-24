// SPDX-License-Identifier: Apache-2.0

#ifdef LMCACHE_AEROSPIKE_RDMA

  #include "connector_pipelined_rdma.h"

  #include "kv_sink_fanout.h"
  #include "notification_depth.h"

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

uint32_t AerospikePipelinedRdmaDriver::desired_notification_depth() const {
  return rdma::desired_notification_depth(registration_.window_bytes,
                                          max_record_bytes_);
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
    context_->enable_layer_notifications(desired_notification_depth());
    max_notification_slots_ = context_->notification_depth();
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
    max_notification_slots_ = 0;
    fabric_ready_ = false;
    session_.reset();
    throw;
  } catch (...) {
    init_error_ = "unknown error during pipelined RDMA initialization";
    context_.reset();
    max_notification_slots_ = 0;
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

uint16_t AerospikePipelinedRdmaDriver::issue_pipelined_fetch(
    const rdma::PipelinedNodeInfoSender& send_info,
    const std::vector<rdma::ChunkPlacement>& placements,
    const std::vector<rdma::ChunkNodeBinding>& chunk_nodes,
    const std::vector<rdma::SlotDigest>& slot_digests) {
  return begin_and_send(send_info, [&](rdma::PipelinedFetchSession& session) {
    return session.begin_request(placements, chunk_nodes, slot_digests);
  });
}

uint16_t AerospikePipelinedRdmaDriver::issue_planned_fetch(
    const rdma::PipelinedNodeInfoSender& send_info,
    const std::vector<rdma::PlannedSlot>& slots) {
  return begin_and_send(send_info, [&](rdma::PipelinedFetchSession& session) {
    return session.begin_request_from_slots(slots);
  });
}

uint32_t AerospikePipelinedRdmaDriver::max_slots_per_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  return session_ ? session_->max_slots_per_request() : 0;
}

uint16_t AerospikePipelinedRdmaDriver::begin_and_send(
    const rdma::PipelinedNodeInfoSender& send_info,
    const std::function<uint16_t(rdma::PipelinedFetchSession&)>& begin) {
  uint16_t generation = 0;
  std::vector<std::pair<std::string, std::string>> commands;
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (!session_) {
      throw std::runtime_error(
          "Aerospike pipelined RDMA: pipelined fetch is not ready");
    }
    generation = begin(*session_);
    next_generation_ = static_cast<uint16_t>(generation + 1);
    commands = session_->pipelined_fetch_commands();
  }

  try {
    for (const auto& entry : commands) {
      const std::string& node_name = entry.first;
      const std::string& command = entry.second;
      std::string reply;
      try {
        reply = send_info(node_name, command);
      } catch (...) {
        reply = rdma::declined_reply_for_command(command);
      }
      std::lock_guard<std::mutex> lock(mu_);
      if (!session_ || !session_->has_active_request()) {
        throw std::runtime_error(
            "Aerospike pipelined RDMA: session ended during fetch issue");
      }
      session_->on_node_reply(node_name, command, reply, generation);
    }
  } catch (...) {
    std::lock_guard<std::mutex> lock(mu_);
    if (session_ && session_->has_active_request()) {
      session_->abandon_request();
    }
    throw;
  }
  return generation;
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

bool AerospikePipelinedRdmaDriver::is_layer_ready(
    uint32_t layer_id, uint16_t request_generation) const {
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
  return session_->is_layer_ready(layer_id, request_generation);
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
      max_record_bytes_, registration_.window_bytes, max_notification_slots_);
  session_->restore_generation_counter(next_generation_);
}

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
