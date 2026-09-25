// SPDX-License-Identifier: Apache-2.0
//
// Does an RDMA_WRITE_WITH_IMM consume a posted receive on this transport?
//
// Acceptance item A7 (docs/design/v1/layerwise/track-a-acceptance.md). The
// pipelined fetch sizes its receive queue from the plan because on RC every
// immediate consumes one posted receive, and a missing receive stalls the
// writer (receiver-not-ready, RNR). Whether EFA's SRD behaves the same, drops
// the immediate, or fails the writer cannot be read from headers, so this
// probe measures it.
//
// One process, one device: a sender queue pair writes into a receiver queue
// pair over loopback. Each scenario builds fresh queue pairs, posts `posted`
// receives, sends `writes` write-with-immediates carrying their slot index,
// then records for `--wait-ms`:
//
//   * receive completions, and whether an immediate was out of range or
//     repeated;
//   * sender completions and the first error status;
//   * how many slots' bytes landed.
//
// When writes > posted it then posts the missing receives and records again,
// which tells "stalled until a receive appeared" from "lost".
//
// Scenarios:
//   fits              posted == writes                  sanity check
//   starved           posted <  writes, infinite retry  the A7 question
//   starved_no_retry  posted == 0, rnr_retry 0          what the writer sees
//   unsolicited       posted == 0, EFA QP flag set      only with --unsolicited
//
// Output is one RESULT line per scenario and a VERDICT line; paste them into
// aerospike_rdma.md. Expected on RC: consumes_recv_wr=yes, with the starved
// writes delivered only after the repost.
//
// Usage:
//   efa_imm_probe [device [gid_index]] [--transport rc|srd] [--wait-ms N]
//                 [--unsolicited] [--expect-consumes]
//
// --expect-consumes exits 1 unless the verdict is "yes"; `make test` uses it
// for the RC baseline. Exits 77 (skip) when no RDMA device is present, and 2
// when the device cannot run the probe (e.g. EFA without RDMA write).

#include <arpa/inet.h>
#include <infiniband/verbs.h>

#ifdef LMCACHE_AEROSPIKE_EFA
  #include <infiniband/efadv.h>
#endif

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int kSkipExitCode = 77;
constexpr int kCannotRunExitCode = 2;
constexpr uint8_t kPort = 1;
constexpr uint16_t kPkeyIndex = 0;
constexpr uint32_t kSrdQkey = 0x11111111;
constexpr size_t kSlotBytes = 4096;
constexpr size_t kRecvScratchBytes = 64;
constexpr uint8_t kInfiniteRnrRetry = 7;
constexpr uint8_t kMinRnrTimer = 12;  // 0.64 ms, as in RdmaContext

// efadv device_caps bits. Spelled out because older efadv.h lacks the newer
// names, and the probe must report them on any build.
constexpr uint32_t kEfaCapRnrRetry = 1u << 1;
constexpr uint32_t kEfaCapRdmaWrite = 1u << 3;
constexpr uint32_t kEfaCapUnsolicitedWriteRecv = 1u << 4;

enum class Transport { kRc, kSrd };

struct Options {
  std::string device;
  uint8_t gid_index = 0;
  Transport transport = Transport::kRc;
  int wait_ms = 1000;
  bool unsolicited = false;
  bool expect_consumes = false;
};

struct Scenario {
  const char* name;
  uint32_t posted;
  uint32_t writes;
  uint8_t rnr_retry;
  bool unsolicited;
};

struct Outcome {
  uint32_t recv_before_repost = 0;
  uint32_t recv_after_repost = 0;
  uint32_t recv_unsolicited = 0;
  uint32_t send_ok = 0;
  uint32_t send_error = 0;
  std::string first_send_error = "-";
  std::string first_recv_error = "-";
  uint32_t landed_before_repost = 0;
  uint32_t landed_final = 0;
  bool bad_immediate = false;
};

[[noreturn]] void fail_verbs(const char* call) {
  const int saved = errno;
  std::ostringstream os;
  os << call << " failed: " << std::strerror(saved) << " (errno " << saved
     << ")";
  throw std::runtime_error(os.str());
}

uint8_t pattern_byte(uint32_t slot, size_t offset) {
  return static_cast<uint8_t>(0x80 | ((slot * 13 + offset) & 0x7f));
}

bool has_rdma_device() {
  int count = 0;
  ibv_device** list = ibv_get_device_list(&count);
  if (list != nullptr) {
    ibv_free_device_list(list);
  }
  return count > 0;
}

const char* transport_name(Transport transport) {
  return transport == Transport::kSrd ? "srd" : "rc";
}

// EFA device_caps of `ctx`, or 0 on a build or device without EFA.
uint32_t efa_device_caps(ibv_context* ctx) {
#ifdef LMCACHE_AEROSPIKE_EFA
  efadv_device_attr attr{};
  if (efadv_query_device(ctx, &attr, sizeof(attr)) != 0) {
    return 0;
  }
  return attr.device_caps;
#else
  (void)ctx;
  return 0;
#endif
}

// Print the EFA capabilities that matter here. Returns false, after saying
// why, when the device cannot run the probe over SRD.
bool report_srd_device(const Options& options) {
#ifndef LMCACHE_AEROSPIKE_EFA
  (void)options;
  std::cout << "SRD needs an EFA build: make efa-probe EFA=1 (needs libefa)\n";
  return false;
#else
  int count = 0;
  ibv_device** list = ibv_get_device_list(&count);
  ibv_context* ctx = nullptr;
  for (int i = 0; list != nullptr && i < count && ctx == nullptr; ++i) {
    const char* name = ibv_get_device_name(list[i]);
    if (options.device.empty() || (name != nullptr && options.device == name)) {
      ctx = ibv_open_device(list[i]);
    }
  }
  if (list != nullptr) ibv_free_device_list(list);
  if (ctx == nullptr) {
    std::cout << "cannot open RDMA device '" << options.device << "'\n";
    return false;
  }
  efadv_device_attr attr{};
  const bool is_efa = efadv_query_device(ctx, &attr, sizeof(attr)) == 0;
  ibv_close_device(ctx);
  if (!is_efa) {
    std::cout << "efadv_query_device failed: not an EFA device\n";
    return false;
  }
  const uint32_t caps = attr.device_caps;
  std::cout << "EFA device_caps=0x" << std::hex << caps << std::dec
            << " rdma_write=" << ((caps & kEfaCapRdmaWrite) ? 1 : 0)
            << " rnr_retry=" << ((caps & kEfaCapRnrRetry) ? 1 : 0)
            << " unsolicited_write_recv="
            << ((caps & kEfaCapUnsolicitedWriteRecv) ? 1 : 0)
            << " max_rq_wr=" << attr.max_rq_wr << "\n";
  if ((caps & kEfaCapRdmaWrite) == 0) {
    std::cout << "This EFA device has no RDMA write; use an instance type "
                 "that has it\n";
    return false;
  }
  return true;
#endif
}

// One sender and one receiver queue pair on one device, wired to each other.
class Loopback {
 public:
  Loopback(const Options& options, const Scenario& scenario)
      : options_(options), scenario_(scenario) {
    open_device();
    allocate_buffers();
    create_completion_queues();
    create_queue_pairs();
    connect();
  }

  ~Loopback() {
    if (ah_ != nullptr) ibv_destroy_ah(ah_);
    if (sender_ != nullptr) ibv_destroy_qp(sender_);
    if (receiver_ != nullptr) ibv_destroy_qp(receiver_);
    if (send_cq_ != nullptr) ibv_destroy_cq(send_cq_);
    if (recv_cq_ != nullptr) ibv_destroy_cq(recv_cq_);
    if (send_mr_ != nullptr) ibv_dereg_mr(send_mr_);
    if (recv_mr_ != nullptr) ibv_dereg_mr(recv_mr_);
    if (pd_ != nullptr) ibv_dealloc_pd(pd_);
    if (ctx_ != nullptr) ibv_close_device(ctx_);
  }

  Loopback(const Loopback&) = delete;
  Loopback& operator=(const Loopback&) = delete;

  void post_receives(uint32_t count) {
    for (uint32_t i = 0; i < count; ++i) {
      const uint32_t index = next_receive_++;
      ibv_sge sge{};
      sge.addr = reinterpret_cast<uintptr_t>(scratch(index));
      sge.length = kRecvScratchBytes;
      sge.lkey = recv_mr_->lkey;
      ibv_recv_wr wr{};
      wr.wr_id = index;
      wr.sg_list = &sge;
      wr.num_sge = 1;
      ibv_recv_wr* bad = nullptr;
      if (ibv_post_recv(receiver_, &wr, &bad) != 0) {
        fail_verbs("ibv_post_recv");
      }
    }
  }

  void post_writes() {
    if (options_.transport == Transport::kSrd) {
      post_writes_srd();
    } else {
      post_writes_rc();
    }
  }

  // Poll both queues until `recv_target` receives and every send completed,
  // or `wait_ms` passed. Adds what it saw to `out`.
  void poll(uint32_t recv_target, uint32_t& recv_count, Outcome& out) {
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(options_.wait_ms);
    while (std::chrono::steady_clock::now() < deadline) {
      drain_send(out);
      drain_receive(recv_count, out);
      if (recv_count >= recv_target &&
          out.send_ok + out.send_error >= scenario_.writes) {
        return;
      }
      std::this_thread::sleep_for(std::chrono::microseconds(200));
    }
  }

  uint32_t landed() const {
    uint32_t count = 0;
    for (uint32_t slot = 0; slot < scenario_.writes; ++slot) {
      const uint8_t* dest = recv_buffer_.data() + slot * kSlotBytes;
      bool intact = true;
      for (size_t i = 0; i < kSlotBytes && intact; ++i) {
        intact = dest[i] == pattern_byte(slot, i);
      }
      count += intact ? 1 : 0;
    }
    return count;
  }

 private:
  uint8_t* scratch(uint32_t index) {
    return recv_buffer_.data() + scenario_.writes * kSlotBytes +
           (index % scenario_.writes) * kRecvScratchBytes;
  }

  void open_device() {
    int count = 0;
    ibv_device** list = ibv_get_device_list(&count);
    if (list == nullptr || count == 0) {
      throw std::runtime_error("no RDMA device");
    }
    ibv_device* chosen = nullptr;
    for (int i = 0; i < count && chosen == nullptr; ++i) {
      const char* name = ibv_get_device_name(list[i]);
      if (options_.device.empty() ||
          (name != nullptr && options_.device == name)) {
        chosen = list[i];
      }
    }
    if (chosen == nullptr) {
      ibv_free_device_list(list);
      throw std::runtime_error("no RDMA device named " + options_.device);
    }
    ctx_ = ibv_open_device(chosen);
    ibv_free_device_list(list);
    if (ctx_ == nullptr) fail_verbs("ibv_open_device");
    pd_ = ibv_alloc_pd(ctx_);
    if (pd_ == nullptr) fail_verbs("ibv_alloc_pd");
    if (ibv_query_gid(ctx_, kPort, options_.gid_index, &gid_) != 0) {
      fail_verbs("ibv_query_gid");
    }
    if (options_.transport == Transport::kSrd) {
      srd_rnr_retry_supported_ = (efa_device_caps(ctx_) & kEfaCapRnrRetry) != 0;
    }
  }

  void allocate_buffers() {
    send_buffer_.resize(scenario_.writes * kSlotBytes);
    for (uint32_t slot = 0; slot < scenario_.writes; ++slot) {
      for (size_t i = 0; i < kSlotBytes; ++i) {
        send_buffer_[slot * kSlotBytes + i] = pattern_byte(slot, i);
      }
    }
    recv_buffer_.assign(scenario_.writes * (kSlotBytes + kRecvScratchBytes), 0);
    send_mr_ = ibv_reg_mr(pd_, send_buffer_.data(), send_buffer_.size(),
                          IBV_ACCESS_LOCAL_WRITE);
    if (send_mr_ == nullptr) fail_verbs("ibv_reg_mr(send)");
    recv_mr_ = ibv_reg_mr(pd_, recv_buffer_.data(), recv_buffer_.size(),
                          IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
    if (recv_mr_ == nullptr) fail_verbs("ibv_reg_mr(recv)");
  }

  void create_completion_queues() {
    const int depth = static_cast<int>(scenario_.writes) + 1;
    send_cq_ = ibv_create_cq(ctx_, depth, nullptr, nullptr, 0);
    if (send_cq_ == nullptr) fail_verbs("ibv_create_cq(send)");
    if (scenario_.unsolicited) {
      create_unsolicited_receive_cq(depth);
      return;
    }
    recv_cq_ = ibv_create_cq(ctx_, depth, nullptr, nullptr, 0);
    if (recv_cq_ == nullptr) fail_verbs("ibv_create_cq(recv)");
  }

  void create_unsolicited_receive_cq(int depth) {
#ifdef LMCACHE_EFA_UNSOLICITED
    ibv_cq_init_attr_ex attr{};
    attr.cqe = static_cast<uint32_t>(depth);
    attr.wc_flags = IBV_WC_STANDARD_FLAGS;
    efadv_cq_init_attr efa{};
    efa.wc_flags = EFADV_WC_EX_WITH_IS_UNSOLICITED;
    recv_cq_ex_ = efadv_create_cq(ctx_, &attr, &efa, sizeof(efa));
    if (recv_cq_ex_ == nullptr) fail_verbs("efadv_create_cq");
    recv_efa_cq_ = efadv_cq_from_ibv_cq_ex(recv_cq_ex_);
    recv_cq_ = ibv_cq_ex_to_cq(recv_cq_ex_);
#else
    (void)depth;
    throw std::runtime_error(
        "built without EFA unsolicited-write-receive support");
#endif
  }

  void create_queue_pairs() {
    const uint32_t depth = scenario_.writes;
    if (options_.transport == Transport::kSrd) {
      sender_ = create_srd_qp(depth, 1, /*with_write_imm=*/true, false);
      receiver_ = create_srd_qp(1, depth, /*with_write_imm=*/false,
                                scenario_.unsolicited);
      return;
    }
    ibv_qp_init_attr init{};
    init.qp_type = IBV_QPT_RC;
    init.cap.max_send_sge = 1;
    init.cap.max_recv_sge = 1;
    init.send_cq = send_cq_;
    init.recv_cq = send_cq_;
    init.cap.max_send_wr = depth;
    init.cap.max_recv_wr = 1;
    sender_ = ibv_create_qp(pd_, &init);
    if (sender_ == nullptr) fail_verbs("ibv_create_qp(sender)");
    init.send_cq = recv_cq_;
    init.recv_cq = recv_cq_;
    init.cap.max_send_wr = 1;
    init.cap.max_recv_wr = depth;
    receiver_ = ibv_create_qp(pd_, &init);
    if (receiver_ == nullptr) fail_verbs("ibv_create_qp(receiver)");
  }

  ibv_qp* create_srd_qp(uint32_t max_send, uint32_t max_recv,
                        bool with_write_imm, bool unsolicited) {
#ifdef LMCACHE_AEROSPIKE_EFA
    ibv_qp_init_attr_ex ex{};
    ex.send_cq = with_write_imm ? send_cq_ : recv_cq_;
    ex.recv_cq = with_write_imm ? send_cq_ : recv_cq_;
    ex.cap.max_send_wr = max_send;
    ex.cap.max_recv_wr = max_recv;
    ex.cap.max_send_sge = 1;
    ex.cap.max_recv_sge = 1;
    ex.qp_type = IBV_QPT_DRIVER;
    ex.pd = pd_;
    ex.comp_mask = IBV_QP_INIT_ATTR_PD;
    if (with_write_imm) {
      ex.comp_mask |= IBV_QP_INIT_ATTR_SEND_OPS_FLAGS;
      ex.send_ops_flags = IBV_QP_EX_WITH_RDMA_WRITE_WITH_IMM;
    }
    efadv_qp_init_attr efa{};
    efa.driver_qp_type = EFADV_QP_DRIVER_TYPE_SRD;
  #ifdef LMCACHE_EFA_UNSOLICITED
    if (unsolicited) {
      efa.flags |= EFADV_QP_FLAGS_UNSOLICITED_WRITE_RECV;
    }
  #else
    (void)unsolicited;
  #endif
    ibv_qp* qp = efadv_create_qp_ex(ctx_, &ex, &efa, sizeof(efa));
    if (qp == nullptr) fail_verbs("efadv_create_qp_ex");
    return qp;
#else
    (void)max_send;
    (void)max_recv;
    (void)with_write_imm;
    (void)unsolicited;
    throw std::runtime_error(
        "SRD needs an EFA build: make efa-probe EFA=1 (needs libefa)");
#endif
  }

  void connect() {
    if (options_.transport == Transport::kSrd) {
      connect_srd(sender_);
      connect_srd(receiver_);
      ibv_ah_attr ah{};
      ah.is_global = 1;
      ah.port_num = kPort;
      ah.grh.dgid = gid_;
      ah.grh.sgid_index = options_.gid_index;
      ah.grh.hop_limit = 1;
      ah_ = ibv_create_ah(pd_, &ah);
      if (ah_ == nullptr) fail_verbs("ibv_create_ah");
      return;
    }
    connect_rc(sender_, receiver_->qp_num);
    connect_rc(receiver_, sender_->qp_num);
  }

  void connect_srd(ibv_qp* qp) {
    ibv_qp_attr attr{};
    attr.qp_state = IBV_QPS_INIT;
    attr.pkey_index = kPkeyIndex;
    attr.port_num = kPort;
    attr.qkey = kSrdQkey;
    if (ibv_modify_qp(qp, &attr,
                      IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT |
                          IBV_QP_QKEY) != 0) {
      fail_verbs("ibv_modify_qp(SRD INIT)");
    }
    std::memset(&attr, 0, sizeof(attr));
    attr.qp_state = IBV_QPS_RTR;
    if (ibv_modify_qp(qp, &attr, IBV_QP_STATE) != 0) {
      fail_verbs("ibv_modify_qp(SRD RTR)");
    }
    std::memset(&attr, 0, sizeof(attr));
    attr.qp_state = IBV_QPS_RTS;
    attr.sq_psn = 0;
    int mask = IBV_QP_STATE | IBV_QP_SQ_PSN;
    if (srd_rnr_retry_supported_) {
      attr.rnr_retry = scenario_.rnr_retry;
      mask |= IBV_QP_RNR_RETRY;
    }
    if (ibv_modify_qp(qp, &attr, mask) != 0) {
      fail_verbs("ibv_modify_qp(SRD RTS)");
    }
  }

  void connect_rc(ibv_qp* qp, uint32_t remote_qpn) {
    ibv_qp_attr attr{};
    attr.qp_state = IBV_QPS_INIT;
    attr.pkey_index = kPkeyIndex;
    attr.port_num = kPort;
    attr.qp_access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE;
    if (ibv_modify_qp(qp, &attr,
                      IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT |
                          IBV_QP_ACCESS_FLAGS) != 0) {
      fail_verbs("ibv_modify_qp(RC INIT)");
    }
    std::memset(&attr, 0, sizeof(attr));
    attr.qp_state = IBV_QPS_RTR;
    attr.path_mtu = IBV_MTU_1024;
    attr.dest_qp_num = remote_qpn;
    attr.rq_psn = 0;
    attr.max_dest_rd_atomic = 1;
    attr.min_rnr_timer = kMinRnrTimer;
    attr.ah_attr.is_global = 1;
    attr.ah_attr.port_num = kPort;
    attr.ah_attr.grh.dgid = gid_;
    attr.ah_attr.grh.sgid_index = options_.gid_index;
    attr.ah_attr.grh.hop_limit = 1;
    if (ibv_modify_qp(qp, &attr,
                      IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU |
                          IBV_QP_DEST_QPN | IBV_QP_RQ_PSN |
                          IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER) !=
        0) {
      fail_verbs("ibv_modify_qp(RC RTR)");
    }
    std::memset(&attr, 0, sizeof(attr));
    attr.qp_state = IBV_QPS_RTS;
    attr.sq_psn = 0;
    attr.timeout = 14;
    attr.retry_cnt = 7;
    attr.rnr_retry = scenario_.rnr_retry;
    attr.max_rd_atomic = 1;
    if (ibv_modify_qp(qp, &attr,
                      IBV_QP_STATE | IBV_QP_SQ_PSN | IBV_QP_TIMEOUT |
                          IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY |
                          IBV_QP_MAX_QP_RD_ATOMIC) != 0) {
      fail_verbs("ibv_modify_qp(RC RTS)");
    }
  }

  uint64_t remote_address(uint32_t slot) const {
    return reinterpret_cast<uintptr_t>(recv_buffer_.data()) + slot * kSlotBytes;
  }

  uint64_t local_address(uint32_t slot) const {
    return reinterpret_cast<uintptr_t>(send_buffer_.data()) + slot * kSlotBytes;
  }

  void post_writes_rc() {
    for (uint32_t slot = 0; slot < scenario_.writes; ++slot) {
      ibv_sge sge{};
      sge.addr = local_address(slot);
      sge.length = kSlotBytes;
      sge.lkey = send_mr_->lkey;
      ibv_send_wr wr{};
      wr.wr_id = slot;
      wr.sg_list = &sge;
      wr.num_sge = 1;
      wr.opcode = IBV_WR_RDMA_WRITE_WITH_IMM;
      wr.send_flags = IBV_SEND_SIGNALED;
      wr.imm_data = htonl(slot);
      wr.wr.rdma.remote_addr = remote_address(slot);
      wr.wr.rdma.rkey = recv_mr_->rkey;
      ibv_send_wr* bad = nullptr;
      if (ibv_post_send(sender_, &wr, &bad) != 0) {
        fail_verbs("ibv_post_send");
      }
    }
  }

  void post_writes_srd() {
    ibv_qp_ex* qpx = ibv_qp_to_qp_ex(sender_);
    if (qpx == nullptr) {
      throw std::runtime_error("sender queue pair has no extended API");
    }
    ibv_wr_start(qpx);
    for (uint32_t slot = 0; slot < scenario_.writes; ++slot) {
      qpx->wr_id = slot;
      qpx->wr_flags = IBV_SEND_SIGNALED;
      ibv_wr_rdma_write_imm(qpx, recv_mr_->rkey, remote_address(slot),
                            htonl(slot));
      ibv_wr_set_ud_addr(qpx, ah_, receiver_->qp_num, kSrdQkey);
      ibv_wr_set_sge(qpx, send_mr_->lkey, local_address(slot), kSlotBytes);
    }
    if (ibv_wr_complete(qpx) != 0) fail_verbs("ibv_wr_complete");
  }

  void drain_send(Outcome& out) {
    ibv_wc wc[16];
    int n = 0;
    while ((n = ibv_poll_cq(send_cq_, 16, wc)) > 0) {
      for (int i = 0; i < n; ++i) {
        if (wc[i].status == IBV_WC_SUCCESS) {
          ++out.send_ok;
          continue;
        }
        if (out.send_error++ == 0) {
          out.first_send_error = ibv_wc_status_str(wc[i].status);
        }
      }
    }
    if (n < 0) fail_verbs("ibv_poll_cq(send)");
  }

  void note_receive(bool success, ibv_wc_status status, uint32_t immediate,
                    bool unsolicited, uint32_t& recv_count, Outcome& out) {
    if (!success) {
      if (out.first_recv_error == "-") {
        out.first_recv_error = ibv_wc_status_str(status);
      }
      return;
    }
    ++recv_count;
    out.recv_unsolicited += unsolicited ? 1 : 0;
    if (immediate >= scenario_.writes || seen_.at(immediate)) {
      out.bad_immediate = true;
      return;
    }
    seen_[immediate] = true;
  }

  void drain_receive(uint32_t& recv_count, Outcome& out) {
    if (seen_.empty()) seen_.assign(scenario_.writes, false);
#ifdef LMCACHE_EFA_UNSOLICITED
    if (recv_cq_ex_ != nullptr) {
      ibv_poll_cq_attr attr{};
      int rc = ibv_start_poll(recv_cq_ex_, &attr);
      if (rc == ENOENT) return;
      if (rc != 0) fail_verbs("ibv_start_poll");
      do {
        const bool success = recv_cq_ex_->status == IBV_WC_SUCCESS;
        note_receive(success, recv_cq_ex_->status,
                     success ? ntohl(ibv_wc_read_imm_data(recv_cq_ex_)) : 0,
                     efadv_wc_is_unsolicited(recv_efa_cq_), recv_count, out);
        rc = ibv_next_poll(recv_cq_ex_);
      } while (rc == 0);
      ibv_end_poll(recv_cq_ex_);
      return;
    }
#endif
    ibv_wc wc[16];
    int n = 0;
    while ((n = ibv_poll_cq(recv_cq_, 16, wc)) > 0) {
      for (int i = 0; i < n; ++i) {
        const bool success = wc[i].status == IBV_WC_SUCCESS &&
                             (wc[i].wc_flags & IBV_WC_WITH_IMM) != 0;
        note_receive(success, wc[i].status, ntohl(wc[i].imm_data), false,
                     recv_count, out);
      }
    }
    if (n < 0) fail_verbs("ibv_poll_cq(recv)");
  }

  Options options_;
  Scenario scenario_;
  ibv_context* ctx_ = nullptr;
  ibv_pd* pd_ = nullptr;
  ibv_gid gid_{};
  ibv_cq* send_cq_ = nullptr;
  ibv_cq* recv_cq_ = nullptr;
#ifdef LMCACHE_EFA_UNSOLICITED
  ibv_cq_ex* recv_cq_ex_ = nullptr;
  efadv_cq* recv_efa_cq_ = nullptr;
#endif
  ibv_qp* sender_ = nullptr;
  ibv_qp* receiver_ = nullptr;
  ibv_ah* ah_ = nullptr;
  ibv_mr* send_mr_ = nullptr;
  ibv_mr* recv_mr_ = nullptr;
  std::vector<uint8_t> send_buffer_;
  std::vector<uint8_t> recv_buffer_;
  std::vector<bool> seen_;
  uint32_t next_receive_ = 0;
  bool srd_rnr_retry_supported_ = false;
};

Outcome run(const Options& options, const Scenario& scenario) {
  Loopback loop(options, scenario);
  Outcome out;
  loop.post_receives(scenario.posted);
  loop.post_writes();
  const uint32_t first_target =
      scenario.unsolicited ? scenario.writes : scenario.posted;
  loop.poll(first_target, out.recv_before_repost, out);
  out.landed_before_repost = loop.landed();
  if (scenario.writes > scenario.posted && !scenario.unsolicited) {
    loop.post_receives(scenario.writes - scenario.posted);
    loop.poll(scenario.writes - scenario.posted, out.recv_after_repost, out);
  }
  out.landed_final = loop.landed();
  return out;
}

void print_result(const Options& options, const Scenario& scenario,
                  const Outcome& out) {
  std::cout << "RESULT transport=" << transport_name(options.transport)
            << " scenario=" << scenario.name << " posted=" << scenario.posted
            << " writes=" << scenario.writes
            << " rnr_retry=" << static_cast<int>(scenario.rnr_retry)
            << " recv_before_repost=" << out.recv_before_repost
            << " recv_after_repost=" << out.recv_after_repost
            << " recv_unsolicited=" << out.recv_unsolicited
            << " send_ok=" << out.send_ok << " send_error=" << out.send_error
            << " first_send_error=\"" << out.first_send_error << "\""
            << " first_recv_error=\"" << out.first_recv_error << "\""
            << " landed_before_repost=" << out.landed_before_repost
            << " landed_final=" << out.landed_final
            << " bad_immediate=" << (out.bad_immediate ? 1 : 0) << "\n";
}

// "yes" when the starved immediates waited for receives, "no" when they
// completed without them, "unclear" otherwise.
std::string verdict(const Scenario& starved, const Outcome& out) {
  if (out.bad_immediate) return "unclear";
  if (out.recv_before_repost == starved.writes) return "no";
  const uint32_t missing = starved.writes - starved.posted;
  if (out.recv_before_repost == starved.posted &&
      (out.recv_after_repost == missing || out.send_error > 0)) {
    return "yes";
  }
  return "unclear";
}

bool fits_is_clean(const Scenario& fits, const Outcome& out) {
  return out.recv_before_repost == fits.writes && out.send_ok == fits.writes &&
         out.send_error == 0 && out.landed_final == fits.writes &&
         !out.bad_immediate;
}

Options parse(int argc, char** argv) {
  Options options;
  std::vector<std::string> positional;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--transport" && i + 1 < argc) {
      const std::string value = argv[++i];
      if (value != "rc" && value != "srd") {
        throw std::invalid_argument("--transport takes rc or srd");
      }
      options.transport = value == "srd" ? Transport::kSrd : Transport::kRc;
    } else if (arg == "--wait-ms" && i + 1 < argc) {
      options.wait_ms = std::atoi(argv[++i]);
    } else if (arg == "--unsolicited") {
      options.unsolicited = true;
    } else if (arg == "--expect-consumes") {
      options.expect_consumes = true;
    } else if (arg.rfind("--", 0) == 0) {
      throw std::invalid_argument("unknown option " + arg);
    } else {
      positional.push_back(arg);
    }
  }
  if (!positional.empty()) options.device = positional[0];
  if (positional.size() > 1) {
    options.gid_index =
        static_cast<uint8_t>(std::strtoul(positional[1].c_str(), nullptr, 10));
  }
  if (options.unsolicited && options.transport != Transport::kSrd) {
    throw std::invalid_argument("--unsolicited needs --transport srd");
  }
#ifndef LMCACHE_EFA_UNSOLICITED
  if (options.unsolicited) {
    throw std::invalid_argument(
        "--unsolicited needs an efadv.h that declares "
        "EFADV_QP_FLAGS_UNSOLICITED_WRITE_RECV; rebuild against newer "
        "rdma-core");
  }
#endif
  return options;
}

}  // namespace

int main(int argc, char** argv) {
  Options options;
  try {
    options = parse(argc, argv);
  } catch (const std::exception& e) {
    std::cerr << e.what() << "\n";
    return kCannotRunExitCode;
  }
  if (!has_rdma_device()) {
    std::cout << "SKIP: no RDMA device present\n";
    return kSkipExitCode;
  }

  const Scenario fits{"fits", 8, 8, kInfiniteRnrRetry, false};
  const Scenario starved{"starved", 4, 8, kInfiniteRnrRetry, false};
  const Scenario starved_no_retry{"starved_no_retry", 0, 1, 0, false};
  const Scenario unsolicited{"unsolicited", 0, 8, kInfiniteRnrRetry, true};

  try {
    if (options.transport == Transport::kSrd && !report_srd_device(options)) {
      return kCannotRunExitCode;
    }
    const Outcome fits_out = run(options, fits);
    print_result(options, fits, fits_out);
    const Outcome starved_out = run(options, starved);
    print_result(options, starved, starved_out);
    const Outcome no_retry_out = run(options, starved_no_retry);
    print_result(options, starved_no_retry, no_retry_out);
    if (options.unsolicited) {
      print_result(options, unsolicited, run(options, unsolicited));
    }

    const std::string consumes = verdict(starved, starved_out);
    const bool silent_data =
        starved_out.landed_before_repost > starved_out.recv_before_repost;
    std::cout << "VERDICT transport=" << transport_name(options.transport)
              << " consumes_recv_wr=" << consumes
              << " data_without_notification=" << (silent_data ? "yes" : "no")
              << " fits_clean=" << (fits_is_clean(fits, fits_out) ? 1 : 0)
              << "\n";
    if (!fits_is_clean(fits, fits_out)) return 1;
    if (options.expect_consumes && consumes != "yes") return 1;
  } catch (const std::exception& e) {
    std::cerr << "probe failed: " << e.what() << "\n";
    return 1;
  }
  return 0;
}
