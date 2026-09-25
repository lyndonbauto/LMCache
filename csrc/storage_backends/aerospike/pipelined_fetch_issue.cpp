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

uint16_t send_commands(PipelinedFetchSession& session,
                       const PipelinedNodeInfoSender& send_info,
                       uint16_t generation) {
  try {
    const std::vector<std::pair<std::string, std::string>> commands =
        session.pipelined_fetch_commands();
    for (const auto& entry : commands) {
      const std::string& node_name = entry.first;
      const std::string& command = entry.second;
      const std::vector<uint16_t> owned_slots =
          pipelined_command_slot_indices(command);
      std::string reply;
      try {
        reply = send_info(node_name, command);
      } catch (...) {
        reply = build_declined_reply(static_cast<uint32_t>(owned_slots.size()),
                                     owned_slots);
      }
      session.on_node_reply(node_name, command, reply, generation);
    }
    return generation;
  } catch (...) {
    if (session.has_active_request()) {
      session.abandon_request();
    }
    throw;
  }
}

}  // namespace

std::string declined_reply_for_command(const std::string& command) {
  const std::vector<uint16_t> owned_slots =
      pipelined_command_slot_indices(command);
  return build_declined_reply(static_cast<uint32_t>(owned_slots.size()),
                              owned_slots);
}

uint16_t issue_pipelined_fetch(PipelinedFetchSession& session,
                               const PipelinedNodeInfoSender& send_info,
                               const std::vector<ChunkPlacement>& placements,
                               const std::vector<ChunkNodeBinding>& chunk_nodes,
                               const std::vector<SlotDigest>& slot_digests) {
  return send_commands(
      session, send_info,
      session.begin_request(placements, chunk_nodes, slot_digests));
}

uint16_t issue_planned_fetch(PipelinedFetchSession& session,
                             const PipelinedNodeInfoSender& send_info,
                             const std::vector<PlannedSlot>& slots) {
  return send_commands(session, send_info,
                       session.begin_request_from_slots(slots));
}

uint16_t issue_planned_fetch(PipelinedFetchPool& pool,
                             const PipelinedNodeInfoSender& send_info,
                             const std::vector<PlannedSlot>& slots) {
  const uint16_t generation = pool.begin_request_from_slots(slots);
  try {
    for (const auto& entry : pool.pipelined_fetch_commands(generation)) {
      std::string reply;
      try {
        reply = send_info(entry.first, entry.second);
      } catch (...) {
        reply = declined_reply_for_command(entry.second);
      }
      pool.on_node_reply(entry.first, entry.second, reply, generation);
    }
  } catch (...) {
    pool.abandon_request(generation);
    throw;
  }
  return generation;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
