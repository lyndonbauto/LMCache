// SPDX-License-Identifier: Apache-2.0
//
// Client-side tests for the pipelined fetch: the command codec, and what
// happens to readiness when the server says it cannot serve a slot.
//
// Needs no RDMA device. The codec is string work, and the failure handling is
// bookkeeping; rdma_pipeline_test.cpp drives the same codec over a real
// fabric against the mock server.
//
// Two things are defended here.
//
// **The command must be distinguishable.** A pipelined fetch is a separate
// command name rather than a flag, because a server that does not implement
// it has to fail outright. If it instead fell back to the all-or-nothing
// fetch, the writes would land but carry no immediate, so no notification
// would ever arrive and the client would block until its deadline on data
// that is already in its buffer.
//
// **A refused slot must not be mistaken for a slow one.** The two are
// identical from the client's side -- an absent arrival -- so the reply has to
// name the slots it declined, and the tracker has to record them. Without
// that, one missing record turns into a full deadline wait, and the layer
// must never be reported ready on the bytes that did arrive, because the rest
// of its buffer holds whatever the previous tenant left there.
//
// Usage: pipelined_fetch_test    (takes no arguments; ignores any passed, so
// it can share the harness runner with the device-dependent tests)

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "kv_sink_client.h"
#include "layer_pipeline.h"

namespace {

using lmcache::connector::rdma::ArrivalStatus;
using lmcache::connector::rdma::build_pipelined_fetch_command;
using lmcache::connector::rdma::encode_immediate;
using lmcache::connector::rdma::kDefaultMaxSinksPerPipelinedCommand;
using lmcache::connector::rdma::LayerReadiness;
using lmcache::connector::rdma::NodeRegistration;
using lmcache::connector::rdma::parse_pipelined_fetch_reply;
using lmcache::connector::rdma::parse_register_reply;
using lmcache::connector::rdma::PipelinedFetchReply;
using lmcache::connector::rdma::RequestPlan;
using lmcache::connector::rdma::SinkRequest;

constexpr uint16_t kGeneration = 0x1234;
constexpr size_t kPieceBytes = 4096;

int failures = 0;

void check(bool condition, const std::string& what) {
  if (condition) {
    std::cout << "  ok   " << what << "\n";
  } else {
    std::cout << "  FAIL " << what << "\n";
    ++failures;
  }
}

SinkRequest sink(const std::string& digest, size_t offset, size_t length,
                 uint16_t slot) {
  SinkRequest request;
  request.digest_hex = digest;
  request.offset = offset;
  request.length = length;
  request.slot = slot;
  return request;
}

void test_the_command_carries_a_slot_per_sink() {
  std::cout << "the command carries a slot per sink\n";

  const std::vector<SinkRequest> sinks = {
      sink("aa00", 0, kPieceBytes, 0),
      sink("bb11", kPieceBytes, kPieceBytes, 1),
  };
  const std::string command =
      build_pipelined_fetch_command("kv", 7, kGeneration, sinks);

  check(command.rfind("kv-sink-fetch-pipelined:", 0) == 0,
        "the command has its own name, so a server without the feature "
        "rejects it rather than silently serving it unpipelined");
  check(command.find(";gen=4660;") != std::string::npos,
        "the generation is sent once for the whole command");
  check(
      command.find("sinks=aa00@0:4096#0,bb11@4096:4096#1") != std::string::npos,
      "each sink names its slot after the destination");
}

void test_the_command_rejects_what_would_hang_the_request() {
  std::cout << "the command rejects what would hang the request\n";

  bool threw = false;
  try {
    build_pipelined_fetch_command("kv", 7, kGeneration, {});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "a fetch with no sinks is refused");

  threw = false;
  try {
    build_pipelined_fetch_command("kv", 7, kGeneration,
                                  {sink("aa00", 0, kPieceBytes, 3),
                                   sink("bb11", kPieceBytes, kPieceBytes, 3)});
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw,
        "a repeated slot index is refused: the second arrival would count as "
        "a duplicate and its layer could never complete");
}

void test_the_reply_is_parsed() {
  std::cout << "the reply is parsed\n";

  const PipelinedFetchReply full =
      parse_pipelined_fetch_reply("n=4;accepted=2;failed=1,3;bytes=8192");
  check(full.requested == 4, "the requested count is read");
  check(full.accepted == 2, "the accepted count is read");
  check(full.bytes == 8192, "the byte count is read");
  check(full.failed_slots == std::vector<uint16_t>({1, 3}),
        "the failed slots are read");
  check(!full.all_accepted(), "a reply naming failures is not all accepted");

  const PipelinedFetchReply clean =
      parse_pipelined_fetch_reply("n=4;accepted=4;failed=;bytes=16384");
  check(clean.failed_slots.empty(),
        "an empty failed list means nothing failed");
  check(clean.all_accepted(), "a reply with no failures is all accepted");

  const PipelinedFetchReply terse =
      parse_pipelined_fetch_reply("n=1;accepted=1");
  check(terse.failed_slots.empty() && terse.bytes == 0 && terse.all_accepted(),
        "failed and bytes are optional");
}

void test_a_malformed_reply_is_refused() {
  std::cout << "a malformed reply is refused\n";

  // Not parsing is the right outcome: the alternative is defaulting accepted
  // to zero, which reads as "the server did nothing" and would abandon a
  // fetch whose writes are in flight.
  bool threw = false;
  try {
    parse_pipelined_fetch_reply("accepted=4;bytes=1");
  } catch (const std::runtime_error&) {
    threw = true;
  }
  check(threw, "a reply with no requested count is refused");

  threw = false;
  try {
    parse_pipelined_fetch_reply("n=4;bytes=1");
  } catch (const std::runtime_error&) {
    threw = true;
  }
  check(threw, "a reply with no accepted count is refused");

  threw = false;
  try {
    parse_pipelined_fetch_reply("n=4;accepted=0;failed=70000");
  } catch (const std::runtime_error&) {
    threw = true;
  }
  check(threw, "a failed slot too large for the 16-bit index is refused");
}

void test_a_refused_slot_does_not_hang_its_layer() {
  std::cout << "a refused slot does not hang its layer\n";

  // Layer 0 spans two chunks; layer 1 likewise. The server serving chunk 1
  // cannot find its record for layer 0.
  RequestPlan plan(kGeneration);
  const uint16_t l0c0 = plan.add_slot(0, 0, 0, kPieceBytes);
  const uint16_t l0c1 = plan.add_slot(0, 1, kPieceBytes, kPieceBytes);
  const uint16_t l1c0 = plan.add_slot(1, 0, 2 * kPieceBytes, kPieceBytes);
  const uint16_t l1c1 = plan.add_slot(1, 1, 3 * kPieceBytes, kPieceBytes);

  LayerReadiness readiness(plan);
  check(readiness.note_unservable(l0c1),
        "marking a slot unservable is reported as new");
  check(!readiness.note_unservable(l0c1),
        "marking the same slot twice is reported as already known");

  readiness.note_arrival(plan.immediate_for(l0c0));
  check(!readiness.is_layer_ready(0),
        "a layer with an unservable slot is never ready, even though every "
        "slot that will ever arrive has arrived");
  check(readiness.unservable_layers() == std::vector<uint32_t>({0}),
        "the layer needing recompute is named");
  check(readiness.has_unservable_slots(), "the request reports a failure");

  check(readiness.note_arrival(plan.immediate_for(l1c0)) ==
            ArrivalStatus::kAccepted,
        "an unrelated layer still accumulates");
  check(readiness.note_arrival(plan.immediate_for(l1c1)) ==
            ArrivalStatus::kLayerComplete,
        "an unrelated layer still completes, so the request can serve what "
        "did arrive and recompute only layer 0");
  check(!readiness.all_ready(),
        "the request as a whole never completes while a slot is unservable");
}

void test_a_late_write_for_a_refused_slot_is_not_progress() {
  std::cout << "a late write for a refused slot is not progress\n";

  RequestPlan plan(kGeneration);
  const uint16_t only = plan.add_slot(0, 0, 0, kPieceBytes);

  LayerReadiness readiness(plan);
  readiness.note_unservable(only);

  // A server may name a slot failed and still have posted the write, or a
  // retry may land later. Either way the layer has already been written off,
  // so counting the arrival would make readiness disagree with what the
  // caller was told.
  check(readiness.note_arrival(plan.immediate_for(only)) ==
            ArrivalStatus::kDuplicate,
        "a write arriving for a slot already written off is not counted");
  check(!readiness.is_layer_ready(0),
        "the layer stays unready after the late write");

  check(!readiness.note_unservable(0xffff),
        "a failed slot outside the plan is ignored rather than throwing, "
        "since a confused server must not be able to crash the client");
}

void test_a_plan_becomes_sinks_for_one_node() {
  std::cout << "a plan becomes sinks for one node\n";

  // The request spans two chunks held by different nodes. Each node's command
  // carries only its own chunk's slots, but in the *request's* numbering.
  RequestPlan plan(kGeneration);
  plan.add_slot(0, 0, 0, kPieceBytes);
  plan.add_slot(0, 1, kPieceBytes, kPieceBytes);
  plan.add_slot(1, 0, 2 * kPieceBytes, kPieceBytes);
  plan.add_slot(1, 1, 3 * kPieceBytes, kPieceBytes);

  std::vector<SinkRequest> node_sinks;
  for (const uint16_t slot_index : plan.slots_for_chunk(1)) {
    node_sinks.push_back(sink("dd22", plan.slot(slot_index).offset,
                              plan.slot(slot_index).length, slot_index));
  }
  const std::string command =
      build_pipelined_fetch_command("kv", 7, plan.generation(), node_sinks);

  check(node_sinks.size() == 2, "the node is asked only for its own chunk");
  check(command.find("#1") != std::string::npos &&
            command.find("#3") != std::string::npos,
        "the slots keep the request's numbering, so this node's immediates "
        "stay distinguishable from the other node's");
  check(command.find("#0") == std::string::npos,
        "the other node's slots are absent");
}

void test_register_reply_default_max_sinks_when_omitted() {
  std::cout << "register reply default max_sinks when omitted\n";

  const NodeRegistration without_token =
      parse_register_reply("n1", "region=1;qpn=2;gid=aa");
  check(without_token.max_sinks_per_command ==
            kDefaultMaxSinksPerPipelinedCommand,
        "missing max_sinks falls back to the historical 256 cap");

  const NodeRegistration with_token =
      parse_register_reply("n2", "region=1;qpn=2;gid=bb;max_sinks=512");
  check(with_token.max_sinks_per_command == 512u,
        "max_sinks token overrides the default");
}

}  // namespace

int main() {
  try {
    test_the_command_carries_a_slot_per_sink();
    test_the_command_rejects_what_would_hang_the_request();
    test_the_reply_is_parsed();
    test_a_malformed_reply_is_refused();
    test_a_refused_slot_does_not_hang_its_layer();
    test_a_late_write_for_a_refused_slot_is_not_progress();
    test_a_plan_becomes_sinks_for_one_node();
    test_register_reply_default_max_sinks_when_omitted();
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
