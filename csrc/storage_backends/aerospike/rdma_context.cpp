// SPDX-License-Identifier: Apache-2.0
#include "rdma_context.h"

#include <infiniband/verbs.h>

#ifdef LMCACHE_AEROSPIKE_EFA
  #include <infiniband/efadv.h>
#endif

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <random>
#include <sstream>
#include <stdexcept>
#include <system_error>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

// Physical port we drive. The PoC and all of our test setups use port 1, and
// Soft-RoCE only ever exposes port 1.
constexpr uint8_t kIbPort = 1;

// Partition key index; 0 is the default partition on IB and the only one on
// RoCE / Soft-RoCE.
constexpr uint16_t kPkeyIndex = 0;

// Shared queue key for SRD. Must match the server's choice; the PoC uses a
// fixed value rather than negotiating one.
constexpr uint32_t kSrdQKey = 0x11111111;

// Fail with the verbs call name and errno, which is where verbs puts almost
// all of its diagnostics.
[[noreturn]] void throw_verbs(const char* call) {
  int saved = errno;
  std::ostringstream os;
  os << call << " failed: " << std::strerror(saved) << " (errno " << saved
     << ")";
  throw std::runtime_error(os.str());
}

// Render a 16-byte GID as 32 lowercase hex characters, the format the
// kv-sink info commands exchange.
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

// Parse 32 hex characters back into a GID.
// Throws std::invalid_argument when the input is not exactly 32 hex chars.
ibv_gid gid_from_hex(const std::string& hex) {
  if (hex.size() != 32) {
    throw std::invalid_argument("gid must be 32 hex characters, got '" + hex +
                                "'");
  }
  ibv_gid gid;
  std::memset(&gid, 0, sizeof(gid));
  for (int i = 0; i < 16; ++i) {
    unsigned byte = 0;
    if (std::sscanf(hex.c_str() + 2 * i, "%2x", &byte) != 1) {
      throw std::invalid_argument("gid contains a non-hex character: '" + hex +
                                  "'");
    }
    gid.raw[i] = static_cast<uint8_t>(byte);
  }
  return gid;
}

// A fresh packet sequence number. RC compares PSNs, so a stale value from a
// previous connection shows up as silently dropped writes.
uint32_t random_psn() {
  std::random_device rd;
  return rd() & 0xffffff;
}

}  // namespace

Transport transport_from_string(const std::string& name) {
  std::string upper = name;
  std::transform(
      upper.begin(), upper.end(), upper.begin(),
      [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
  if (upper == "DISABLED") {
    return Transport::kDisabled;
  }
  if (upper == "RC") {
    return Transport::kRc;
  }
  if (upper == "SRD") {
    return Transport::kSrd;
  }
  throw std::invalid_argument("unknown rdma transport '" + name +
                              "'; expected DISABLED, RC, or SRD");
}

// Holds the raw verbs handles so the header stays free of <infiniband/verbs.h>.
struct RdmaContext::Impl {
  ibv_context* ctx = nullptr;
  ibv_pd* pd = nullptr;
  ibv_cq* cq = nullptr;
  ibv_qp* qp = nullptr;
  ibv_ah* ah = nullptr;
  // One MR per window. Registered once in register_l1().
  std::vector<ibv_mr*> mrs;
  ibv_gid local_gid{};

  ~Impl() {
    // Tear down in reverse dependency order; a leaked MR pins host memory
    // for the process lifetime.
    if (ah != nullptr) {
      ibv_destroy_ah(ah);
    }
    if (qp != nullptr) {
      ibv_destroy_qp(qp);
    }
    for (ibv_mr* mr : mrs) {
      if (mr != nullptr) {
        ibv_dereg_mr(mr);
      }
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
  }
};

RdmaContext::RdmaContext(const std::string& device_name, uint8_t gid_index,
                         Transport transport)
    : impl_(new Impl()),
      device_name_(device_name),
      gid_index_(gid_index),
      transport_(transport) {
  if (transport_ == Transport::kSrd) {
#ifndef LMCACHE_AEROSPIKE_EFA
    throw std::invalid_argument(
        "rdma transport SRD requires an EFA-enabled build; rebuild with "
        "BUILD_WITH_AEROSPIKE_EFA=1, or use transport RC (which also works "
        "on Soft-RoCE)");
#endif
  }

  int num_devices = 0;
  ibv_device** list = ibv_get_device_list(&num_devices);
  if (list == nullptr || num_devices == 0) {
    if (list != nullptr) {
      ibv_free_device_list(list);
    }
    throw std::runtime_error(
        "no RDMA devices found; load a driver (for local testing: modprobe "
        "rdma_rxe && rdma link add rxe0 type rxe netdev <iface>) and verify "
        "with ibv_devinfo");
  }

  ibv_device* chosen = nullptr;
  std::ostringstream available;
  for (int i = 0; i < num_devices; ++i) {
    const char* name = ibv_get_device_name(list[i]);
    if (i > 0) {
      available << ", ";
    }
    available << (name != nullptr ? name : "?");
    if (device_name.empty() ||
        (name != nullptr && device_name == std::string(name))) {
      chosen = list[i];
      if (device_name.empty()) {
        device_name_ = name != nullptr ? name : "";
      }
      break;
    }
  }
  if (chosen == nullptr) {
    std::string names = available.str();
    ibv_free_device_list(list);
    throw std::runtime_error("RDMA device '" + device_name +
                             "' not found; available devices: " + names);
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
  local_.gid_hex = gid_to_hex(impl_->local_gid);
}

RdmaContext::~RdmaContext() = default;

void RdmaContext::register_l1(void* base, size_t size, const WindowPlan& plan) {
  if (registered_) {
    throw std::runtime_error(
        "L1 slab is already registered; re-registering would invalidate the "
        "rkeys already published to Aerospike nodes");
  }
  if (base == nullptr) {
    throw std::runtime_error("cannot register a null L1 slab pointer");
  }
  if (plan.window_count == 0 || plan.window_bytes == 0) {
    throw std::runtime_error("window plan must have a positive count and size");
  }
  const size_t total =
      static_cast<size_t>(plan.window_count) * plan.window_bytes;
  if (total > size) {
    std::ostringstream os;
    os << "window pool needs " << total << " bytes (" << plan.window_count
       << " x " << plan.window_bytes << ") but the L1 slab is only " << size
       << " bytes";
    throw std::runtime_error(os.str());
  }

  const int access = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE;
  impl_->mrs.reserve(plan.window_count);
  local_.windows.reserve(plan.window_count);
  auto* bytes = static_cast<uint8_t*>(base);

  for (uint32_t i = 0; i < plan.window_count; ++i) {
    const size_t offset = static_cast<size_t>(i) * plan.window_bytes;
    ibv_mr* mr =
        ibv_reg_mr(impl_->pd, bytes + offset, plan.window_bytes, access);
    if (mr == nullptr) {
      // Impl's destructor releases the windows registered so far.
      throw std::runtime_error(
          "ibv_reg_mr failed for window " + std::to_string(i) +
          "; check that the L1 slab is pinned and that RLIMIT_MEMLOCK is "
          "large enough for " +
          std::to_string(total) + " bytes");
    }
    impl_->mrs.push_back(mr);
    RegisteredWindow window;
    window.offset = offset;
    window.size = plan.window_bytes;
    window.rkey = mr->rkey;
    local_.windows.push_back(window);
  }

  local_.base_addr = reinterpret_cast<uint64_t>(base);
  local_.total_bytes = total;
  registered_ = true;
}

void RdmaContext::create_queue_pair() {
  if (qp_created_) {
    throw std::runtime_error("queue pair already created");
  }
  if (!registered_) {
    throw std::runtime_error(
        "register_l1() must run before create_queue_pair() so the register "
        "command can publish rkeys alongside the qpn");
  }

  // With notifications off the server fences and replies, so no completion
  // ever lands here and this CQ stays empty by design. With them on it must
  // hold one entry per unconsumed write-with-immediate.
  const uint32_t queue_depth =
      notification_depth_ == 0 ? 1 : notification_depth_;
  impl_->cq = ibv_create_cq(impl_->ctx, static_cast<int>(queue_depth), nullptr,
                            nullptr, 0);
  if (impl_->cq == nullptr) {
    throw_verbs("ibv_create_cq");
  }

  ibv_qp_init_attr init{};
  init.send_cq = impl_->cq;
  init.recv_cq = impl_->cq;
  init.cap.max_send_wr = 1;
  init.cap.max_recv_wr = queue_depth;
  init.cap.max_send_sge = 1;
  init.cap.max_recv_sge = 1;
  init.sq_sig_all = 0;

  if (transport_ == Transport::kSrd) {
#ifdef LMCACHE_AEROSPIKE_EFA
    // EFA exposes SRD only through the device-specific extension; the
    // portable ibv_create_qp cannot express it.
    ibv_qp_init_attr_ex ex{};
    ex.send_cq = impl_->cq;
    ex.recv_cq = impl_->cq;
    ex.cap = init.cap;
    ex.qp_type = IBV_QPT_DRIVER;
    ex.comp_mask = IBV_QP_INIT_ATTR_PD;
    ex.pd = impl_->pd;
    efadv_qp_init_attr efa_attr{};
    efa_attr.driver_qp_type = EFADV_QP_DRIVER_TYPE_SRD;
    impl_->qp =
        efadv_create_qp_ex(impl_->ctx, &ex, &efa_attr, sizeof(efa_attr));
    if (impl_->qp == nullptr) {
      throw_verbs("efadv_create_qp_ex");
    }
#else
    throw std::runtime_error("SRD requested in a build without EFA support");
#endif
  } else {
    init.qp_type = IBV_QPT_RC;
    impl_->qp = ibv_create_qp(impl_->pd, &init);
    if (impl_->qp == nullptr) {
      throw_verbs("ibv_create_qp");
    }
  }

  // INIT. SRD is a datagram type and needs the qkey instead of access flags.
  ibv_qp_attr attr{};
  attr.qp_state = IBV_QPS_INIT;
  attr.pkey_index = kPkeyIndex;
  attr.port_num = kIbPort;
  int mask = IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT;
  if (transport_ == Transport::kSrd) {
    attr.qkey = kSrdQKey;
    mask |= IBV_QP_QKEY;
  } else {
    // The server writes into us, so REMOTE_WRITE must be enabled on the QP
    // as well as on each MR.
    attr.qp_access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE;
    mask |= IBV_QP_ACCESS_FLAGS;
  }
  if (ibv_modify_qp(impl_->qp, &attr, mask) != 0) {
    throw_verbs("ibv_modify_qp(INIT)");
  }

  local_.qpn = impl_->qp->qp_num;
  local_.psn = random_psn();
  qp_created_ = true;
}

void RdmaContext::connect_peer(const PeerEndpoint& peer) {
  if (!qp_created_) {
    throw std::runtime_error(
        "create_queue_pair() must run before connect_peer()");
  }
  if (connected_) {
    throw std::runtime_error(
        "queue pair is already connected; a node restart requires a fresh "
        "RdmaContext so the stale region handle is not reused");
  }

  const ibv_gid peer_gid = gid_from_hex(peer.gid_hex);

  ibv_qp_attr attr{};
  int mask = 0;

  // RTR.
  attr.qp_state = IBV_QPS_RTR;
  if (transport_ == Transport::kSrd) {
    // SRD carries addressing per work request via an AH, so RTR needs
    // nothing beyond the state change.
    mask = IBV_QP_STATE;
  } else {
    attr.path_mtu = IBV_MTU_1024;
    attr.dest_qp_num = peer.qpn;
    attr.rq_psn = peer.psn;
    attr.max_dest_rd_atomic = 1;
    attr.min_rnr_timer = 12;
    attr.ah_attr.is_global = 1;
    attr.ah_attr.port_num = kIbPort;
    attr.ah_attr.sl = 0;
    attr.ah_attr.src_path_bits = 0;
    attr.ah_attr.grh.dgid = peer_gid;
    attr.ah_attr.grh.sgid_index = gid_index_;
    attr.ah_attr.grh.hop_limit = 1;
    attr.ah_attr.grh.traffic_class = 0;
    attr.ah_attr.grh.flow_label = 0;
    mask = IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
           IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER;
  }
  if (ibv_modify_qp(impl_->qp, &attr, mask) != 0) {
    throw_verbs("ibv_modify_qp(RTR)");
  }

  // RTS.
  std::memset(&attr, 0, sizeof(attr));
  attr.qp_state = IBV_QPS_RTS;
  attr.sq_psn = local_.psn;
  mask = IBV_QP_STATE | IBV_QP_SQ_PSN;
  if (transport_ != Transport::kSrd) {
    attr.timeout = 14;
    attr.retry_cnt = 7;
    attr.rnr_retry = 7;
    attr.max_rd_atomic = 1;
    mask |= IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY |
            IBV_QP_MAX_QP_RD_ATOMIC;
  }
  if (ibv_modify_qp(impl_->qp, &attr, mask) != 0) {
    throw_verbs("ibv_modify_qp(RTS)");
  }

  // The address handle for the server. We only ever receive, so this looks
  // unnecessary -- it is not. The peer relationship is bidirectional at the
  // device level, and without an AH for the server its RDMA write fails with
  // UNKNOWN_PEER, a failure that surfaces on the *sender* and is nearly
  // invisible from here.
  ibv_ah_attr ah_attr{};
  ah_attr.is_global = 1;
  ah_attr.port_num = kIbPort;
  ah_attr.sl = 0;
  ah_attr.src_path_bits = 0;
  std::memcpy(&ah_attr.grh.dgid, &peer_gid, sizeof(peer_gid));
  ah_attr.grh.sgid_index = gid_index_;
  ah_attr.grh.hop_limit = 1;
  impl_->ah = ibv_create_ah(impl_->pd, &ah_attr);
  if (impl_->ah == nullptr) {
    throw_verbs("ibv_create_ah");
  }

  connected_ = true;
}

void RdmaContext::enable_layer_notifications(uint32_t depth) {
  if (qp_created_) {
    throw std::runtime_error(
        "enable_layer_notifications() must run before create_queue_pair(); it "
        "sets the receive-queue depth, which is fixed at creation");
  }
  if (depth == 0) {
    throw std::invalid_argument(
        "notification depth must be positive; pass the slot count of the "
        "largest planned fetch");
  }
  notification_depth_ = depth;
}

void RdmaContext::arm_notifications() {
  if (notification_depth_ == 0) {
    throw std::runtime_error(
        "enable_layer_notifications() was never called, so this queue pair "
        "has no room to receive per-layer signals");
  }
  if (!connected_) {
    throw std::runtime_error("arm_notifications() requires a connected peer");
  }

  for (uint32_t i = 0; i < notification_depth_; ++i) {
    post_notification_receive();
  }
}

std::vector<uint32_t> RdmaContext::poll_notifications(uint32_t max_events) {
  if (notification_depth_ == 0) {
    throw std::runtime_error(
        "enable_layer_notifications() was never called, so no notification "
        "can ever arrive");
  }

  std::vector<uint32_t> immediates;
  for (uint32_t drained = 0; drained < max_events; ++drained) {
    ibv_wc wc{};
    const int got = ibv_poll_cq(impl_->cq, 1, &wc);
    if (got < 0) {
      throw_verbs("ibv_poll_cq");
    }
    if (got == 0) {
      break;
    }
    if (wc.status != IBV_WC_SUCCESS) {
      throw std::runtime_error(std::string("notification completion failed: ") +
                               ibv_wc_status_str(wc.status));
    }
    // A plain RDMA write raises nothing here, so anything that is not
    // write-with-immediate means the peer is not speaking the pipelined
    // protocol. Surfacing it as an error beats silently reporting no
    // progress and timing out.
    if (wc.opcode != IBV_WC_RECV_RDMA_WITH_IMM) {
      throw std::runtime_error(
          "unexpected completion opcode " + std::to_string(wc.opcode) +
          " on the notification queue; expected IBV_WC_RECV_RDMA_WITH_IMM");
    }
    if ((wc.wc_flags & IBV_WC_WITH_IMM) == 0) {
      throw std::runtime_error(
          "write-with-immediate completion carried no immediate data");
    }

    // imm_data is big endian on the wire regardless of host order.
    immediates.push_back(be32toh(wc.imm_data));

    // Replace the work request this completion consumed, so a long fetch
    // cannot exhaust the receive queue partway through.
    post_notification_receive();
  }
  return immediates;
}

void RdmaContext::post_notification_receive() {
  // A write-with-immediate consumes a receive work request but scatters no
  // payload into it -- the data went to the RDMA address. So the request
  // needs no buffer, and num_sge stays zero.
  ibv_recv_wr wr{};
  wr.wr_id = 0;
  wr.sg_list = nullptr;
  wr.num_sge = 0;
  wr.next = nullptr;

  ibv_recv_wr* bad = nullptr;
  if (ibv_post_recv(impl_->qp, &wr, &bad) != 0) {
    throw_verbs("ibv_post_recv");
  }
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
