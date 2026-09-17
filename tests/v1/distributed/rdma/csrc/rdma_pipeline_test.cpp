// SPDX-License-Identifier: Apache-2.0
//
// Pipelining test for the Aerospike RDMA reception path.
//
// The equivalence test proves bytes arrive intact. This proves something
// different and, for the pipelining work, more important: that LMCache can
// *consume a layer while later layers have not arrived yet*.
//
// That is the property the whole pipelining thesis rests on. If it holds, the
// GPU can start on layer 0 while the rest of the model is still crossing the
// wire. If it does not, there is nothing to overlap and the remaining
// pipelining tickets are void.
//
// Two things are deliberately asserted that a naive test would miss:
//
//   1. **Layers arriving out of order.** AWS SRD provides reliable but
//      out-of-order delivery -- it sprays packets over many paths and leaves
//      reordering to the layer above. So this test pushes layers 3, 2, 1 in
//      that order and requires that layer 3 is reported ready while layer 1
//      is not. A high-water-mark implementation passes on Soft-RoCE and
//      breaks on EFA; this test refuses to let that through.
//
//   2. **Untouched regions stay untouched.** When layer 0 is reported ready,
//      the destination bytes for layers 1-3 must still be zero. Otherwise
//      "ready" would be meaningless and a consumer could read uninitialized
//      KV as though it were cache.
//
// The data path is a genuine RDMA_WRITE_WITH_IMM across the fabric, driven
// through the production RdmaContext and the production RequestPlan /
// LayerReadiness. Only the Aerospike control plane is mocked.
//
// Requires a working RDMA device; exits 77 (the automake "skip" convention)
// when none is present.
//
// Usage: rdma_pipeline_test [device] [gid_index]
//
// See the GID index note in rdma_equivalence_test.cpp: Soft-RoCE on `lo`
// needs index 1, EFA needs 0.

#include <cstdint>
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
#include "layer_pipeline.h"
#include "rdma_context.h"

namespace {

using lmcache::connector::rdma::ArrivalStatus;
using lmcache::connector::rdma::LayerReadiness;
using lmcache::connector::rdma::LocalEndpoint;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::RdmaContext;
using lmcache::connector::rdma::RequestPlan;
using lmcache::connector::rdma::Transport;
using lmcache::test::KvSinkMockWriter;
using lmcache::test::MockRecord;

constexpr int kSkipExitCode = 77;

constexpr uint32_t kWindowCount = 2;
constexpr size_t kWindowBytes = 256 * 1024;
constexpr size_t kSlabBytes = kWindowCount * kWindowBytes;

// Four layers, each split into two pieces, to exercise the "a layer is not
// ready until every one of its pieces has landed" rule. A single-piece layer
// would make the slot accounting trivially correct.
constexpr uint32_t kLayerCount = 4;
constexpr uint32_t kPiecesPerLayer = 2;
constexpr size_t kPieceBytes = 8 * 1024;
constexpr uint16_t kGeneration = 0x2a2a;
// This harness drives the data path over a real fabric, so it uses a single
// chunk to keep the mock server simple. Request-scoped bookkeeping across
// several chunks and nodes is covered by request_plan_test.cpp.
constexpr uint32_t kChunkId = 0;

constexpr int kPollAttempts = 2000000;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

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

// Digest naming the piece `piece` of layer `layer`, in the mock's registry.
std::string digest_for(uint32_t layer, uint32_t piece) {
  char buf[41];
  std::snprintf(buf, sizeof(buf), "%02x%02x%036x", layer, piece, 0);
  return std::string(buf);
}

// Slot index of a given layer and piece, matching the order they are added
// to the plan below.
uint16_t slot_index_of(uint32_t layer, uint32_t piece) {
  return static_cast<uint16_t>(layer * kPiecesPerLayer + piece);
}

// Report whether every byte of a region is still zero.
bool region_is_zero(const uint8_t* base, size_t offset, size_t length) {
  for (size_t i = 0; i < length; ++i) {
    if (base[offset + i] != 0) {
      return false;
    }
  }
  return true;
}

// Poll notifications into `readiness` until `layer` is ready, or give up.
//
// Returns the number of notifications consumed, or -1 on timeout.
int drain_until_layer_ready(RdmaContext& sink, LayerReadiness& readiness,
                            uint32_t layer) {
  int consumed = 0;
  for (int attempt = 0; attempt < kPollAttempts; ++attempt) {
    const std::vector<uint32_t> immediates = sink.poll_notifications(8);
    for (const uint32_t immediate : immediates) {
      const ArrivalStatus status = readiness.note_arrival(immediate);
      if (status == ArrivalStatus::kStaleGeneration ||
          status == ArrivalStatus::kUnknownSlot) {
        std::cout << "  FAIL unexpected arrival status for immediate 0x"
                  << std::hex << immediate << std::dec << "\n";
        ++failures;
      }
      ++consumed;
    }
    if (readiness.is_layer_ready(layer)) {
      return consumed;
    }
  }
  return -1;
}

}  // namespace

int main(int argc, char** argv) {
  const std::string device = argc > 1 ? argv[1] : "";
  const uint8_t gid_index =
      argc > 2 ? static_cast<uint8_t>(std::strtoul(argv[2], nullptr, 10)) : 0;

  if (!has_rdma_device()) {
    std::cout << "SKIP: no RDMA device present. To create a software device:\n"
              << "  sudo modprobe rdma_rxe\n"
              << "  sudo rdma link add rxe0 type rxe netdev lo\n"
              << "  ibv_devinfo -d rxe0\n";
    return kSkipExitCode;
  }

  try {
    // ---- The plan. LMCache chooses every destination offset. ----
    RequestPlan plan(kGeneration);
    for (uint32_t layer = 0; layer < kLayerCount; ++layer) {
      for (uint32_t piece = 0; piece < kPiecesPerLayer; ++piece) {
        const size_t offset =
            static_cast<size_t>(slot_index_of(layer, piece)) * kPieceBytes;
        plan.add_slot(layer, kChunkId, offset, kPieceBytes);
      }
    }
    check(plan.slot_count() == kLayerCount * kPiecesPerLayer,
          "plan holds one slot per layer piece");
    check(plan.expected_slots(0) == kPiecesPerLayer,
          "each layer expects every one of its pieces");

    // ---- Sink side: LMCache's pinned L1 slab. ----
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
    const uint8_t* slab_bytes = static_cast<const uint8_t*>(slab);

    RdmaContext sink(device, gid_index, Transport::kRc);
    sink.register_l1(slab, kSlabBytes, {kWindowCount, kWindowBytes});
    // Size the receive queue before the QP exists. One notification per slot,
    // so a fetch can never exhaust it midway.
    sink.enable_layer_notifications(static_cast<uint32_t>(plan.slot_count()) +
                                    1);
    sink.create_queue_pair();

    // ---- Mock server, holding one record per layer piece. ----
    KvSinkMockWriter writer(device, gid_index, /*source_bytes=*/kSlabBytes);
    std::vector<std::vector<uint8_t>> payloads;
    for (uint32_t layer = 0; layer < kLayerCount; ++layer) {
      for (uint32_t piece = 0; piece < kPiecesPerLayer; ++piece) {
        std::vector<uint8_t> payload =
            make_payload(kPieceBytes, slot_index_of(layer, piece) + 1);
        writer.add_record(MockRecord{digest_for(layer, piece), payload});
        payloads.push_back(std::move(payload));
      }
    }

    // ---- Handshake. ----
    const LocalEndpoint& local = sink.local_endpoint();
    const std::string register_reply = writer.handle_register(
        lmcache::connector::rdma::build_register_command(local, 0));
    const NodeRegistration registration =
        lmcache::connector::rdma::parse_register_reply("mock-node",
                                                       register_reply);
    sink.connect_peer(registration.peer);
    check(sink.is_connected(), "sink queue pair reached RTS with an AH");

    sink.arm_notifications();

    LayerReadiness readiness(plan);
    check(!readiness.is_layer_ready(0),
          "no layer is ready before anything is pushed");

    // ---- Stage 1: push only layer 0. ----
    for (uint32_t piece = 0; piece < kPiecesPerLayer; ++piece) {
      const uint16_t slot = slot_index_of(0, piece);
      writer.push_slot(digest_for(0, piece), plan.slot(slot).offset,
                       plan.slot(slot).length, plan.immediate_for(slot));
    }

    check(drain_until_layer_ready(sink, readiness, 0) >= 0,
          "layer 0 reported ready from its write-with-immediate signals");

    // The pipelining proof: layer 0 is readable, the rest has not arrived.
    bool layer0_intact = true;
    for (uint32_t piece = 0; piece < kPiecesPerLayer; ++piece) {
      const uint16_t slot = slot_index_of(0, piece);
      layer0_intact =
          layer0_intact && std::memcmp(slab_bytes + plan.slot(slot).offset,
                                       payloads[slot].data(), kPieceBytes) == 0;
    }
    check(layer0_intact, "layer 0 bytes are correct at the moment it is ready");

    bool later_layers_untouched = true;
    for (uint32_t layer = 1; layer < kLayerCount; ++layer) {
      for (uint32_t piece = 0; piece < kPiecesPerLayer; ++piece) {
        const uint16_t slot = slot_index_of(layer, piece);
        later_layers_untouched =
            later_layers_untouched &&
            region_is_zero(slab_bytes, plan.slot(slot).offset, kPieceBytes);
      }
    }
    check(later_layers_untouched,
          "layers 1-3 are still untouched, so layer 0 is genuinely usable "
          "early");
    check(!readiness.is_layer_ready(1) && !readiness.is_layer_ready(3),
          "unfinished layers are not reported ready");
    check(readiness.ready_layers() == std::vector<uint32_t>{0},
          "exactly one layer is ready");
    check(!readiness.all_ready(), "the fetch as a whole is not complete");

    // ---- Stage 2: push layer 3, skipping 1 and 2 entirely. ----
    // This is the out-of-order case SRD will actually produce.
    for (uint32_t piece = 0; piece < kPiecesPerLayer; ++piece) {
      const uint16_t slot = slot_index_of(3, piece);
      writer.push_slot(digest_for(3, piece), plan.slot(slot).offset,
                       plan.slot(slot).length, plan.immediate_for(slot));
    }
    check(drain_until_layer_ready(sink, readiness, 3) >= 0,
          "layer 3 reported ready while layers 1 and 2 have not been sent");
    check(readiness.ready_layers() == (std::vector<uint32_t>{0, 3}),
          "readiness is a set, not a high-water mark");
    check(!readiness.is_layer_ready(1) && !readiness.is_layer_ready(2),
          "skipped layers stay unready even though a later layer arrived");

    // ---- Stage 3: a single piece of layer 1, which must not suffice. ----
    {
      const uint16_t slot = slot_index_of(1, 0);
      writer.push_slot(digest_for(1, 0), plan.slot(slot).offset,
                       plan.slot(slot).length, plan.immediate_for(slot));
    }
    bool saw_partial = false;
    for (int attempt = 0; attempt < kPollAttempts && !saw_partial; ++attempt) {
      for (const uint32_t immediate : sink.poll_notifications(8)) {
        readiness.note_arrival(immediate);
        saw_partial = true;
      }
    }
    check(saw_partial, "the lone piece of layer 1 raised a notification");
    check(!readiness.is_layer_ready(1),
          "a layer with one of two pieces landed is NOT ready");

    // ---- Stage 4: finish everything. ----
    for (uint32_t layer = 1; layer <= 2; ++layer) {
      for (uint32_t piece = 0; piece < kPiecesPerLayer; ++piece) {
        if (layer == 1 && piece == 0) {
          continue;  // already pushed above
        }
        const uint16_t slot = slot_index_of(layer, piece);
        writer.push_slot(digest_for(layer, piece), plan.slot(slot).offset,
                         plan.slot(slot).length, plan.immediate_for(slot));
      }
    }
    check(drain_until_layer_ready(sink, readiness, 2) >= 0,
          "remaining layers reported ready");
    check(readiness.all_ready(), "every slot in the plan has landed");

    bool all_intact = true;
    for (uint16_t slot = 0; slot < plan.slot_count(); ++slot) {
      all_intact =
          all_intact && std::memcmp(slab_bytes + plan.slot(slot).offset,
                                    payloads[slot].data(), kPieceBytes) == 0;
    }
    check(all_intact, "every layer is byte-identical once the fetch completes");

    // ---- Stale and bogus immediates are rejected, not counted. ----
    LayerReadiness fresh(plan);
    check(fresh.note_arrival(lmcache::connector::rdma::encode_immediate(
              kGeneration + 1, 0)) == ArrivalStatus::kStaleGeneration,
          "a write from a superseded fetch is rejected by generation");
    check(fresh.note_arrival(lmcache::connector::rdma::encode_immediate(
              kGeneration, 9999)) == ArrivalStatus::kUnknownSlot,
          "an immediate naming no slot in the plan is rejected");
    check(fresh.note_arrival(plan.immediate_for(0)) == ArrivalStatus::kAccepted,
          "the first piece of a two-piece layer is accepted but incomplete");
    check(
        fresh.note_arrival(plan.immediate_for(0)) == ArrivalStatus::kDuplicate,
        "a repeated immediate is reported as a duplicate, not double counted");
    check(fresh.note_arrival(plan.immediate_for(1)) ==
              ArrivalStatus::kLayerComplete,
          "the final piece of a layer reports completion");
    check(fresh.slots_landed() == 2, "duplicates do not inflate the count");

    munlock(slab, kSlabBytes);
    std::free(slab);
  } catch (const std::exception& e) {
    std::cerr << "EXCEPTION: " << e.what() << "\n";
    return 1;
  }

  if (failures != 0) {
    std::cout << "\n" << failures << " check(s) FAILED\n";
    return 1;
  }
  std::cout << "\nPASS\n";
  return 0;
}
