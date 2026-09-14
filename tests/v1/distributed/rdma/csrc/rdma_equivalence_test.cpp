// SPDX-License-Identifier: Apache-2.0
//
// Byte-equivalence test for the Aerospike RDMA reception path.
//
// This is the deliverable that matters: data written by RDMA into LMCache's
// registered L1 windows must be byte-identical to the same payload as the
// normal, non-RDMA path would have produced.
//
// The sink side uses the *production* classes -- RdmaContext for the verbs
// foundation and build_register_command / parse_register_reply /
// parse_fetch_reply for the codec -- so this exercises the real code, not a
// reimplementation of it. Only the Aerospike server is mocked, and only its
// info-command control plane is faked; the data path is a genuine
// ibv_post_send RDMA write across the fabric.
//
// Requires a working RDMA device. On a machine with none, it exits 77
// (the automake "skip" convention) so the pytest wrapper can skip rather
// than fail.
//
// Build and run:
//   make -C tests/v1/distributed/rdma test

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <random>
#include <string>
#include <vector>

#include <infiniband/verbs.h>
#include <sys/mman.h>

#include "kv_sink_client.h"
#include "kv_sink_mock_writer.h"
#include "rdma_context.h"

namespace {

using lmcache::connector::rdma::FetchReply;
using lmcache::connector::rdma::LocalEndpoint;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::NodeRegistry;
using lmcache::connector::rdma::RdmaContext;
using lmcache::connector::rdma::SinkRequest;
using lmcache::connector::rdma::Transport;
using lmcache::test::KvSinkMockWriter;
using lmcache::test::MockRecord;

constexpr int kSkipExitCode = 77;

constexpr uint32_t kWindowCount = 4;
constexpr size_t kWindowBytes = 64 * 1024;
constexpr size_t kSlabBytes = kWindowCount * kWindowBytes;
constexpr size_t kPayloadBytes = 16 * 1024;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

// Report whether any RDMA device is present, so we can skip cleanly.
bool has_rdma_device() {
  int n = 0;
  ibv_device** list = ibv_get_device_list(&n);
  if (list != nullptr) {
    ibv_free_device_list(list);
  }
  return n > 0;
}

// A deterministic payload, so a partial or misdirected write is obvious
// rather than looking like plausible data.
std::vector<uint8_t> make_payload(size_t bytes, uint32_t seed) {
  std::mt19937 rng(seed);
  std::vector<uint8_t> out(bytes);
  for (size_t i = 0; i < bytes; ++i) {
    out[i] = static_cast<uint8_t>(rng() & 0xff);
  }
  return out;
}

}  // namespace

int main(int argc, char** argv) {
  const std::string device = argc > 1 ? argv[1] : "";

  if (!has_rdma_device()) {
    std::cout << "SKIP: no RDMA device present. To create a software device:\n"
              << "  sudo modprobe rdma_rxe\n"
              << "  sudo rdma link add rxe0 type rxe netdev lo\n"
              << "  ibv_devinfo -d rxe0\n";
    return kSkipExitCode;
  }

  try {
    // ---- Sink side: stand in for LMCache's pinned L1 slab. ----
    // Page-aligned and mlock'd, matching how L1 is pinned; ibv_reg_mr on
    // unpinned memory is what usually trips RLIMIT_MEMLOCK.
    void* slab = std::aligned_alloc(4096, kSlabBytes);
    if (slab == nullptr) {
      std::cerr << "failed to allocate slab\n";
      return 1;
    }
    std::memset(slab, 0, kSlabBytes);
    if (mlock(slab, kSlabBytes) != 0) {
      std::cout << "note: mlock failed (" << std::strerror(errno)
                << "); continuing, but raise 'ulimit -l' if ibv_reg_mr "
                   "fails\n";
    }

    RdmaContext sink(device, /*gid_index=*/0, Transport::kRc);
    sink.register_l1(slab, kSlabBytes, {kWindowCount, kWindowBytes});
    sink.create_queue_pair();
    check(sink.window_count() == kWindowCount,
          "sink registered " + std::to_string(kWindowCount) + " windows");

    // ---- Mock server side. ----
    KvSinkMockWriter writer(device, /*gid_index=*/0,
                            /*source_bytes=*/kSlabBytes);
    const std::string digest_a = "aa00000000000000000000000000000000000000";
    const std::string digest_b = "bb00000000000000000000000000000000000000";
    const std::vector<uint8_t> payload_a = make_payload(kPayloadBytes, 1);
    const std::vector<uint8_t> payload_b = make_payload(kPayloadBytes, 2);
    writer.add_record(MockRecord{digest_a, payload_a});
    writer.add_record(MockRecord{digest_b, payload_b});

    // ---- Handshake, in the order the protocol forces. ----
    // We publish window 0's rkey; the register command must carry our qpn and
    // psn, and only the reply tells us the peer's.
    const LocalEndpoint& local = sink.local_endpoint();
    const std::string register_cmd =
        lmcache::connector::rdma::build_register_command(local, 0);
    const std::string register_reply = writer.handle_register(register_cmd);

    NodeRegistration registration =
        lmcache::connector::rdma::parse_register_reply("mock-node",
                                                       register_reply);
    NodeRegistry registry;
    registry.set(registration);
    check(registry.region_for("mock-node") == writer.region(),
          "region handle is held per node");

    sink.connect_peer(registration.peer);
    check(sink.is_connected(), "sink queue pair reached RTS with an AH");

    // ---- Hot path: two chunks into window 0, at offsets we choose. ----
    std::vector<SinkRequest> sinks;
    sinks.push_back(SinkRequest{digest_a, 0, kPayloadBytes});
    sinks.push_back(SinkRequest{digest_b, kPayloadBytes, kPayloadBytes});

    const std::string fetch_cmd = lmcache::connector::rdma::build_fetch_command(
        "lmcache", registration.region, sinks);
    const std::string fetch_reply = writer.handle_fetch(fetch_cmd);
    const FetchReply parsed =
        lmcache::connector::rdma::parse_fetch_reply(fetch_reply);

    check(parsed.all_ok(), "fetch reply reports every chunk ok");
    check(parsed.bytes == 2 * kPayloadBytes,
          "fetch reply reports the expected byte count");

    // ---- The assertion that matters. ----
    // The reply is the completion, so by now the bytes must already be in the
    // slab with no further synchronization on our side.
    const auto* landed = static_cast<const uint8_t*>(slab);
    check(std::memcmp(landed, payload_a.data(), kPayloadBytes) == 0,
          "chunk A landed byte-identical to the non-RDMA payload");
    check(std::memcmp(landed + kPayloadBytes, payload_b.data(),
                      kPayloadBytes) == 0,
          "chunk B landed byte-identical to the non-RDMA payload");

    // Nothing may have been written past what we asked for.
    bool tail_clean = true;
    for (size_t i = 2 * kPayloadBytes; i < kSlabBytes; ++i) {
      if (landed[i] != 0) {
        tail_clean = false;
        break;
      }
    }
    check(tail_clean, "no bytes landed outside the requested offsets");

    // A sink outside the registered window must be refused, not written.
    std::vector<SinkRequest> overrun;
    overrun.push_back(SinkRequest{digest_a, kWindowBytes, kPayloadBytes});
    const FetchReply refused = lmcache::connector::rdma::parse_fetch_reply(
        writer.handle_fetch(lmcache::connector::rdma::build_fetch_command(
            "lmcache", registration.region, overrun)));
    check(!refused.all_ok() && refused.ok == 0,
          "a write past the window is refused, bounding the blast radius");

    munlock(slab, kSlabBytes);
    std::free(slab);
  } catch (const std::exception& e) {
    std::cerr << "EXCEPTION: " << e.what() << "\n";
    return 1;
  }

  std::cout << (failures == 0 ? "\nPASS\n" : "\nFAIL\n");
  return failures == 0 ? 0 : 1;
}
