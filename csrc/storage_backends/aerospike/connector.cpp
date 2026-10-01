// SPDX-License-Identifier: Apache-2.0

#include "connector.h"

#include <aerospike/aerospike_batch.h>
#include <aerospike/aerospike_info.h>
#include <aerospike/aerospike_key.h>
#include <aerospike/as_cluster.h>
#include <aerospike/as_config.h>
#include <aerospike/as_node.h>
#include <aerospike/as_partition.h>
#include <aerospike/as_record.h>
#include <aerospike/as_status.h>

#include <algorithm>
#include <cassert>
#include <chrono>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <functional>
#include <random>
#include <sstream>
#include <stdexcept>
#include <thread>

namespace lmcache {
namespace connector {
namespace {

constexpr size_t kSafetyMarginBytes = 64 * 1024;
constexpr size_t kDefaultRecordCapBytes = 1024 * 1024;
constexpr const char* kBinPayload = "b";
constexpr const char* kBinState = "state";
constexpr const char* kBinNseg = "nseg";
constexpr const char* kBinSegBytes = "seg_b";
constexpr const char* kBinPlaneBytes = "plane_b";
constexpr const char* kBinRuns = "runs";
constexpr const char* kBinTotalBytes = "tot_b";
constexpr const char* kBinVersion = "ver";
constexpr const char* kBinCreatedAt = "created_at";
constexpr const char* kBinPin = "pin";
constexpr const char* kBinWriteId = "wid";
constexpr const char* kReady = "ready";
// Read by read_write_ids(); the batch API takes non-const names but only
// reads them.
char kBinStateName[] = "state";
char kBinWriteIdName[] = "wid";
char* kWriteIdBins[] = {kBinStateName, kBinWriteIdName};

// 64 random bits as 16 lowercase hex digits. Each thread seeds its own
// generator, so concurrent stores never share state.
std::string new_write_id() {
  thread_local std::mt19937_64 rng(
      (static_cast<uint64_t>(std::random_device{}()) << 32) ^
      static_cast<uint64_t>(std::random_device{}()) ^
      static_cast<uint64_t>(
          std::chrono::steady_clock::now().time_since_epoch().count()) ^
      std::hash<std::thread::id>{}(std::this_thread::get_id()));
  char hex[17];
  std::snprintf(hex, sizeof(hex), "%016" PRIx64, rng());
  return std::string(hex);
}

// The write ID a meta record names, or "" for inline and old-layout records.
std::string write_id_of(as_record* rec) {
  const char* wid = as_record_get_str(rec, kBinWriteId);
  return wid == nullptr ? std::string() : std::string(wid);
}

std::vector<std::string> split(const std::string& s, char sep) {
  std::vector<std::string> out;
  size_t start = 0;
  for (size_t i = 0; i <= s.size(); ++i) {
    if (i == s.size() || s[i] == sep) {
      out.emplace_back(s.substr(start, i - start));
      start = i + 1;
    }
  }
  return out;
}

std::string status_message(as_status status, const as_error& err) {
  std::ostringstream oss;
  oss << "Aerospike status " << static_cast<int>(status);
  if (err.message[0] != '\0') {
    oss << ": " << err.message;
  }
  return oss.str();
}

// `as_record_get_int64` requires a default to return when a bin is absent or
// not an int64, so a fallback is unavoidable at the API level. This helper also
// treats non-positive values as "use the fallback" because the count/size bins
// written by put_meta_record() are always >= 1 on a healthy record. Integrity-
// critical fields (e.g. total size) are validated explicitly by the caller and
// must not rely on this fallback to mask a missing bin.
int64_t positive_int_bin(as_record* rec, const char* bin, int64_t fallback) {
  int64_t value = as_record_get_int64(rec, bin, fallback);
  return value > 0 ? value : fallback;
}

}  // namespace

AerospikeNativeConnector::AerospikeNativeConnector(
    std::string hosts, std::string ns, std::string set_name, int num_workers,
    uint32_t read_timeout_ms, uint32_t write_timeout_ms,
    uint32_t default_ttl_seconds, size_t target_segment_bytes,
    size_t max_record_bytes, std::string username, std::string password,
    L1RdmaRegistration l1_rdma_registration, size_t plane_bytes)
    : ConnectorBase(num_workers),
      hosts_(std::move(hosts)),
      ns_(std::move(ns)),
      set_name_(std::move(set_name)),
      read_timeout_ms_(read_timeout_ms),
      write_timeout_ms_(write_timeout_ms),
      default_ttl_seconds_(default_ttl_seconds),
      plane_bytes_(plane_bytes),
      l1_rdma_registration_(std::move(l1_rdma_registration)) {
  as_config config;
  as_config_init(&config);
  config.thread_pool_size = static_cast<uint32_t>(std::max(num_workers, 1));

  for (const auto& [host, port] : parse_hosts(hosts_)) {
    as_config_add_host(&config, host.c_str(), port);
  }
  if (!username.empty()) {
    if (!as_config_set_user(&config, username.c_str(), password.c_str())) {
      throw std::runtime_error("invalid Aerospike username/password config");
    }
  }

  configure_policies();
  aerospike_init(&as_, &config);

  try {
    as_error err;
    if (aerospike_connect(&as_, &err) != AEROSPIKE_OK) {
      throw std::runtime_error("aerospike_connect failed: " +
                               status_message(err.code, err));
    }
    connected_ = true;

    size_t discovered =
        max_record_bytes == 0 ? discover_record_cap() : max_record_bytes;
    if (discovered <= kSafetyMarginBytes) {
      throw std::runtime_error("Aerospike record cap is too small");
    }
    max_record_bytes_ = discovered - kSafetyMarginBytes;
    target_segment_bytes_ =
        target_segment_bytes == 0
            ? max_record_bytes_
            : std::min(target_segment_bytes, max_record_bytes_);
    single_record_threshold_bytes_ = target_segment_bytes_;

#ifdef LMCACHE_AEROSPIKE_RDMA
    if (l1_rdma_registration_.is_enabled()) {
      pipelined_rdma_ = std::make_unique<AerospikeSinkFetchDriver>(
          l1_rdma_registration_, ns_, set_name_);
      try_initialize_pipelined_rdma();
    }
#endif

    start_workers();
  } catch (...) {
    if (connected_) {
      as_error close_err;
      aerospike_close(&as_, &close_err);
      connected_ = false;
    }
    aerospike_destroy(&as_);
    throw;
  }
}

AerospikeNativeConnector::~AerospikeNativeConnector() { close(); }

void AerospikeNativeConnector::close() {
  {
    std::lock_guard<std::mutex> lk(close_mu_);
    if (closed_native_) {
      return;
    }
    closed_native_ = true;
  }

#ifdef LMCACHE_AEROSPIKE_RDMA
  // The shared client closes once the workers stop, and deregistering needs it.
  if (pipelined_rdma_ && connected_) {
    pipelined_rdma_->shutdown();
  }
#endif
  ConnectorBase<WorkerAerospikeConn>::close();
}

WorkerAerospikeConn AerospikeNativeConnector::create_connection() {
  WorkerAerospikeConn conn;
  conn.client = &as_;
  conn.ns = ns_;
  conn.set_name = set_name_;

  as_policy_read_init(&conn.read_policy);
  conn.read_policy.base.total_timeout = read_timeout_ms_;
  conn.read_policy.base.socket_timeout = read_timeout_ms_;
  conn.read_policy.base.max_retries = 2;
  conn.read_policy.key = AS_POLICY_KEY_DIGEST;
  conn.read_policy.replica = AS_POLICY_REPLICA_SEQUENCE;
  // 0: the namespace's default-read-touch-ttl-pct decides. A load reads the
  // meta record and every segment, so all of an object's records are
  // extended together.
  conn.read_policy.read_touch_ttl_percent = 0;

  conn.lookup_policy = conn.read_policy;
  conn.lookup_policy.read_touch_ttl_percent = -1;

  as_policy_write_init(&conn.write_policy);
  conn.write_policy.base.total_timeout = write_timeout_ms_;
  conn.write_policy.base.socket_timeout = write_timeout_ms_;
  conn.write_policy.base.max_retries = 0;
  conn.write_policy.key = AS_POLICY_KEY_DIGEST;
  conn.write_policy.exists = AS_POLICY_EXISTS_IGNORE;
  conn.write_policy.gen = AS_POLICY_GEN_IGNORE;
  conn.write_policy.commit_level = AS_POLICY_COMMIT_LEVEL_ALL;
  conn.write_policy.ttl = default_ttl_seconds_;

  as_policy_remove_init(&conn.remove_policy);
  conn.remove_policy.base.total_timeout = write_timeout_ms_;
  conn.remove_policy.base.socket_timeout = write_timeout_ms_;
  conn.remove_policy.base.max_retries = 0;
  conn.remove_policy.key = AS_POLICY_KEY_DIGEST;

  as_policy_batch_init(&conn.batch_policy);
  conn.batch_policy.base.total_timeout = read_timeout_ms_;
  conn.batch_policy.base.socket_timeout = read_timeout_ms_;
  conn.batch_policy.base.max_retries = 2;
  conn.batch_policy.replica = AS_POLICY_REPLICA_SEQUENCE;
  // Send each node's share of a batch in parallel rather than node by node.
  conn.batch_policy.concurrent = true;
  conn.batch_policy.read_touch_ttl_percent = -1;

  return conn;
}

void AerospikeNativeConnector::do_single_get(WorkerAerospikeConn& conn,
                                             const std::string& key, void* buf,
                                             size_t len,
                                             size_t /*chunk_size*/) {
  std::string meta_key = meta_user_key(key);

  as_key as_meta_key;
  as_key_init_str(&as_meta_key, conn.ns.c_str(), conn.set_name.c_str(),
                  meta_key.c_str());

  as_error err;
  as_record* rec = nullptr;
  as_status status = aerospike_key_get(conn.client, &err, &conn.read_policy,
                                       &as_meta_key, &rec);
  if (status != AEROSPIKE_OK) {
    throw_status("get-meta", status, err);
  }
  if (rec == nullptr) {
    throw std::runtime_error("get-meta returned no record");
  }

  const char* state = as_record_get_str(rec, kBinState);
  if (state == nullptr || std::strcmp(state, kReady) != 0) {
    as_record_destroy(rec);
    throw std::runtime_error("meta record is not ready");
  }

  ShardPlan shard;
  shard.nseg = static_cast<uint32_t>(positive_int_bin(rec, kBinNseg, 1));
  shard.seg_b = static_cast<size_t>(positive_int_bin(rec, kBinSegBytes, len));
  // Absent on records written before plane-aligned sharding existed, and on
  // any payload sharded by byte count. Zero means "records tile the payload
  // uniformly", which is what those records did, so they stay readable.
  shard.plane_b = static_cast<size_t>(
      std::max<int64_t>(as_record_get_int64(rec, kBinPlaneBytes, 0), 0));
  // Present only on objects whose kernel groups differ in plane size. When
  // set it alone describes the records, and must describe exactly `len`
  // bytes in `nseg` records -- a layout that disagrees is corrupt, not a
  // reason to guess.
  const char* runs = as_record_get_str(rec, kBinRuns);
  if (runs != nullptr) {
    try {
      shard.runs = decode_shard_runs(runs);
      shard.seg_b = 0;
      shard.plane_b = 0;
      require_consistent_runs(shard, len);
    } catch (const std::invalid_argument& error) {
      as_record_destroy(rec);
      throw std::runtime_error(std::string("meta record runs: ") +
                               error.what());
    }
  }
  uint32_t nseg = shard.nseg;
  const std::string write_id = write_id_of(rec);
  // Read the stored total directly with a sentinel so a missing or corrupt bin
  // fails the integrity check instead of silently matching `len`.
  int64_t total_raw = as_record_get_int64(rec, kBinTotalBytes, -1);
  if (total_raw < 0 || static_cast<size_t>(total_raw) != len) {
    as_record_destroy(rec);
    throw std::runtime_error("meta record total size mismatch");
  }
  bool ok = true;
  if (nseg == 1) {
    as_bytes* payload = as_record_get_bytes(rec, kBinPayload);
    if (payload == nullptr || payload->size != len) {
      ok = false;
    } else {
      std::memcpy(buf, payload->value, len);
    }
  }
  as_record_destroy(rec);

  if (!ok) {
    throw std::runtime_error("single-record payload size mismatch");
  }
  if (nseg == 1) {
    return;
  }

  size_t covered = 0;
  for (uint32_t i = 0; i < nseg; ++i) {
    std::string segment_key_i = segment_user_key(key, write_id, i);
    ShardRange range = segment_range(shard, i, len);
    if (!read_payload_record(conn, segment_key_i,
                             static_cast<char*>(buf) + range.offset,
                             range.length)) {
      // A miss, never a delete: at replication factor 1 a segment reads as
      // absent while its node is down and is back when the node rejoins.
      // A store of this key replaces the object if it is still damaged.
      throw std::runtime_error("missing segment payload");
    }
    covered += range.length;
  }
  if (covered != len) {
    throw std::runtime_error("segment read size mismatch");
  }
}

void AerospikeNativeConnector::do_single_set(WorkerAerospikeConn& conn,
                                             const std::string& key,
                                             const void* buf, size_t len,
                                             size_t /*chunk_size*/) {
  ShardPlan shard = plan(len);
  as_error err;

  if (shard.nseg == 1) {
    const as_status status =
        create_meta_record(conn, key, shard, len, buf, "", &err);
    // RECORD_EXISTS: another write of this key won; the key is stored.
    if (status != AEROSPIKE_OK && status != AEROSPIKE_ERR_RECORD_EXISTS) {
      throw_status("put-meta", status, err);
    }
    return;
  }

  // Segments go under keys no other write uses, so nothing here can touch
  // the records an existing meta record names. See
  // docs/design/v1/distributed/l2_adapters/aerospike_concurrent_writes.md.
  const std::string write_id = new_write_id();
  try {
    for (uint32_t i = 0; i < shard.nseg; ++i) {
      ShardRange range = segment_range(shard, i, len);
      put_payload_record(conn, segment_user_key(key, write_id, i),
                         static_cast<const char*>(buf) + range.offset,
                         range.length);
    }
  } catch (...) {
    remove_segments(conn, key, write_id, shard.nseg);
    throw;
  }

  const as_status status =
      create_meta_record(conn, key, shard, len, nullptr, write_id, &err);
  if (status == AEROSPIKE_OK) {
    return;
  }
  // Lost the race (RECORD_EXISTS) or failed: no meta record names these
  // segments. An in-doubt put may have landed, so its segments stay; the TTL
  // reclaims them if it did not.
  if (status == AEROSPIKE_ERR_RECORD_EXISTS || !err.in_doubt) {
    remove_segments(conn, key, write_id, shard.nseg);
  }
  if (status != AEROSPIKE_ERR_RECORD_EXISTS) {
    throw_status("put-meta", status, err);
  }
}

bool AerospikeNativeConnector::do_single_exists(WorkerAerospikeConn& conn,
                                                const std::string& key) {
  std::string user_key = meta_user_key(key);

  as_key as_meta_key;
  as_key_init_str(&as_meta_key, conn.ns.c_str(), conn.set_name.c_str(),
                  user_key.c_str());

  as_error err;
  as_record* rec = nullptr;
  as_status status = aerospike_key_exists(
      conn.client, &err, &conn.lookup_policy, &as_meta_key, &rec);
  if (rec != nullptr) {
    as_record_destroy(rec);
  }
  if (status == AEROSPIKE_OK) {
    return true;
  }
  if (status == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
    return false;
  }
  throw_status("exists", status, err);
  return false;
}

void AerospikeNativeConnector::do_batch_exists(WorkerAerospikeConn& conn,
                                               const Request& req) {
  for (size_t begin = 0; begin < req.keys.size();
       begin += kMaxBatchExistsKeys) {
    const size_t end = std::min(begin + kMaxBatchExistsKeys, req.keys.size());
    const uint32_t count = static_cast<uint32_t>(end - begin);

    // as_key_init_str keeps a pointer to the user key, so the strings must
    // outlive the batch call.
    std::vector<std::string> user_keys;
    user_keys.reserve(count);
    for (size_t i = begin; i < end; ++i) {
      user_keys.push_back(meta_user_key(req.keys[i]));
    }

    std::unique_ptr<as_batch_records, void (*)(as_batch_records*)> records(
        as_batch_records_create(count), &as_batch_records_destroy);
    for (const std::string& user_key : user_keys) {
      as_batch_read_record* record = as_batch_read_reserve(records.get());
      as_key_init_str(&record->key, conn.ns.c_str(), conn.set_name.c_str(),
                      user_key.c_str());
      // No bins and read_all_bins false: the server returns the header only.
      record->read_all_bins = false;
    }

    as_error err;
    const as_status status = aerospike_batch_read(
        conn.client, &err, &conn.batch_policy, records.get());
    // BATCH_FAILED means some keys failed; their own results say how.
    if (status != AEROSPIKE_OK && status != AEROSPIKE_BATCH_FAILED) {
      throw_status("batch-exists", status, err);
    }

    for (uint32_t i = 0; i < count; ++i) {
      const as_batch_read_record* record =
          static_cast<const as_batch_read_record*>(
              as_vector_get(&records->list, i));
      const size_t slot = req.start_idx + begin + i;
      if (record->result == AEROSPIKE_OK) {
        req.batch->per_key_results[slot] = 1;
      } else if (record->result == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
        req.batch->per_key_results[slot] = 0;
      } else {
        as_error key_err;
        as_error_init(&key_err);
        throw_status("batch-exists", record->result, key_err);
      }
    }
  }
}

bool AerospikeNativeConnector::do_single_delete(WorkerAerospikeConn& conn,
                                                const std::string& key) {
  std::string user_key = meta_user_key(key);
  uint32_t nseg = 1;
  std::string write_id;

  as_key as_meta_key;
  as_key_init_str(&as_meta_key, conn.ns.c_str(), conn.set_name.c_str(),
                  user_key.c_str());

  as_error err;
  as_record* rec = nullptr;
  as_status status = aerospike_key_get(conn.client, &err, &conn.lookup_policy,
                                       &as_meta_key, &rec);
  if (status == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
    return false;
  }
  if (status != AEROSPIKE_OK) {
    throw_status("delete-read-meta", status, err);
  }
  if (rec != nullptr) {
    nseg = static_cast<uint32_t>(positive_int_bin(rec, kBinNseg, 1));
    write_id = write_id_of(rec);
    as_record_destroy(rec);
  }

  status = aerospike_key_remove(conn.client, &err, &conn.remove_policy,
                                &as_meta_key);
  if (status == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
    return false;
  }
  if (status != AEROSPIKE_OK) {
    throw_status("delete-meta", status, err);
  }

  if (nseg > 1) {
    remove_segments(conn, key, write_id, nseg);
  }
  return true;
}

std::map<std::string, std::string> AerospikeNativeConnector::read_write_ids(
    const std::vector<std::string>& keys) {
  as_policy_batch policy;
  as_policy_batch_init(&policy);
  policy.base.total_timeout = read_timeout_ms_;
  policy.base.socket_timeout = read_timeout_ms_;
  policy.base.max_retries = 2;
  policy.replica = AS_POLICY_REPLICA_SEQUENCE;
  policy.concurrent = true;
  // A lookup, not a load: extending the meta record alone would let it
  // outlive its segments.
  policy.read_touch_ttl_percent = -1;

  std::map<std::string, std::string> found;
  for (size_t begin = 0; begin < keys.size(); begin += kMaxBatchExistsKeys) {
    const size_t end = std::min(begin + kMaxBatchExistsKeys, keys.size());
    const uint32_t count = static_cast<uint32_t>(end - begin);

    // as_key_init_str keeps a pointer to the user key, so the strings must
    // outlive the batch call.
    std::vector<std::string> user_keys;
    user_keys.reserve(count);
    for (size_t i = begin; i < end; ++i) {
      user_keys.push_back(meta_user_key(keys[i]));
    }

    std::unique_ptr<as_batch_records, void (*)(as_batch_records*)> records(
        as_batch_records_create(count), &as_batch_records_destroy);
    for (const std::string& user_key : user_keys) {
      as_batch_read_record* record = as_batch_read_reserve(records.get());
      as_key_init_str(&record->key, ns_.c_str(), set_name_.c_str(),
                      user_key.c_str());
      record->read_all_bins = false;
      record->bin_names = kWriteIdBins;
      record->n_bin_names = 2;
    }

    as_error err;
    const as_status status =
        aerospike_batch_read(&as_, &err, &policy, records.get());
    if (status != AEROSPIKE_OK && status != AEROSPIKE_BATCH_FAILED) {
      throw_status("read-write-ids", status, err);
    }

    for (uint32_t i = 0; i < count; ++i) {
      as_batch_read_record* record =
          static_cast<as_batch_read_record*>(as_vector_get(&records->list, i));
      if (record->result == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
        continue;
      }
      if (record->result != AEROSPIKE_OK) {
        as_error key_err;
        as_error_init(&key_err);
        throw_status("read-write-ids", record->result, key_err);
      }
      const char* state = as_record_get_str(&record->record, kBinState);
      if (state == nullptr || std::strcmp(state, kReady) != 0) {
        continue;
      }
      found[keys[begin + i]] = write_id_of(&record->record);
    }
  }
  return found;
}

void AerospikeNativeConnector::shutdown_connections() {
  // Shared client must outlive worker threads until ConnectorBase drains.
}

void AerospikeNativeConnector::on_workers_stopped() {
  if (connected_) {
    as_error err;
    aerospike_close(&as_, &err);
    aerospike_destroy(&as_);
    connected_ = false;
  }
}

std::vector<std::pair<std::string, int>> AerospikeNativeConnector::parse_hosts(
    const std::string& hosts) {
  std::vector<std::pair<std::string, int>> out;
  for (const std::string& part : split(hosts, ',')) {
    size_t colon = part.rfind(':');
    if (colon == std::string::npos || colon == 0 || colon + 1 >= part.size()) {
      throw std::runtime_error("hosts must be host:port[,host:port...]");
    }
    try {
      out.emplace_back(part.substr(0, colon),
                       std::stoi(part.substr(colon + 1)));
    } catch (...) {
      throw std::runtime_error("invalid port in hosts config: " + part);
    }
  }
  if (out.empty()) {
    throw std::runtime_error("hosts must not be empty");
  }
  return out;
}

std::string AerospikeNativeConnector::meta_user_key(
    const std::string& cache_key) {
  return cache_key + "|m";
}

std::string AerospikeNativeConnector::segment_user_key(
    const std::string& cache_key, const std::string& write_id, uint32_t index) {
  if (write_id.empty()) {
    return cache_key + "|s|" + std::to_string(index);
  }
  return cache_key + "|s|" + write_id + "|" + std::to_string(index);
}

void AerospikeNativeConnector::remove_segments(WorkerAerospikeConn& conn,
                                               const std::string& cache_key,
                                               const std::string& write_id,
                                               uint32_t nseg) {
  for (uint32_t i = 0; i < nseg; ++i) {
    const std::string segment_key_i = segment_user_key(cache_key, write_id, i);
    as_key as_segment_key;
    as_key_init_str(&as_segment_key, conn.ns.c_str(), conn.set_name.c_str(),
                    segment_key_i.c_str());
    as_error seg_err;
    aerospike_key_remove(conn.client, &seg_err, &conn.remove_policy,
                         &as_segment_key);
  }
}

as_status AerospikeNativeConnector::create_meta_record(
    WorkerAerospikeConn& conn, const std::string& cache_key,
    const ShardPlan& shard, size_t total_bytes, const void* inline_buf,
    const std::string& write_id, as_error* err) {
  const std::string user_key = meta_user_key(cache_key);
  as_status status = put_meta_record(conn, user_key, shard, total_bytes,
                                     inline_buf, write_id, err);
  if (status != AEROSPIKE_ERR_RECORD_EXISTS ||
      !remove_damaged_object(conn, cache_key)) {
    return status;
  }
  // One retry: RECORD_EXISTS again means another store took the freed key.
  return put_meta_record(conn, user_key, shard, total_bytes, inline_buf,
                         write_id, err);
}

bool AerospikeNativeConnector::remove_damaged_object(
    WorkerAerospikeConn& conn, const std::string& cache_key) {
  const std::string user_key = meta_user_key(cache_key);
  as_key as_meta_key;
  as_key_init_str(&as_meta_key, conn.ns.c_str(), conn.set_name.c_str(),
                  user_key.c_str());

  static const char* kLayoutBins[] = {kBinNseg, kBinWriteId, nullptr};
  as_error err;
  as_record* rec = nullptr;
  as_status status = aerospike_key_select(
      conn.client, &err, &conn.lookup_policy, &as_meta_key, kLayoutBins, &rec);
  if (status == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
    return true;
  }
  if (status != AEROSPIKE_OK || rec == nullptr) {
    if (rec != nullptr) {
      as_record_destroy(rec);
    }
    return false;
  }
  const uint32_t nseg =
      static_cast<uint32_t>(positive_int_bin(rec, kBinNseg, 1));
  const std::string write_id = write_id_of(rec);
  const uint16_t meta_gen = rec->gen;
  as_record_destroy(rec);
  if (nseg <= 1) {
    return false;
  }

  // Only a segment the cluster reports absent counts as damage; any other
  // error leaves the object alone.
  bool missing = false;
  for (uint32_t i = 0; i < nseg && !missing; ++i) {
    const std::string segment_key_i = segment_user_key(cache_key, write_id, i);
    as_key as_segment_key;
    as_key_init_str(&as_segment_key, conn.ns.c_str(), conn.set_name.c_str(),
                    segment_key_i.c_str());
    as_record* seg = nullptr;
    status = aerospike_key_exists(conn.client, &err, &conn.lookup_policy,
                                  &as_segment_key, &seg);
    if (seg != nullptr) {
      as_record_destroy(seg);
    }
    if (status == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
      missing = true;
    } else if (status != AEROSPIKE_OK) {
      return false;
    }
  }
  if (!missing) {
    return false;
  }

  // Remove only at the generation just read. A delete and a new store in
  // between can recreate the record at the same generation; the caller then
  // replaces that winner with its own complete object, so a reader still
  // sees one write's bytes. Filter expressions would close the window, but
  // the C client's macros do not compile as C++ under g++.
  as_policy_remove policy = conn.remove_policy;
  policy.gen = AS_POLICY_GEN_EQ;
  policy.generation = meta_gen;
  status = aerospike_key_remove(conn.client, &err, &policy, &as_meta_key);
  if (status == AEROSPIKE_OK) {
    remove_segments(conn, cache_key, write_id, nseg);
    return true;
  }
  return status == AEROSPIKE_ERR_RECORD_NOT_FOUND;
}

void AerospikeNativeConnector::throw_status(const char* op, as_status status,
                                            const as_error& err) {
  if (status == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
    throw std::runtime_error(std::string(op) + ": record not found");
  }
  throw std::runtime_error(std::string(op) + ": " +
                           status_message(status, err));
}

void AerospikeNativeConnector::set_plane_bytes(size_t plane_bytes) {
  plane_bytes_.store(plane_bytes, std::memory_order_relaxed);
}

void AerospikeNativeConnector::set_record_layouts(
    const std::vector<std::vector<PlaneRun>>& object_groups) {
  std::lock_guard<std::mutex> lk(record_layouts_mu_);
  // Every TP rank registers the same layouts, so only new ones are kept.
  std::vector<std::vector<PlaneRun>> merged = registered_record_layouts_;
  for (const std::vector<PlaneRun>& runs : object_groups) {
    if (std::find(merged.begin(), merged.end(), runs) == merged.end()) {
      merged.push_back(runs);
    }
  }
  // Validates every group before anything is replaced.
  std::map<size_t, std::vector<PlaneRun>> layouts =
      record_layouts_by_payload(merged);
  registered_record_layouts_ = std::move(merged);
  record_layouts_ = std::move(layouts);
}

size_t AerospikeNativeConnector::max_record_bytes() const {
  return max_record_bytes_;
}

std::string AerospikeNativeConnector::record_digest_hex(
    const std::string& user_key) const {
  if (user_key.empty()) {
    throw std::invalid_argument("record_digest_hex: user key is empty");
  }
  as_key key;
  as_key_init_str(&key, ns_.c_str(), set_name_.c_str(), user_key.c_str());
  const as_digest* digest = as_key_digest(&key);
  if (digest == nullptr || !digest->init) {
    as_key_destroy(&key);
    throw std::runtime_error("record_digest_hex: client computed no digest");
  }
  static constexpr char kHex[] = "0123456789abcdef";
  std::string hex;
  hex.reserve(2 * AS_DIGEST_VALUE_SIZE);
  for (size_t i = 0; i < AS_DIGEST_VALUE_SIZE; ++i) {
    hex.push_back(kHex[digest->value[i] >> 4]);
    hex.push_back(kHex[digest->value[i] & 0x0f]);
  }
  as_key_destroy(&key);
  return hex;
}

std::string AerospikeNativeConnector::record_node(
    const std::string& user_key) const {
  if (user_key.empty()) {
    throw std::invalid_argument("record_node: user key is empty");
  }
  if (as_.cluster == nullptr) {
    throw std::runtime_error("record_node: client is not connected");
  }
  as_key key;
  as_key_init_str(&key, ns_.c_str(), set_name_.c_str(), user_key.c_str());
  as_error err;
  as_error_init(&err);
  as_partition_info partition;
  if (as_partition_info_init(&partition, as_.cluster, &err, &key) !=
      AEROSPIKE_OK) {
    as_key_destroy(&key);
    throw std::runtime_error(std::string("record_node: ") + err.message);
  }
  uint8_t replica_index = 0;
  as_node* node = as_partition_get_node(
      as_.cluster, partition.ns, partition.partition, nullptr,
      AS_POLICY_REPLICA_MASTER, partition.replica_size, &replica_index);
  as_key_destroy(&key);
  if (node == nullptr) {
    throw std::runtime_error("record_node: no node masters the partition of '" +
                             user_key + "'");
  }
  // The map does not reserve the node; hold it while copying the name.
  as_node_reserve(node);
  std::string name(node->name);
  as_node_release(node);
  return name;
}

#ifdef LMCACHE_AEROSPIKE_RDMA

void AerospikeNativeConnector::try_initialize_pipelined_rdma() {
  if (!pipelined_rdma_) {
    return;
  }
  try {
    pipelined_rdma_->initialize(&as_);
  } catch (const std::exception& e) {
    pipelined_init_error_ = e.what();
  } catch (...) {
    pipelined_init_error_ =
        "unknown error during pipelined RDMA initialization";
  }
}

bool AerospikeNativeConnector::pipelined_fetch_ready() const {
  if (!pipelined_rdma_) {
    return false;
  }
  return pipelined_rdma_->is_ready();
}

std::string AerospikeNativeConnector::pipelined_fetch_init_error() const {
  if (!pipelined_init_error_.empty()) {
    return pipelined_init_error_;
  }
  if (!pipelined_rdma_) {
    return {};
  }
  return pipelined_rdma_->init_error_message();
}

std::string AerospikeNativeConnector::pipelined_fetch_node_name() const {
  if (!pipelined_rdma_) {
    throw std::runtime_error("Aerospike pipelined RDMA: not enabled");
  }
  return pipelined_rdma_->node_name();
}

void AerospikeNativeConnector::set_object_group_layouts(
    const std::map<uint32_t, rdma::ObjectGroupLayoutInput>& layouts) {
  if (!pipelined_rdma_) {
    return;
  }
  const std::vector<rdma::ObjectGroupLayout> converted =
      rdma::object_group_layouts_from_inputs(layouts);
  pipelined_rdma_->set_object_group_layouts(converted);
}

uint16_t AerospikeNativeConnector::issue_pipelined_fetch_by_slots(
    const std::vector<std::string>& node_names,
    const std::vector<PlannedSlotKey>& slots) {
  if (!pipelined_rdma_) {
    throw std::runtime_error(
        "Aerospike pipelined fetch: RDMA path is not enabled");
  }
  std::vector<sink::SinkSlot> planned;
  planned.reserve(slots.size());
  for (size_t i = 0; i < slots.size(); ++i) {
    const PlannedSlotKey& slot = slots[i];
    if (slot.node_index >= node_names.size()) {
      throw std::invalid_argument(
          "Aerospike pipelined fetch: slot " + std::to_string(i) +
          " names node index " + std::to_string(slot.node_index) +
          " but only " + std::to_string(node_names.size()) +
          " nodes were given");
    }
    planned.push_back(
        {slot.record_key, slot.layer_id, slot.dest_offset, slot.length});
  }
  return pipelined_rdma_->issue(std::move(planned));
}

uint32_t AerospikeNativeConnector::pipelined_max_slots_per_request() const {
  if (!pipelined_rdma_) {
    return 0;
  }
  return pipelined_rdma_->max_slots_per_request();
}

bool AerospikeNativeConnector::is_pipelined_layer_ready(
    uint32_t layer_id, uint16_t request_generation) const {
  if (!pipelined_rdma_) {
    return false;
  }
  return pipelined_rdma_->is_layer_ready(layer_id, request_generation);
}

std::vector<uint32_t> AerospikeNativeConnector::pipelined_unservable_layers(
    uint16_t generation) const {
  if (!pipelined_rdma_) {
    return {};
  }
  return pipelined_rdma_->unservable_layers(generation);
}

void AerospikeNativeConnector::finish_pipelined_fetch(uint16_t generation) {
  if (!pipelined_rdma_) {
    throw std::runtime_error(
        "Aerospike pipelined fetch: RDMA path is not enabled");
  }
  pipelined_rdma_->finish_request(generation);
}

void AerospikeNativeConnector::abandon_pipelined_fetch(uint16_t generation) {
  if (!pipelined_rdma_) {
    return;
  }
  pipelined_rdma_->abandon_request(generation);
}
#endif

ShardPlan AerospikeNativeConnector::plan(size_t payload_bytes) const {
  std::lock_guard<std::mutex> lk(record_layouts_mu_);
  return choose_shard_plan(payload_bytes, record_layouts_,
                           target_segment_bytes_, max_record_bytes_,
                           single_record_threshold_bytes_,
                           plane_bytes_.load(std::memory_order_relaxed));
}

size_t AerospikeNativeConnector::discover_record_cap() {
  std::string request = "namespace/" + ns_;
  char* response = nullptr;
  as_error err;
  as_status status =
      aerospike_info_any(&as_, &err, nullptr, request.c_str(), &response);
  if (status != AEROSPIKE_OK || response == nullptr) {
    if (response != nullptr) {
      std::free(response);
    }
    throw_status("info namespace", status, err);
  }

  std::string text(response);
  std::free(response);
  for (const char* field : {"max-record-size=", "write-block-size="}) {
    size_t pos = text.find(field);
    if (pos == std::string::npos) {
      continue;
    }
    pos += std::strlen(field);
    size_t end = text.find(';', pos);
    std::string value =
        text.substr(pos, end == std::string::npos ? end : end - pos);
    size_t cap = 0;
    try {
      cap = static_cast<size_t>(std::stoull(value));
    } catch (...) {
      continue;
    }
    if (cap > 0) {
      return cap;
    }
  }
  return kDefaultRecordCapBytes;
}

void AerospikeNativeConnector::configure_policies() {}

void AerospikeNativeConnector::put_payload_record(WorkerAerospikeConn& conn,
                                                  const std::string& user_key,
                                                  const void* buf, size_t len) {
  as_key key;
  as_key_init_str(&key, conn.ns.c_str(), conn.set_name.c_str(),
                  user_key.c_str());

  as_record rec;
  as_record_inita(&rec, 1);
  rec.ttl = AS_RECORD_CLIENT_DEFAULT_TTL;
  as_record_set_raw(&rec, kBinPayload, reinterpret_cast<const uint8_t*>(buf),
                    static_cast<uint32_t>(len));

  as_error err;
  as_status status =
      aerospike_key_put(conn.client, &err, &conn.write_policy, &key, &rec);
  as_record_destroy(&rec);
  if (status != AEROSPIKE_OK) {
    throw_status("put-payload", status, err);
  }
}

as_status AerospikeNativeConnector::put_meta_record(
    WorkerAerospikeConn& conn, const std::string& user_key,
    const ShardPlan& shard, size_t total_bytes, const void* inline_buf,
    const std::string& write_id, as_error* err) {
  as_key key;
  as_key_init_str(&key, conn.ns.c_str(), conn.set_name.c_str(),
                  user_key.c_str());

  // Kept alive until the put: as_record_set_str borrows the pointer.
  const std::string runs =
      shard.runs.empty() ? std::string() : encode_shard_runs(shard.runs);

  as_record rec;
  // Bin count must match the number of as_record_set_* calls below: the bin
  // array is allocated on the stack here, so an undercount overruns it.
  as_record_inita(&rec, 8 + (inline_buf == nullptr ? 0 : 1) +
                            (runs.empty() ? 0 : 1) +
                            (write_id.empty() ? 0 : 1));
  rec.ttl = AS_RECORD_CLIENT_DEFAULT_TTL;
  as_record_set_int64(&rec, kBinVersion, 1);
  as_record_set_str(&rec, kBinState, kReady);
  as_record_set_int64(&rec, kBinNseg, shard.nseg);
  as_record_set_int64(&rec, kBinSegBytes, static_cast<int64_t>(shard.seg_b));
  // Written even when zero, so a reader never has to distinguish "byte-count
  // sharded" from "bin absent" -- both mean uniform tiling.
  as_record_set_int64(&rec, kBinPlaneBytes,
                      static_cast<int64_t>(shard.plane_b));
  // Only for objects whose kernel groups differ in plane size. Uniform
  // objects keep the three-number form, so readers that predate runs can
  // still read them.
  if (!runs.empty()) {
    as_record_set_str(&rec, kBinRuns, runs.c_str());
  }
  as_record_set_int64(&rec, kBinTotalBytes, static_cast<int64_t>(total_bytes));
  as_record_set_int64(&rec, kBinCreatedAt,
                      static_cast<int64_t>(std::time(nullptr)));
  as_record_set_bool(&rec, kBinPin, false);
  if (!write_id.empty()) {
    as_record_set_str(&rec, kBinWriteId, write_id.c_str());
  }
  if (inline_buf != nullptr) {
    // The inline payload path is only taken for single-record writes, where
    // do_single_set() already guaranteed (via plan()) that
    // total_bytes <= max_record_bytes_ -- the discovered server record cap
    // minus a safety margin, which is far below UINT32_MAX. The narrowing cast
    // below is therefore safe; assert the invariant in case that ever changes.
    assert(total_bytes <= max_record_bytes_);
    as_record_set_raw(&rec, kBinPayload,
                      reinterpret_cast<const uint8_t*>(inline_buf),
                      static_cast<uint32_t>(total_bytes));
  }

  // Create-only: the first write of a key wins and is never replaced, so no
  // reader ever races a writer over the records a meta record names.
  as_policy_write policy = conn.write_policy;
  policy.exists = AS_POLICY_EXISTS_CREATE;
  const as_status status =
      aerospike_key_put(conn.client, err, &policy, &key, &rec);
  as_record_destroy(&rec);
  return status;
}

bool AerospikeNativeConnector::read_payload_record(WorkerAerospikeConn& conn,
                                                   const std::string& user_key,
                                                   void* buf, size_t len) {
  as_key key;
  as_key_init_str(&key, conn.ns.c_str(), conn.set_name.c_str(),
                  user_key.c_str());

  as_error err;
  as_record* rec = nullptr;
  as_status status =
      aerospike_key_get(conn.client, &err, &conn.read_policy, &key, &rec);
  if (status == AEROSPIKE_ERR_RECORD_NOT_FOUND) {
    return false;
  }
  if (status != AEROSPIKE_OK) {
    throw_status("get-payload", status, err);
  }
  if (rec == nullptr) {
    return false;
  }
  as_bytes* payload = as_record_get_bytes(rec, kBinPayload);
  bool ok = payload != nullptr && payload->size == len;
  if (ok) {
    std::memcpy(buf, payload->value, payload->size);
  }
  as_record_destroy(rec);
  return ok;
}

}  // namespace connector
}  // namespace lmcache
