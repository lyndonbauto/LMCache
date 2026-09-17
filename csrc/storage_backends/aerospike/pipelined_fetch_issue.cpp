// SPDX-License-Identifier: Apache-2.0

#include "pipelined_fetch_issue.h"

#include "kv_sink_client.h"

#include <sstream>
#include <stdexcept>
#include <utility>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

std::string build_declined_reply(uint32_t requested,
                                 const std::vector<uint16_t>& failed_slots) {
  std::ostringstream oss;
  oss << "n=" << requested << ";accepted=0;failed=";
  for (size_t i = 0; i < failed_slots.size(); ++i) {
    if (i > 0) {
      oss << ',';
    }
    oss << failed_slots[i];
  }
  oss << ";bytes=0";
  return oss.str();
}

std::vector<uint16_t> slots_for_command(const std::string& command) {
  const std::string sinks = find_info_field(command, "sinks");
  std::vector<uint16_t> slots;
  size_t start = 0;
  while (start < sinks.size()) {
    const size_t comma = sinks.find(',', start);
    const std::string sink = sinks.substr(
        start, comma == std::string::npos ? std::string::npos : comma - start);
    const size_t slot_pos = sink.rfind('#');
    if (slot_pos != std::string::npos && slot_pos + 1 < sink.size()) {
      slots.push_back(
          static_cast<uint16_t>(std::stoul(sink.substr(slot_pos + 1))));
    }
    if (comma == std::string::npos) {
      break;
    }
    start = comma + 1;
  }
  return slots;
}

}  // namespace

std::string declined_reply_for_command(const std::string& command) {
  const std::vector<uint16_t> owned_slots = slots_for_command(command);
  return build_declined_reply(static_cast<uint32_t>(owned_slots.size()),
                              owned_slots);
}

uint16_t issue_pipelined_fetch(PipelinedFetchSession& session,
                               const PipelinedNodeInfoSender& send_info,
                               const std::vector<ChunkPlacement>& placements,
                               const std::vector<ChunkNodeBinding>& chunk_nodes,
                               const std::vector<SlotDigest>& slot_digests) {
  const uint16_t generation =
      session.begin_request(placements, chunk_nodes, slot_digests);
  try {
    const std::map<std::string, std::string> commands =
        session.pipelined_fetch_commands();
    for (const auto& entry : commands) {
      const std::string& node_name = entry.first;
      const std::string& command = entry.second;
      const std::vector<uint16_t> owned_slots = slots_for_command(command);
      std::string reply;
      try {
        reply = send_info(node_name, command);
      } catch (...) {
        reply = build_declined_reply(static_cast<uint32_t>(owned_slots.size()),
                                     owned_slots);
      }
      session.on_node_reply(node_name, reply, generation);
    }
    return generation;
  } catch (...) {
    if (session.has_active_request()) {
      session.abandon_request();
    }
    throw;
  }
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
