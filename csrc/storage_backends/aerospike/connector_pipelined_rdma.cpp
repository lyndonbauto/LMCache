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
  return fabric_ready_ && pool_ != nullptr;
}

std::string AerospikePipelinedRdmaDriver::init_error_message() const {
  std::lock_guard<std::mutex> lock(mu_);
  return init_error_.empty() ? layout_error_ : init_error_;
}

uint32_t AerospikePipelinedRdmaDriver::desired_notification_depth() const {
  return rdma::desired_notification_depth(registration_.window_bytes,
                                          max_record_bytes_,
                                          registration_.window_count);
}

void AerospikePipelinedRdmaDriver::initialize(aerospike* client) {
  std::lock_guard<std::mutex> lock(mu_);
  if (fabric_ready_ && pool_ != nullptr) {
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
  if (registration_.window_count > rdma::kMaxFetchWindows) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: window_count " +
        std::to_string(registration_.window_count) + " is above the " +
        std::to_string(rdma::kMaxFetchWindows) + " windows one pool runs");
  }

  try {
    const rdma::Transport transport =
        rdma::transport_from_string(registration_.transport);
    context_ = std::make_unique<rdma::RdmaContext>(
        registration_.device_name,
        static_cast<uint8_t>(registration_.gid_index), transport);
    context_->enable_layer_notifications(desired_notification_depth());
    max_notification_slots_ = context_->notification_depth();
    if (max_notification_slots_ < registration_.window_count) {
      throw std::runtime_error(
          "Aerospike pipelined RDMA: the device allows " +
          std::to_string(max_notification_slots_) +
          " notifications, fewer than one per window for " +
          std::to_string(registration_.window_count) +
          " windows; reduce rdma window_count");
    }
    context_->register_l1(reinterpret_cast<void*>(registration_.base),
                          registration_.size,
                          window_plan_from_registration(registration_));

    const rdma::ClusterRegistrationResult result =
        rdma::register_all_nodes(client, nullptr, context_.get(), &registry_);
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
    ensure_pool();
  } catch (const std::exception& e) {
    init_error_ = e.what();
    context_.reset();
    max_notification_slots_ = 0;
    fabric_ready_ = false;
    pool_.reset();
    throw;
  } catch (...) {
    init_error_ = "unknown error during pipelined RDMA initialization";
    context_.reset();
    max_notification_slots_ = 0;
    fabric_ready_ = false;
    pool_.reset();
    throw;
  }
}

void AerospikePipelinedRdmaDriver::set_object_group_layouts(
    std::vector<rdma::ObjectGroupLayout> layouts) {
  std::lock_guard<std::mutex> lock(mu_);
  if (pool_ && pool_->has_active_request()) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: cannot replace layouts during an active "
        "fetch");
  }
  if (pool_) {
    generation_counters_ = pool_->generation_counters();
  }
  pool_.reset();
  layout_error_ = rdma::window_fit_error(layouts, registration_.window_bytes,
                                         registration_.align_bytes);
  planner_ = std::make_unique<rdma::SlotPlanner>(std::move(layouts));
  ensure_pool();
}

uint16_t AerospikePipelinedRdmaDriver::issue_pipelined_fetch(
    const rdma::PipelinedNodeInfoSender& send_info,
    const std::vector<rdma::ChunkPlacement>& placements,
    const std::vector<rdma::ChunkNodeBinding>& chunk_nodes,
    const std::vector<rdma::SlotDigest>& slot_digests) {
  return begin_and_send(send_info, [&](rdma::PipelinedFetchPool& pool) {
    return pool.begin_request(placements, chunk_nodes, slot_digests);
  });
}

uint16_t AerospikePipelinedRdmaDriver::issue_planned_fetch(
    const rdma::PipelinedNodeInfoSender& send_info,
    const std::vector<rdma::PlannedSlot>& slots) {
  return begin_and_send(send_info, [&](rdma::PipelinedFetchPool& pool) {
    return pool.begin_request_from_slots(slots);
  });
}

uint32_t AerospikePipelinedRdmaDriver::max_slots_per_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  return pool_ ? pool_->max_slots_per_request() : 0;
}

uint16_t AerospikePipelinedRdmaDriver::begin_and_send(
    const rdma::PipelinedNodeInfoSender& send_info,
    const std::function<uint16_t(rdma::PipelinedFetchPool&)>& begin) {
  uint16_t generation = 0;
  std::vector<std::pair<std::string, std::string>> commands;
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (!pool_) {
      throw std::runtime_error(
          "Aerospike pipelined RDMA: pipelined fetch is not ready");
    }
    generation = begin(*pool_);
    commands = pool_->pipelined_fetch_commands(generation);
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
      if (!pool_ || !pool_->is_active(generation)) {
        throw std::runtime_error(
            "Aerospike pipelined RDMA: fetch ended during issue");
      }
      pool_->on_node_reply(node_name, command, reply, generation);
    }
  } catch (...) {
    std::lock_guard<std::mutex> lock(mu_);
    if (pool_) {
      pool_->abandon_request(generation);
    }
    throw;
  }
  return generation;
}

void AerospikePipelinedRdmaDriver::poll_notifications() {
  rdma::RdmaContext* context = nullptr;
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (!pool_) {
      return;
    }
    context = context_.get();
  }
  if (context == nullptr) {
    return;
  }
  const std::vector<uint32_t> events =
      context->poll_notifications(context->notification_depth());
  std::lock_guard<std::mutex> lock(mu_);
  if (pool_) {
    pool_->on_notifications(events);
  }
}

bool AerospikePipelinedRdmaDriver::has_active_request() const {
  std::lock_guard<std::mutex> lock(mu_);
  return pool_ && pool_->has_active_request();
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
    if (pool_) {
      pool_->on_notifications(events);
    }
  }
  std::lock_guard<std::mutex> lock(mu_);
  return pool_ && pool_->is_layer_ready(layer_id, request_generation);
}

std::vector<uint32_t> AerospikePipelinedRdmaDriver::unservable_layers(
    uint16_t generation) const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!pool_) {
    return {};
  }
  return pool_->unservable_layers(generation);
}

void AerospikePipelinedRdmaDriver::finish_request(uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  if (!pool_) {
    throw std::runtime_error(
        "Aerospike pipelined RDMA: pipelined fetch is not ready");
  }
  pool_->finish_request(generation);
}

void AerospikePipelinedRdmaDriver::abandon_request(uint16_t generation) {
  std::lock_guard<std::mutex> lock(mu_);
  if (pool_) {
    pool_->abandon_request(generation);
  }
}

void AerospikePipelinedRdmaDriver::ensure_pool() {
  if (!planner_ || !fabric_ready_ || !layout_error_.empty()) {
    pool_.reset();
    return;
  }
  pool_ = std::make_unique<rdma::PipelinedFetchPool>(
      *planner_, registry_, namespace_name_, max_record_bytes_,
      max_record_bytes_, registration_.window_bytes, registration_.window_count,
      max_notification_slots_);
  if (generation_counters_.size() == registration_.window_count) {
    pool_->restore_generation_counters(generation_counters_);
  }
}

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
