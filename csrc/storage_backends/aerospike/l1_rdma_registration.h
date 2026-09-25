// SPDX-License-Identifier: Apache-2.0
#pragma once

// Plain description of the L1 slab and the RDMA window pool to register over
// it, passed from the Python L2 adapter factory into the native connector.
//
// This header is deliberately free of any libibverbs dependency so it can be
// compiled into every Aerospike build, including builds without RDMA support.
// On a build without RDMA the pybind layer still exposes the type but binds
// none of its fields, which is how the Python factory detects that RDMA was
// compiled out (see ``_build_native_rdma_registration``).

#include <cstddef>
#include <cstdint>
#include <string>

namespace lmcache {
namespace connector {

struct L1RdmaRegistration {
  // "DISABLED", "RC", or "SRD"; matches Python's RdmaTransport enum names.
  // Defaults to disabled so a default-constructed value is inert.
  std::string transport = "DISABLED";
  // libibverbs device to open; empty selects the first device listed.
  std::string device_name;
  // Port GID index for ibv_query_gid. 0 is the link-local GID.
  uint32_t gid_index = 0;
  // Base address and length of LMCache's pinned L1 slab.
  uint64_t base = 0;
  size_t size = 0;
  // Bounded window pool carved out of the slab. window_count is also the
  // maximum number of concurrently outstanding RDMA fetches.
  uint32_t window_count = 0;
  size_t window_bytes = 0;
  // L1 allocation alignment; every object in a window is rounded up to it.
  // 0 means unknown, and sizes are then taken unrounded.
  size_t align_bytes = 0;
  // Deadline for one kv-sink-fetch round trip, DMA included. Validated at
  // startup to be strictly below the L1 write-lock TTL, because a fetch that
  // outlives the lock lets the destination buffer become readable and
  // evictable while a node may still be writing into it.
  uint32_t fetch_timeout_ms = 0;

  // Report whether RDMA reception is enabled for this registration.
  bool is_enabled() const { return transport != "DISABLED"; }
};

}  // namespace connector
}  // namespace lmcache
