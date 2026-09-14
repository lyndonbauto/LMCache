// SPDX-License-Identifier: Apache-2.0
#pragma once

// libibverbs foundation for receiving KV payloads by RDMA write into
// LMCache's pinned L1 slab.
//
// LMCache is a pure *target* here: the Aerospike server issues every
// ibv_post_send and fences on its own completion queue, and the info-command
// reply is the completion signal. So this file never posts a work request and
// never polls a CQ for data. What it must do is:
//
//   1. open a device, allocate a protection domain,
//   2. register bounded windows of the L1 slab once, at init, with
//      LOCAL_WRITE | REMOTE_WRITE, producing the rkeys we publish outward,
//   3. create a queue pair and drive it INIT -> RTR -> RTS,
//   4. create an address handle for the server peer.
//
// Step 4 is easy to mistake for dead code because we only ever receive. It is
// not: the peer relationship is bidirectional at the device level, so without
// an AH for the server the server's RDMA write fails with UNKNOWN_PEER, which
// is very hard to diagnose from the receiving side.

#include <cstddef>
#include <cstdint>
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

// Owns the verbs resources for one L1 slab: device, PD, MR window pool, QP,
// and the peer AH.
//
// Not copyable. All methods throw std::runtime_error on verbs failure, with
// the failing call and errno in the message.
//
// Thread safety: construction, register_l1(), and connect_peer() must be
// called from a single thread during initialization. local_endpoint() and
// window() are const and safe to call concurrently afterwards.
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

  // Return what should be sent to the server in "kv-sink-register".
  //
  // Valid after register_l1(); `qpn`/`psn` are only meaningful once the QP
  // exists, which happens in create_queue_pair().
  const LocalEndpoint& local_endpoint() const { return local_; }

  // Create the queue pair and move it to INIT, without a peer.
  //
  // Split out from connect_peer() because the register/fetch protocol needs
  // our qpn and psn *in* the register command, but only learns the server's
  // gid and qpn *from* the reply. So the order is: create_queue_pair(),
  // send register, then connect_peer() with the parsed reply.
  //
  // Throws std::runtime_error if called twice or if verbs fails.
  void create_queue_pair();

  // Report whether connect_peer() has completed and the QP is in RTS.
  bool is_connected() const { return connected_; }

  // Number of registered windows, i.e. the max concurrent RDMA fetches.
  uint32_t window_count() const {
    return static_cast<uint32_t>(local_.windows.size());
  }

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;

  std::string device_name_;
  uint8_t gid_index_;
  Transport transport_;
  LocalEndpoint local_;
  bool registered_ = false;
  bool qp_created_ = false;
  bool connected_ = false;
};

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
