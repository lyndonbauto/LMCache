// SPDX-License-Identifier: Apache-2.0
#pragma once

// Sizing the receive queue for pipelined write-with-immediate notifications.
//
// Each piece the server lands consumes one posted receive work request on
// LMCache's queue pair, so the depth we ask ibv_create_qp for must cover the
// largest request plan we will build. The window and record cap give an upper
// bound on that plan, but the device caps how many receive work requests a
// single queue pair may hold -- and EFA's limits are far below the Soft-RoCE
// setups this code was first exercised on.
//
// This file keeps that arithmetic in one place so the driver, RdmaContext,
// and the device-free logic harness can share it without pulling in
// libibverbs.

#include <cstddef>
#include <cstdint>

namespace lmcache {
namespace connector {
namespace rdma {

// Receive-path limits reported by ibv_query_device for the opened device.
struct RdmaDeviceCaps {
  uint32_t max_recv_wr_per_qp = 0;
  uint32_t max_cq_entries = 0;
};

// Desired versus clamped notification depth and the slot limit that follows.
struct NotificationDepthBudget {
  uint32_t desired_depth = 0;
  uint32_t effective_depth = 0;
  uint32_t max_slots_per_request = 0;
};

// Upper bound on unconsumed notifications for one request, from the leased
// window and the connector's record cap. Also bounded by kMaxSlotsPerRequest
// because the immediate carries only 16 slot-index bits.
uint32_t desired_notification_depth(size_t window_bytes,
                                    size_t max_record_bytes);

// Clamp desired depth to what the device can allocate on the queue pair and
// completion queue. max_slots_per_request equals effective_depth.
NotificationDepthBudget notification_depth_budget(
    size_t window_bytes, size_t max_record_bytes,
    const RdmaDeviceCaps& device_caps);

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
