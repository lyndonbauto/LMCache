// SPDX-License-Identifier: Apache-2.0

#ifdef LMCACHE_AEROSPIKE_RDMA

  #include "connector_sink_fetch.h"

  #include <aerospike/aerospike_batch.h>
  #include <aerospike/as_batch.h>
  #include <aerospike/as_cluster.h>
  #include <aerospike/as_node.h>

  #include <cstdio>
  #include <limits>
  #include <stdexcept>
  #include <utility>

namespace lmcache {
namespace connector {
namespace {

// Batches in flight at once, across every fetch. Each worker blocks in one
// synchronous batch read, and the server orders what they queue by priority,
// so this bounds concurrency without deciding order.
constexpr size_t kBatchWorkers = 16;

// Upper bound on windows, so a bad config cannot build a huge table.
constexpr uint32_t kMaxWindows = 4096;

// The sink transport for an L1RdmaRegistration transport name. Never
// "local": the patched client's same-host stand-in lets the server write
// into any process it names.
const char* sink_transport_name(const std::string& transport) {
  if (transport == "RC") {
    return "rc";
  }
  if (transport == "SRD") {
    return "srd";
  }
  throw std::runtime_error("Aerospike sink fetch: unsupported transport '" +
                           transport + "'; expected RC or SRD");
}

}  // namespace

AerospikeSinkFetchDriver::AerospikeSinkFetchDriver(
    L1RdmaRegistration registration, std::string namespace_name,
    std::string set_name)
    : registration_(std::move(registration)),
      namespace_name_(std::move(namespace_name)),
      set_name_(std::move(set_name)) {
  as_policy_batch_init(&batch_policy_);
}

AerospikeSinkFetchDriver::~AerospikeSinkFetchDriver() { stop_workers(); }

void AerospikeSinkFetchDriver::initialize(aerospike* client) {
  std::lock_guard<std::mutex> lock(mu_);
  if (sink_ != nullptr || !registration_.is_enabled()) {
    return;
  }
  try {
    if (registration_.base == 0 || registration_.size == 0) {
      throw std::runtime_error(
          "Aerospike sink fetch: L1 base and size must be set before init");
    }
    if (registration_.window_count == 0 || registration_.window_bytes == 0) {
      throw std::runtime_error(
          "Aerospike sink fetch: window_count and window_bytes must be "
          "non-zero");
    }
    if (registration_.window_count > kMaxWindows) {
      throw std::runtime_error("Aerospike sink fetch: window_count " +
                               std::to_string(registration_.window_count) +
                               " is above " + std::to_string(kMaxWindows));
    }
    const size_t range_bytes = static_cast<size_t>(registration_.window_count) *
                               registration_.window_bytes;
    if (range_bytes > registration_.size) {
      throw std::runtime_error(
          "Aerospike sink fetch: " +
          std::to_string(registration_.window_count) + " windows of " +
          std::to_string(registration_.window_bytes) +
          " bytes do not fit the " + std::to_string(registration_.size) +
          "-byte L1 slab");
    }
    if (registration_.fetch_timeout_ms == 0) {
      throw std::runtime_error(
          "Aerospike sink fetch: fetch_timeout_ms must be non-zero");
    }

    as_sink_config config;
    as_sink_config_init(&config);
    config.transport = sink_transport_name(registration_.transport);
    config.device = registration_.device_name.empty()
                        ? nullptr
                        : registration_.device_name.c_str();
    config.gid_index = static_cast<int>(registration_.gid_index);
    config.queue_pairs = registration_.queue_pairs;

    as_error err;
    as_sink* created = nullptr;
    const as_status status = aerospike_sink_create(
        client, &err, reinterpret_cast<void*>(registration_.base), range_bytes,
        &config, &created);
    if (status != AEROSPIKE_OK || created == nullptr) {
      throw std::runtime_error(
          std::string("Aerospike sink fetch: aerospike_sink_create failed "
                      "(no node accepted the registration): ") +
          err.message);
    }
    // The client accepts a sink some nodes refused: their rows fail with
    // AEROSPIKE_ERR_SINK_UNKNOWN_REGION and a refresh retries them.
    uint32_t registered = 0;
    for (uint32_t i = 0; i < created->n_nodes; ++i) {
      registered += created->nodes[i].registered ? 1 : 0;
    }
    if (registered < created->n_nodes) {
      std::fprintf(stderr,
                   "LMCache Aerospike sink fetch: sink registered on %u of %u "
                   "nodes; rows for the others reload whole objects until a "
                   "refresh registers them: %s\n",
                   registered, created->n_nodes, err.message);
    }

    // A late row must fail rather than write after LMCache gave up on it,
    // so rows are never retried and the deadline is the fetch timeout. The
    // server fails a queued write past the same deadline, but one already
    // on the wire still lands; the window leaser's quarantine covers that.
    batch_policy_.base.total_timeout = registration_.fetch_timeout_ms;
    batch_policy_.base.socket_timeout = registration_.fetch_timeout_ms;
    batch_policy_.base.max_retries = 0;
    batch_policy_.concurrent = true;
    // Sink rows read segments without their meta record; touching them
    // would let the meta record expire under still-live segments.
    batch_policy_.read_touch_ttl_percent = -1;

    table_ = std::make_unique<sink::SinkFetchTable>(registration_.window_bytes,
                                                    registration_.window_count);
    client_ = client;
    sink_ = created;
    init_error_.clear();
    start_workers();
  } catch (const std::exception& e) {
    init_error_ = e.what();
    throw;
  }
}

bool AerospikeSinkFetchDriver::is_ready() const {
  std::lock_guard<std::mutex> lock(mu_);
  return sink_ != nullptr && !shut_down_ && layouts_set_ &&
         layout_error_.empty();
}

std::string AerospikeSinkFetchDriver::init_error_message() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (!init_error_.empty()) {
    return init_error_;
  }
  if (shut_down_) {
    return "Aerospike sink fetch: the connector is closed";
  }
  return layout_error_;
}

std::string AerospikeSinkFetchDriver::node_name() const {
  std::lock_guard<std::mutex> lock(mu_);
  if (sink_ == nullptr || shut_down_) {
    throw std::runtime_error(
        "Aerospike sink fetch: pipelined fetch did not initialize");
  }
  as_nodes* nodes = as_nodes_reserve(client_->cluster);
  if (nodes == nullptr || nodes->size == 0) {
    if (nodes != nullptr) {
      as_nodes_release(nodes);
    }
    throw std::runtime_error("Aerospike sink fetch: the cluster has no nodes");
  }
  std::string name(nodes->array[0]->name);
  as_nodes_release(nodes);
  return name;
}

void AerospikeSinkFetchDriver::set_object_group_layouts(
    std::vector<rdma::ObjectGroupLayout> layouts) {
  std::lock_guard<std::mutex> lock(mu_);
  if (table_ && table_->has_active_request()) {
    throw std::runtime_error(
        "Aerospike sink fetch: cannot replace layouts during an active fetch");
  }
  // Plans come from the caller, so the planner is built only because its
  // constructor rejects a malformed layout.
  const rdma::SlotPlanner validated(layouts);
  layout_error_ = rdma::window_fit_error(layouts, registration_.window_bytes,
                                         registration_.align_bytes);
  layouts_set_ = true;
}

uint16_t AerospikeSinkFetchDriver::issue(std::vector<sink::SinkSlot> slots) {
  for (size_t i = 0; i < slots.size(); ++i) {
    if (slots[i].length > std::numeric_limits<uint32_t>::max()) {
      throw std::invalid_argument(
          "Aerospike sink fetch: slot " + std::to_string(i) + " length " +
          std::to_string(slots[i].length) + " does not fit a sink row");
    }
  }
  if (!is_ready()) {
    throw std::runtime_error(
        "Aerospike sink fetch: pipelined fetch is not ready");
  }
  auto shared =
      std::make_shared<const std::vector<sink::SinkSlot>>(std::move(slots));
  sink::BegunFetch begun = table_->begin(*shared);
  {
    std::lock_guard<std::mutex> lock(queue_mu_);
    for (sink::LayerBatch& batch : begun.batches) {
      queue_.push_back({std::move(batch), shared});
    }
  }
  queue_cv_.notify_all();
  return begun.generation;
}

uint32_t AerospikeSinkFetchDriver::max_slots_per_request() const {
  return is_ready() ? table_->max_slots_per_request() : 0;
}

bool AerospikeSinkFetchDriver::is_layer_ready(uint32_t layer_id,
                                              uint16_t generation) const {
  return table_ && table_->is_layer_ready(layer_id, generation);
}

std::vector<uint32_t> AerospikeSinkFetchDriver::unservable_layers(
    uint16_t generation) const {
  if (!table_) {
    return {};
  }
  return table_->unservable_layers(generation);
}

void AerospikeSinkFetchDriver::finish_request(uint16_t generation) {
  if (!table_) {
    throw std::runtime_error(
        "Aerospike sink fetch: pipelined fetch is not ready");
  }
  table_->finish(generation);
}

void AerospikeSinkFetchDriver::abandon_request(uint16_t generation) {
  if (table_) {
    table_->abandon(generation);
  }
}

bool AerospikeSinkFetchDriver::window_settled(uint32_t window_index) const {
  if (!table_ || window_index >= registration_.window_count) {
    return false;
  }
  return table_->window_settled(window_index);
}

void AerospikeSinkFetchDriver::shutdown() {
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (sink_ == nullptr || shut_down_) {
      return;
    }
    shut_down_ = true;
  }
  stop_workers();
  // Deregisters from every node, so the server stops writing into L1.
  aerospike_sink_destroy(sink_);
  sink_ = nullptr;
}

void AerospikeSinkFetchDriver::start_workers() {
  workers_.reserve(kBatchWorkers);
  for (size_t i = 0; i < kBatchWorkers; ++i) {
    workers_.emplace_back([this] { run_worker(); });
  }
}

void AerospikeSinkFetchDriver::stop_workers() {
  {
    std::lock_guard<std::mutex> lock(queue_mu_);
    stopping_ = true;
    queue_.clear();
  }
  queue_cv_.notify_all();
  for (std::thread& worker : workers_) {
    if (worker.joinable()) {
      worker.join();
    }
  }
  workers_.clear();
}

void AerospikeSinkFetchDriver::run_worker() {
  while (true) {
    QueuedBatch queued;
    {
      std::unique_lock<std::mutex> lock(queue_mu_);
      queue_cv_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
      if (stopping_) {
        return;
      }
      queued = std::move(queue_.front());
      queue_.pop_front();
    }
    sink::BatchEnd end = sink::BatchEnd::kMayStillWrite;
    try {
      end = execute(queued);
    } catch (const std::exception& e) {
      std::fprintf(stderr, "LMCache Aerospike sink fetch: layer %u: %s\n",
                   queued.batch.layer_id, e.what());
      for (const uint32_t slot : queued.batch.slot_indices) {
        table_->on_slot_result(queued.batch.generation, slot, false,
                               queued.batch.token);
      }
    }
    table_->on_batch_done(queued.batch.window, end);
  }
}

namespace {

// Whether a row with this final result had its write either land before
// the answer (AEROSPIKE_OK) or never start (the record or region is
// missing). Any other result, a timeout above all, leaves it unknown.
bool row_cannot_write_later(as_status result) {
  return result == AEROSPIKE_OK || result == AEROSPIKE_ERR_RECORD_NOT_FOUND ||
         result == AEROSPIKE_ERR_SINK_UNKNOWN_REGION;
}

}  // namespace

// A row that fails with AEROSPIKE_ERR_SINK_UNKNOWN_REGION found no usable
// region on its node: the node restarted, reclaimed the sink, never had it, or
// dropped it after a failed write. The row's bytes are not complete either
// way, and a retry rewrites the same destination, so those rows get one retry
// after the sink is refreshed; every other failure is final for the slot.
sink::BatchEnd AerospikeSinkFetchDriver::execute(const QueuedBatch& queued) {
  const sink::LayerBatch& batch = queued.batch;
  sink::BatchEnd end = sink::BatchEnd::kSettled;
  std::vector<uint32_t> pending = batch.slot_indices;
  for (int attempt = 0; attempt < 2 && !pending.empty(); ++attempt) {
    if (!table_->is_active(batch.generation, batch.token)) {
      return end;
    }
    uint64_t seen_epoch = 0;
    {
      std::lock_guard<std::mutex> lock(refresh_mu_);
      seen_epoch = refresh_epoch_;
    }
    const std::vector<as_status> results = read_rows(queued, pending);
    std::vector<uint32_t> unknown_region;
    for (size_t i = 0; i < pending.size(); ++i) {
      if (!row_cannot_write_later(results[i])) {
        end = sink::BatchEnd::kMayStillWrite;
      }
      if (results[i] == AEROSPIKE_ERR_SINK_UNKNOWN_REGION && attempt == 0) {
        unknown_region.push_back(pending[i]);
        continue;
      }
      table_->on_slot_result(batch.generation, pending[i],
                             results[i] == AEROSPIKE_OK, batch.token);
    }
    if (!unknown_region.empty()) {
      refresh_sink(seen_epoch);
    }
    pending = std::move(unknown_region);
  }
  return end;
}

std::vector<as_status> AerospikeSinkFetchDriver::read_rows(
    const QueuedBatch& queued, const std::vector<uint32_t>& slot_indices) {
  as_batch_records* records =
      as_batch_records_create(static_cast<uint32_t>(slot_indices.size()));
  for (const uint32_t index : slot_indices) {
    const sink::SinkSlot& slot = (*queued.slots)[index];
    as_batch_read_record* row = as_batch_read_reserve(records);
    // Borrows the key string, which `queued.slots` keeps alive.
    as_key_init_str(&row->key, namespace_name_.c_str(), set_name_.c_str(),
                    slot.record_key.c_str());
    row->read_all_bins = true;
    row->sink = sink_;
    row->sink_offset = slot.offset;
    row->sink_length = static_cast<uint32_t>(slot.length);
    row->sink_priority = queued.batch.priority;
    // Reserved rows are zeroed, and zero is AEROSPIKE_OK: a batch that fails
    // before the client resets its rows must not read as landed.
    row->result = AEROSPIKE_NO_RESPONSE;
  }

  as_error err;
  aerospike_batch_read(client_, &err, &batch_policy_, records);

  std::vector<as_status> results;
  results.reserve(slot_indices.size());
  for (uint32_t i = 0; i < records->list.size; ++i) {
    const auto* row = static_cast<const as_batch_read_record*>(
        as_vector_get(&records->list, i));
    results.push_back(row->result);
  }
  as_batch_records_destroy(records);
  return results;
}

void AerospikeSinkFetchDriver::refresh_sink(uint64_t seen_epoch) {
  std::lock_guard<std::mutex> lock(refresh_mu_);
  if (refresh_epoch_ != seen_epoch) {
    return;
  }
  as_error err;
  if (aerospike_sink_refresh(sink_, &err) != AEROSPIKE_OK) {
    std::fprintf(stderr,
                 "LMCache Aerospike sink fetch: sink refresh left a node "
                 "unregistered: %s\n",
                 err.message);
  }
  ++refresh_epoch_;
}

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
