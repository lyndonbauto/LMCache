// SPDX-License-Identifier: Apache-2.0
#pragma once

#include "pipelined_fetch_pool.h"
#include "pipelined_fetch_session.h"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace lmcache {
namespace connector {
namespace rdma {

// Sends one pipelined fetch info command to a cluster node.
using PipelinedNodeInfoSender = std::function<std::string(
    const std::string& node_name, const std::string& command)>;

// Build a pipelined acknowledgement that declines every slot in ``command``.
std::string declined_reply_for_command(const std::string& command);

// Begin a pipelined fetch, issue every node's command through ``send_info``,
// and feed acknowledgements back into ``session``.
//
// When ``send_info`` throws, every slot owned by that node is marked
// unservable via a synthetic declined reply. When any other step throws after
// ``begin_request``, the active request is abandoned before the exception
// propagates.
//
// Thread safety: ``session`` must not be accessed concurrently for the
// duration of this call; the caller holds whatever lock serializes it.
//
// Returns the request generation issued to the servers.
uint16_t issue_pipelined_fetch(PipelinedFetchSession& session,
                               const PipelinedNodeInfoSender& send_info,
                               const std::vector<ChunkPlacement>& placements,
                               const std::vector<ChunkNodeBinding>& chunk_nodes,
                               const std::vector<SlotDigest>& slot_digests);

// Same as issue_pipelined_fetch(), for a request planned by the caller: see
// PipelinedFetchSession::begin_request_from_slots.
uint16_t issue_planned_fetch(PipelinedFetchSession& session,
                             const PipelinedNodeInfoSender& send_info,
                             const std::vector<PlannedSlot>& slots);

// Same, through a pool: the request runs in the window of its first slot,
// and only that request is abandoned on failure.
//
// Thread safety: other windows' requests may be used concurrently.
uint16_t issue_planned_fetch(PipelinedFetchPool& pool,
                             const PipelinedNodeInfoSender& send_info,
                             const std::vector<PlannedSlot>& slots);

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
