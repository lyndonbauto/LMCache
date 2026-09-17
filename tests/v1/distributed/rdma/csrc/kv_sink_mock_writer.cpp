// SPDX-License-Identifier: Apache-2.0
#include "kv_sink_mock_writer.h"

#include <infiniband/verbs.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <random>
#include <sstream>
#include <stdexcept>

#include "kv_sink_client.h"
#include "layer_pipeline.h"

namespace lmcache {
namespace test {
namespace {

constexpr uint8_t kIbPort = 1;
constexpr uint16_t kPkeyIndex = 0;
// Bound the CQ fence so a lost write fails the test instead of hanging it.
constexpr int kPollAttempts = 5'000'000;

[[noreturn]] void throw_verbs(const char* call) {
  const int saved = errno;
  std::ostringstream os;
  os << "mock writer: " << call << " failed: " << std::strerror(saved)
     << " (errno " << saved << ")";
  throw std::runtime_error(os.str());
}

std::string gid_to_hex(const ibv_gid& gid) {
  static const char* kHex = "0123456789abcdef";
  std::string out;
  out.reserve(32);
  for (int i = 0; i < 16; ++i) {
    out.push_back(kHex[gid.raw[i] >> 4]);
    out.push_back(kHex[gid.raw[i] & 0x0f]);
  }
  return out;
}

ibv_gid gid_from_hex(const std::string& hex) {
  if (hex.size() != 32) {
    throw std::runtime_error("mock writer: gid must be 32 hex chars, got '" +
                             hex + "'");
  }
  ibv_gid gid;
  std::memset(&gid, 0, sizeof(gid));
  for (int i = 0; i < 16; ++i) {
    unsigned byte = 0;
    if (std::sscanf(hex.c_str() + 2 * i, "%2x", &byte) != 1) {
      throw std::runtime_error("mock writer: bad gid hex '" + hex + "'");
    }
    gid.raw[i] = static_cast<uint8_t>(byte);
  }
  return gid;
}

uint64_t require_u64(const std::string& text, const std::string& key) {
  const std::string value = connector::rdma::find_info_field(text, key);
  if (value.empty()) {
    throw std::runtime_error("mock writer: command is missing '" + key +
                             "': '" + text + "'");
  }
  return std::stoull(value);
}

}  // namespace

struct KvSinkMockWriter::Impl {
  ibv_context* ctx = nullptr;
  ibv_pd* pd = nullptr;
  ibv_cq* cq = nullptr;
  ibv_qp* qp = nullptr;
  ibv_ah* ah = nullptr;
  // Source buffer the payloads are written from, registered once.
  uint8_t* source = nullptr;
  ibv_mr* source_mr = nullptr;
  ibv_gid local_gid{};
  uint32_t local_psn = 0;

  // Where each record's payload sits inside the source buffer.
  struct Slice {
    size_t offset = 0;
    size_t length = 0;
  };
  std::map<std::string, Slice> records;

  // The client endpoint learned from kv-sink-register.
  uint64_t client_addr = 0;
  size_t client_size = 0;
  uint32_t client_rkey = 0;

  ~Impl() {
    if (ah != nullptr) {
      ibv_destroy_ah(ah);
    }
    if (qp != nullptr) {
      ibv_destroy_qp(qp);
    }
    if (source_mr != nullptr) {
      ibv_dereg_mr(source_mr);
    }
    if (cq != nullptr) {
      ibv_destroy_cq(cq);
    }
    if (pd != nullptr) {
      ibv_dealloc_pd(pd);
    }
    if (ctx != nullptr) {
      ibv_close_device(ctx);
    }
    std::free(source);
  }
};

KvSinkMockWriter::KvSinkMockWriter(const std::string& device_name,
                                   uint8_t gid_index, size_t source_bytes)
    : impl_(new Impl()), gid_index_(gid_index), source_bytes_(source_bytes) {
  int num_devices = 0;
  ibv_device** list = ibv_get_device_list(&num_devices);
  if (list == nullptr || num_devices == 0) {
    if (list != nullptr) {
      ibv_free_device_list(list);
    }
    throw std::runtime_error(
        "mock writer: no RDMA devices found; for local testing run "
        "'sudo modprobe rdma_rxe && sudo rdma link add rxe0 type rxe netdev "
        "lo' and confirm with ibv_devinfo");
  }

  ibv_device* chosen = nullptr;
  for (int i = 0; i < num_devices; ++i) {
    const char* name = ibv_get_device_name(list[i]);
    if (device_name.empty() ||
        (name != nullptr && device_name == std::string(name))) {
      chosen = list[i];
      break;
    }
  }
  if (chosen == nullptr) {
    ibv_free_device_list(list);
    throw std::runtime_error("mock writer: device '" + device_name +
                             "' not found");
  }

  impl_->ctx = ibv_open_device(chosen);
  ibv_free_device_list(list);
  if (impl_->ctx == nullptr) {
    throw_verbs("ibv_open_device");
  }

  impl_->pd = ibv_alloc_pd(impl_->ctx);
  if (impl_->pd == nullptr) {
    throw_verbs("ibv_alloc_pd");
  }
  if (ibv_query_gid(impl_->ctx, kIbPort, gid_index_, &impl_->local_gid) != 0) {
    throw_verbs("ibv_query_gid");
  }

  // Page-align the source buffer the way the engine's KV pages are aligned.
  impl_->source = static_cast<uint8_t*>(std::aligned_alloc(4096, source_bytes));
  if (impl_->source == nullptr) {
    throw std::runtime_error("mock writer: failed to allocate source buffer");
  }
  std::memset(impl_->source, 0, source_bytes);

  impl_->source_mr = ibv_reg_mr(impl_->pd, impl_->source, source_bytes,
                                IBV_ACCESS_LOCAL_WRITE);
  if (impl_->source_mr == nullptr) {
    throw_verbs("ibv_reg_mr(source)");
  }

  impl_->cq = ibv_create_cq(impl_->ctx, 256, nullptr, nullptr, 0);
  if (impl_->cq == nullptr) {
    throw_verbs("ibv_create_cq");
  }

  ibv_qp_init_attr init{};
  init.send_cq = impl_->cq;
  init.recv_cq = impl_->cq;
  init.qp_type = IBV_QPT_RC;
  init.cap.max_send_wr = 256;
  init.cap.max_recv_wr = 1;
  init.cap.max_send_sge = 1;
  init.cap.max_recv_sge = 1;
  impl_->qp = ibv_create_qp(impl_->pd, &init);
  if (impl_->qp == nullptr) {
    throw_verbs("ibv_create_qp");
  }

  std::random_device rd;
  impl_->local_psn = rd() & 0xffffff;
}

KvSinkMockWriter::~KvSinkMockWriter() = default;

void KvSinkMockWriter::add_record(const MockRecord& record) {
  if (source_used_ + record.payload.size() > source_bytes_) {
    throw std::runtime_error("mock writer: source buffer is full");
  }
  std::memcpy(impl_->source + source_used_, record.payload.data(),
              record.payload.size());
  impl_->records[record.digest_hex] =
      Impl::Slice{source_used_, record.payload.size()};
  source_used_ += record.payload.size();
}

std::string KvSinkMockWriter::handle_register(const std::string& command) {
  if (command.rfind("kv-sink-register", 0) != 0) {
    throw std::runtime_error("mock writer: not a kv-sink-register command: '" +
                             command + "'");
  }

  const std::string client_gid_hex =
      connector::rdma::find_info_field(command, "gid");
  if (client_gid_hex.empty()) {
    throw std::runtime_error("mock writer: register command is missing 'gid'");
  }
  const auto client_qpn = static_cast<uint32_t>(require_u64(command, "qpn"));
  const auto client_psn = static_cast<uint32_t>(require_u64(command, "psn"));
  impl_->client_rkey = static_cast<uint32_t>(require_u64(command, "rkey"));
  impl_->client_addr = require_u64(command, "addr");
  impl_->client_size = static_cast<size_t>(require_u64(command, "size"));

  const ibv_gid client_gid = gid_from_hex(client_gid_hex);

  // INIT.
  ibv_qp_attr attr{};
  attr.qp_state = IBV_QPS_INIT;
  attr.pkey_index = kPkeyIndex;
  attr.port_num = kIbPort;
  attr.qp_access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE;
  if (ibv_modify_qp(impl_->qp, &attr,
                    IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT |
                        IBV_QP_ACCESS_FLAGS) != 0) {
    throw_verbs("ibv_modify_qp(INIT)");
  }

  // RTR, aimed at the client that just registered.
  std::memset(&attr, 0, sizeof(attr));
  attr.qp_state = IBV_QPS_RTR;
  attr.path_mtu = IBV_MTU_1024;
  attr.dest_qp_num = client_qpn;
  attr.rq_psn = client_psn;
  attr.max_dest_rd_atomic = 1;
  attr.min_rnr_timer = 12;
  attr.ah_attr.is_global = 1;
  attr.ah_attr.port_num = kIbPort;
  attr.ah_attr.sl = 0;
  attr.ah_attr.src_path_bits = 0;
  attr.ah_attr.grh.dgid = client_gid;
  attr.ah_attr.grh.sgid_index = gid_index_;
  attr.ah_attr.grh.hop_limit = 1;
  if (ibv_modify_qp(impl_->qp, &attr,
                    IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU |
                        IBV_QP_DEST_QPN | IBV_QP_RQ_PSN |
                        IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER) !=
      0) {
    throw_verbs("ibv_modify_qp(RTR)");
  }

  // RTS.
  std::memset(&attr, 0, sizeof(attr));
  attr.qp_state = IBV_QPS_RTS;
  attr.sq_psn = impl_->local_psn;
  attr.timeout = 14;
  attr.retry_cnt = 7;
  attr.rnr_retry = 7;
  attr.max_rd_atomic = 1;
  if (ibv_modify_qp(impl_->qp, &attr,
                    IBV_QP_STATE | IBV_QP_SQ_PSN | IBV_QP_TIMEOUT |
                        IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY |
                        IBV_QP_MAX_QP_RD_ATOMIC) != 0) {
    throw_verbs("ibv_modify_qp(RTS)");
  }

  // The sender needs an AH for the peer too; this is the other half of the
  // bidirectional peer relationship that makes the client's ibv_create_ah
  // non-optional.
  ibv_ah_attr ah_attr{};
  ah_attr.is_global = 1;
  ah_attr.port_num = kIbPort;
  ah_attr.grh.dgid = client_gid;
  ah_attr.grh.sgid_index = gid_index_;
  ah_attr.grh.hop_limit = 1;
  impl_->ah = ibv_create_ah(impl_->pd, &ah_attr);
  if (impl_->ah == nullptr) {
    throw_verbs("ibv_create_ah");
  }

  connected_ = true;
  region_ = 4;  // Matches the PoC's example reply.

  std::ostringstream os;
  os << "region=" << region_ << ";transport=verbs;qp=rc"
     << ";qpn=" << impl_->qp->qp_num << ";psn=" << impl_->local_psn
     << ";gid=" << gid_to_hex(impl_->local_gid);
  return os.str();
}

std::string KvSinkMockWriter::handle_fetch(const std::string& command) {
  if (!connected_) {
    throw std::runtime_error(
        "mock writer: kv-sink-fetch before kv-sink-register");
  }
  // The colon matters: without it this would also accept
  // "kv-sink-fetch-pipelined" and silently serve it all-or-nothing, so the
  // client's per-slot signals would never arrive.
  if (command.rfind("kv-sink-fetch:", 0) != 0) {
    throw std::runtime_error("mock writer: not a kv-sink-fetch command: '" +
                             command + "'");
  }

  const std::string sinks = connector::rdma::find_info_field(command, "sinks");
  if (sinks.empty()) {
    throw std::runtime_error("mock writer: fetch command has no sinks");
  }

  std::vector<std::string> results;
  uint32_t requested = 0;
  uint32_t posted = 0;
  uint64_t bytes = 0;

  size_t cursor = 0;
  while (cursor <= sinks.size()) {
    size_t comma = sinks.find(',', cursor);
    const std::string entry =
        sinks.substr(cursor, comma == std::string::npos ? std::string::npos
                                                        : comma - cursor);
    cursor = comma == std::string::npos ? sinks.size() + 1 : comma + 1;
    if (entry.empty()) {
      continue;
    }
    ++requested;

    // <digest>@<offset>:<length>
    const size_t at = entry.find('@');
    const size_t colon = entry.find(':', at == std::string::npos ? 0 : at);
    if (at == std::string::npos || colon == std::string::npos) {
      results.push_back("err");
      continue;
    }
    const std::string digest = entry.substr(0, at);
    const size_t offset = std::stoull(entry.substr(at + 1, colon - at - 1));
    const size_t length = std::stoull(entry.substr(colon + 1));

    const auto found = impl_->records.find(digest);
    if (found == impl_->records.end() || found->second.length != length) {
      results.push_back("err");
      continue;
    }
    // Refuse to write outside the window the client registered. The bounded
    // window is what makes this check possible at all.
    if (offset + length > impl_->client_size) {
      results.push_back("err");
      continue;
    }

    ibv_sge sge{};
    sge.addr = reinterpret_cast<uint64_t>(impl_->source + found->second.offset);
    sge.length = static_cast<uint32_t>(length);
    sge.lkey = impl_->source_mr->lkey;

    ibv_send_wr wr{};
    wr.wr_id = posted;
    wr.sg_list = &sge;
    wr.num_sge = 1;
    wr.opcode = IBV_WR_RDMA_WRITE;
    // Signal every write so the fence below sees one completion each.
    wr.send_flags = IBV_SEND_SIGNALED;
    wr.wr.rdma.remote_addr = impl_->client_addr + offset;
    wr.wr.rdma.rkey = impl_->client_rkey;

    ibv_send_wr* bad = nullptr;
    if (ibv_post_send(impl_->qp, &wr, &bad) != 0) {
      throw_verbs("ibv_post_send");
    }
    ++posted;
    bytes += length;
    results.push_back("ok");
  }

  // Fence on our own send CQ. This is the whole reason the info reply can be
  // treated as the completion: we do not answer until every byte has landed.
  uint32_t completed = 0;
  int attempts = 0;
  while (completed < posted) {
    ibv_wc wc{};
    const int got = ibv_poll_cq(impl_->cq, 1, &wc);
    if (got < 0) {
      throw std::runtime_error("mock writer: ibv_poll_cq failed");
    }
    if (got == 0) {
      if (++attempts > kPollAttempts) {
        throw std::runtime_error("mock writer: timed out waiting for " +
                                 std::to_string(posted - completed) +
                                 " RDMA write completion(s)");
      }
      continue;
    }
    if (wc.status != IBV_WC_SUCCESS) {
      throw std::runtime_error(
          std::string("mock writer: RDMA write failed: ") +
          ibv_wc_status_str(wc.status) +
          " -- IBV_WC_REM_ACCESS_ERR usually means a bad rkey or an offset "
          "outside the registered window; UNKNOWN_PEER-style failures mean "
          "the peer never created an address handle for us");
    }
    ++completed;
  }

  std::ostringstream os;
  os << "n=" << requested << ";ok=" << posted << ";bytes=" << bytes
     << ";in-place=" << posted << ";results=";
  for (size_t i = 0; i < results.size(); ++i) {
    if (i > 0) {
      os << ',';
    }
    os << results[i];
  }
  return os.str();
}

std::string KvSinkMockWriter::handle_pipelined_fetch(
    const std::string& command) {
  if (!connected_) {
    throw std::runtime_error(
        "mock writer: kv-sink-fetch-pipelined before kv-sink-register");
  }
  if (command.rfind("kv-sink-fetch-pipelined:", 0) != 0) {
    throw std::runtime_error(
        "mock writer: not a kv-sink-fetch-pipelined command: '" + command +
        "'");
  }

  const std::string gen_field =
      connector::rdma::find_info_field(command, "gen");
  if (gen_field.empty()) {
    throw std::runtime_error(
        "mock writer: pipelined fetch command has no gen; without it the "
        "client cannot tell our writes from a previous request's");
  }
  const uint16_t generation = static_cast<uint16_t>(std::stoul(gen_field));

  const std::string sinks = connector::rdma::find_info_field(command, "sinks");
  if (sinks.empty()) {
    throw std::runtime_error(
        "mock writer: pipelined fetch command has no sinks");
  }

  uint32_t requested = 0;
  uint32_t accepted = 0;
  uint64_t bytes = 0;
  std::vector<uint16_t> failed;

  size_t cursor = 0;
  while (cursor <= sinks.size()) {
    const size_t comma = sinks.find(',', cursor);
    const std::string entry =
        sinks.substr(cursor, comma == std::string::npos ? std::string::npos
                                                        : comma - cursor);
    cursor = comma == std::string::npos ? sinks.size() + 1 : comma + 1;
    if (entry.empty()) {
      continue;
    }
    ++requested;

    // <digest>@<offset>:<length>#<slot>
    const size_t at = entry.find('@');
    const size_t colon = entry.find(':', at == std::string::npos ? 0 : at);
    const size_t hash = entry.find('#', colon == std::string::npos ? 0 : colon);
    if (at == std::string::npos || colon == std::string::npos ||
        hash == std::string::npos) {
      throw std::runtime_error("mock writer: malformed pipelined sink '" +
                               entry + "'");
    }
    const std::string digest = entry.substr(0, at);
    const size_t offset = std::stoull(entry.substr(at + 1, colon - at - 1));
    const size_t length =
        std::stoull(entry.substr(colon + 1, hash - colon - 1));
    const uint16_t slot =
        static_cast<uint16_t>(std::stoul(entry.substr(hash + 1)));

    // Validated here rather than by catching push_slot's exception, so that a
    // data problem becomes a named failed slot while a fabric problem still
    // propagates.
    const auto found = impl_->records.find(digest);
    if (found == impl_->records.end() || found->second.length != length ||
        offset + length > impl_->client_size) {
      failed.push_back(slot);
      continue;
    }

    push_slot(digest, offset, length,
              connector::rdma::encode_immediate(generation, slot));
    ++accepted;
    bytes += length;
  }

  // No fence. Writes are still in flight as this reply is built, and that is
  // the point.
  std::ostringstream os;
  os << "n=" << requested << ";accepted=" << accepted << ";failed=";
  for (size_t i = 0; i < failed.size(); ++i) {
    if (i > 0) {
      os << ',';
    }
    os << failed[i];
  }
  os << ";bytes=" << bytes;
  return os.str();
}

void KvSinkMockWriter::push_slot(const std::string& digest_hex, size_t offset,
                                 size_t length, uint32_t immediate) {
  if (!connected_) {
    throw std::runtime_error(
        "push_slot before handle_register: there is no client to write to");
  }

  const auto found = impl_->records.find(digest_hex);
  if (found == impl_->records.end()) {
    throw std::runtime_error("push_slot: unknown digest " + digest_hex);
  }
  if (length > found->second.length) {
    throw std::runtime_error("push_slot: requested " + std::to_string(length) +
                             " bytes but record holds " +
                             std::to_string(found->second.length));
  }
  // Same bounded-window check as the fenced path. A pipelined write is no
  // less capable of landing in someone else's KV cache.
  if (offset + length > impl_->client_size) {
    throw std::runtime_error("push_slot: offset " + std::to_string(offset) +
                             " plus length " + std::to_string(length) +
                             " overruns the client window of " +
                             std::to_string(impl_->client_size) + " bytes");
  }

  ibv_sge sge{};
  sge.addr = reinterpret_cast<uint64_t>(impl_->source + found->second.offset);
  sge.length = static_cast<uint32_t>(length);
  sge.lkey = impl_->source_mr->lkey;

  ibv_send_wr wr{};
  wr.wr_id = immediate;
  wr.sg_list = &sge;
  wr.num_sge = 1;
  // The one line that makes pipelining possible: unlike IBV_WR_RDMA_WRITE,
  // this raises a receive completion on the client carrying imm_data.
  wr.opcode = IBV_WR_RDMA_WRITE_WITH_IMM;
  wr.send_flags = IBV_SEND_SIGNALED;
  wr.imm_data = htobe32(immediate);
  wr.wr.rdma.remote_addr = impl_->client_addr + offset;
  wr.wr.rdma.rkey = impl_->client_rkey;

  ibv_send_wr* bad = nullptr;
  if (ibv_post_send(impl_->qp, &wr, &bad) != 0) {
    throw_verbs("ibv_post_send(write_with_imm)");
  }

  // Reap our own send completions so the send queue does not fill up over a
  // long pipelined fetch. This is bookkeeping, not a fence: we do not block
  // until the write has landed, which is the entire difference from
  // handle_fetch().
  ibv_wc wc{};
  while (ibv_poll_cq(impl_->cq, 1, &wc) > 0) {
    if (wc.status != IBV_WC_SUCCESS) {
      throw std::runtime_error(std::string("push_slot: RDMA write failed: ") +
                               ibv_wc_status_str(wc.status));
    }
  }
}

}  // namespace test
}  // namespace lmcache
