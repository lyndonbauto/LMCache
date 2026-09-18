// SPDX-License-Identifier: Apache-2.0
#pragma once

// libibverbs foundation for receiving KV payloads by RDMA write into
// LMCache's pinned L1 slab.
//
// LMCache is a pure *target* here: the Aerospike server issues every
// ibv_post_send, so this file never posts a send and never moves payload
// bytes itself. What it must do is:
//
//   1. open a device, allocate a protection domain,
//   2. register bounded windows of the L1 slab once, at init, with
//      LOCAL_WRITE | REMOTE_WRITE, producing the rkeys we publish outward,
//   3. create a queue pair and drive it INIT -> RTR -> RTS,
//   4. create an address handle for the server peer.
//
// In the baseline protocol that is the whole job, because the server fences
// on its own send CQ and the info reply is the completion.
//
// For *pipelined* fetches the server instead signals each piece with
// RDMA_WRITE_WITH_IMM, which raises a receive completion on this side. So
// this file also owns an optional notification path -- see
// enable_layer_notifications() -- which posts receive work requests and polls
// for those completions. It still posts no sends. See layer_pipeline.h for
// what the immediate data means and why arrival order cannot be trusted.
//
// Step 4 is easy to mistake for dead code because we only ever receive. It is
// not: the peer relationship is bidirectional at the device level, so without
// an AH for the server the server's RDMA write fails with UNKNOWN_PEER, which
// is very hard to diagnose from the receiving side.

#include "notification_depth.h"

#include <cstddef>
#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// Queue-pair flavor used to receive payloads.
enum class Transport {
  // RDMA is off; nothing in this file is exercised.
  kDisabled,
  // Reliable Connected. The portable path: InfiniBand, RoCE, and the
  // Soft-RoCE (rdma_rxe) software device used for local testing.
  kRc,
  // Scalable Reliable Datagram. AWS EFA only, and compiled in only when
  // LMCACHE_AEROSPIKE_EFA is defined.
  kSrd,
};

// Parse a transport name as produced by Python's RdmaTransport enum.
// Accepts "DISABLED", "RC", "SRD" case-insensitively.
// Throws std::invalid_argument on an unknown name.
Transport transport_from_string(const std::string& name);

// A pool of equally sized registration windows inside the L1 slab.
//
// One window is leased per in-flight request so the rkey handed to a remote
// writer never covers more than that request's own destination buffer. See
// docs/design/v1/distributed/l2_adapters/aerospike_rdma.md for why this is
// preferred over a single slab-wide registration.
struct WindowPlan {
  uint32_t window_count = 0;
  size_t window_bytes = 0;
};

// One registered window: what we publish and where it points.
struct RegisteredWindow {
  // Byte offset of this window from the base of the L1 slab. Chosen by
  // LMCache, never by the server.
  size_t offset = 0;
  size_t size = 0;
  // Remote key for this window, published to the Aerospike server.
  uint32_t rkey = 0;
};

// Local addressing information published to the server in
// "kv-sink-register".
struct LocalEndpoint {
  // Port GID rendered as 32 lowercase hex characters.
  std::string gid_hex;
  uint32_t qpn = 0;
  uint32_t psn = 0;
  uint64_t base_addr = 0;
  size_t total_bytes = 0;
  std::vector<RegisteredWindow> windows;
};

// Server addressing information parsed out of the register reply.
struct PeerEndpoint {
  std::string gid_hex;
  uint32_t qpn = 0;
  uint32_t psn = 0;
};

// Owns the verbs resources for one L1 slab: device, PD, MR window pool, and
// one queue pair per Aerospike node that may write into the leased window.
//
// Pipelined fetches fan out across several nodes, but every immediate lands in
// one readiness table, so the completion queue is shared while each remote
// writer gets its own RC (or SRD) queue pair. A single QP wired to one peer
// cannot receive writes from any other node.
//
// Not copyable. All methods throw std::runtime_error on verbs failure, with
// the failing call and errno in the message.
//
// Thread safety: construction, register_l1(), create_queue_pair*(), and
// connect_peer*() must run from a single thread during initialization.
class RdmaContext {
 public:
  // Open `device_name` (empty selects the first device the driver lists) and
  // allocate a protection domain.
  //
  // `gid_index` is the port GID index passed to ibv_query_gid; index 0 is the
  // link-local GID, which is what Soft-RoCE exposes.
  //
  // Throws std::runtime_error if no device matches, or std::invalid_argument
  // if `transport` is kSrd on a build without EFA support.
  RdmaContext(const std::string& device_name, uint8_t gid_index,
              Transport transport);
  ~RdmaContext();

  RdmaContext(const RdmaContext&) = delete;
  RdmaContext& operator=(const RdmaContext&) = delete;

  // Register the window pool over the L1 slab, exactly once.
  //
  // Each window is registered with LOCAL_WRITE | REMOTE_WRITE. Registration
  // is expensive enough to erase the entire benefit of the RDMA path, so it
  // happens here at init and never per request.
  //
  // Throws std::runtime_error if called twice, if the plan does not fit in
  // `size`, or if ibv_reg_mr fails (commonly because the slab is not pinned
  // or exceeds the locked-memory rlimit).
  void register_l1(void* base, size_t size, const WindowPlan& plan);

  // Create the queue pair and drive it INIT -> RTR -> RTS, then create the
  // address handle for `peer`.
  //
  // Must be called after register_l1(). Throws std::runtime_error if the QP
  // is already connected or any verbs call fails.
  void connect_peer(const PeerEndpoint& peer);

  void connect_peer(const std::string& node_name, const PeerEndpoint& peer);

  const LocalEndpoint& local_endpoint() const { return local_; }

  LocalEndpoint local_endpoint_for_node(const std::string& node_name) const;

  void create_queue_pair();

  void create_queue_pair_for_node(const std::string& node_name);

  bool is_connected() const;

  uint32_t notification_depth() const { return notification_depth_; }

  // Depth passed to enable_layer_notifications() before device clamping.
  //
  // Zero when notifications were never enabled.
  uint32_t notification_depth_requested() const {
    return notification_depth_requested_;
  }

  // Receive-path limits from ibv_query_device at device open time.
  const RdmaDeviceCaps& device_caps() const { return device_caps_; }

  // Number of registered windows, i.e. the max concurrent RDMA fetches.
  uint32_t window_count() const {
    return static_cast<uint32_t>(local_.windows.size());
  }

  // Size the receive path so the server can signal per-layer progress with
  // RDMA_WRITE_WITH_IMM instead of a single reply at the end.
  //
  // Must be called before create_queue_pair(), because it determines the
  // receive-queue and completion-queue depths. Without it the QP is built
  // with the baseline depth of one and this context cannot observe
  // notifications at all -- which is the correct shape for the non-pipelined
  // protocol, where the info reply is the completion.
  //
  // `depth` is the maximum number of unconsumed notifications to absorb, and
  // should be at least the slot count of the largest planned fetch. An
  // overflowing receive queue stalls the writer rather than losing data on
  // RC, but on SRD it drops the notification, so size this generously.
  //
  // Throws std::runtime_error if the queue pair already exists, and
  // std::invalid_argument if `depth` is zero.
  void enable_layer_notifications(uint32_t depth);

  // Post the receive work requests that incoming write-with-immediate
  // completions consume.
  //
  // Must be called after connect_peer(). Each notification consumed by
  // poll_notifications() is automatically replaced, so this only needs to run
  // once to prime the queue.
  //
  // Note that this is a Reliable Connected requirement. EFA can create a
  // queue pair with EFA_CREATE_QP_WITH_UNSOLICITED_WRITE_RECV, where incoming
  // write-with-immediate consumes no receive work request at all and the
  // completion is flagged unsolicited; on that path priming is unnecessary.
  // The RC path is implemented here because it is what Soft-RoCE supports.
  //
  // Throws std::runtime_error if notifications were not enabled, if the QP is
  // not connected, or if ibv_post_recv fails.
  void arm_notifications();

  // Drain up to `max_events` notifications and return their immediate data,
  // already converted to host byte order.
  //
  // Non-blocking: returns an empty vector when nothing has landed. Values are
  // returned in the order the completion queue yields them, which on SRD
  // bears no relation to the order the writes were posted -- decode them with
  // LayerReadiness rather than inferring anything from position.
  //
  // Each returned notification has its receive work request re-posted, so the
  // queue stays primed.
  //
  // Throws std::runtime_error if notifications were not enabled, if
  // ibv_poll_cq fails, or if a completion carries an error status.
  std::vector<uint32_t> poll_notifications(uint32_t max_events);

 private:
  struct PeerQueue;

  void ensure_completion_queue();
  PeerQueue& require_peer_queue(const std::string& node_name);
  const PeerQueue& require_peer_queue(const std::string& node_name) const;
  void drive_queue_to_rts(PeerQueue& peer, const PeerEndpoint& remote);
  void post_notification_receive_for_node(const std::string& node_name);

  struct Impl;
  std::unique_ptr<Impl> impl_;

  std::string device_name_;
  uint8_t gid_index_;
  Transport transport_;
  LocalEndpoint local_;
  bool registered_ = false;
  RdmaDeviceCaps device_caps_;
  uint32_t notification_depth_requested_ = 0;
  uint32_t notification_depth_ = 0;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
