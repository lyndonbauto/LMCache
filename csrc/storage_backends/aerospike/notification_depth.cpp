// SPDX-License-Identifier: Apache-2.0
#include "notification_depth.h"

#include "layer_pipeline.h"

#include <algorithm>

namespace lmcache {
namespace connector {
namespace rdma {

uint32_t desired_notification_depth(size_t window_bytes,
                                    size_t max_record_bytes) {
  if (max_record_bytes == 0 || window_bytes == 0) {
    return 1;
  }
  const size_t slots = (window_bytes + max_record_bytes - 1) / max_record_bytes;
  return static_cast<uint32_t>(
      std::min(slots, static_cast<size_t>(kMaxSlotsPerRequest)));
}

NotificationDepthBudget notification_depth_budget(
    size_t window_bytes, size_t max_record_bytes,
    const RdmaDeviceCaps& device_caps) {
  NotificationDepthBudget budget;
  budget.desired_depth =
      desired_notification_depth(window_bytes, max_record_bytes);
  budget.effective_depth = budget.desired_depth;
  if (device_caps.max_recv_wr_per_qp != 0) {
    budget.effective_depth =
        std::min(budget.effective_depth, device_caps.max_recv_wr_per_qp);
  }
  if (device_caps.max_cq_entries != 0) {
    budget.effective_depth =
        std::min(budget.effective_depth, device_caps.max_cq_entries);
  }
  if (budget.effective_depth == 0) {
    budget.effective_depth = 1;
  }
  budget.max_slots_per_request = budget.effective_depth;
  return budget;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
