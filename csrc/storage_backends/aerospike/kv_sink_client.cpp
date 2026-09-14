// SPDX-License-Identifier: Apache-2.0
#include "kv_sink_client.h"

#include <cstdlib>
#include <sstream>
#include <stdexcept>

namespace lmcache {
namespace connector {
namespace rdma {
namespace {

// Parse a required unsigned field, naming the field on failure so a
// malformed reply is attributable.
uint64_t require_u64(const std::string& reply, const std::string& key,
                     const char* op) {
  const std::string value = find_info_field(reply, key);
  if (value.empty()) {
    throw std::runtime_error(std::string(op) + ": reply is missing '" + key +
                             "'; got '" + reply + "'");
  }
  try {
    return std::stoull(value);
  } catch (const std::exception&) {
    throw std::runtime_error(std::string(op) + ": field '" + key +
                             "' is not a number: '" + value + "'");
  }
}

// Split a comma-separated list; returns an empty vector for an empty input.
std::vector<std::string> split_csv(const std::string& text) {
  std::vector<std::string> out;
  if (text.empty()) {
    return out;
  }
  size_t start = 0;
  while (true) {
    const size_t comma = text.find(',', start);
    if (comma == std::string::npos) {
      out.push_back(text.substr(start));
      return out;
    }
    out.push_back(text.substr(start, comma - start));
    start = comma + 1;
  }
}

}  // namespace

std::string build_register_command(const LocalEndpoint& local,
                                   uint32_t window_index) {
  if (window_index >= local.windows.size()) {
    throw std::out_of_range("window index " + std::to_string(window_index) +
                            " is not registered (" +
                            std::to_string(local.windows.size()) + " windows)");
  }
  const RegisteredWindow& window = local.windows[window_index];
  std::ostringstream os;
  os << "kv-sink-register:transport=verbs"
     << ";gid=" << local.gid_hex << ";qpn=" << local.qpn << ";psn=" << local.psn
     << ";rkey=" << window.rkey << ";addr=" << (local.base_addr + window.offset)
     << ";size=" << window.size;
  return os.str();
}

std::string build_fetch_command(const std::string& ns, uint64_t region,
                                const std::vector<SinkRequest>& sinks) {
  if (sinks.empty()) {
    throw std::invalid_argument("kv-sink-fetch requires at least one sink");
  }
  std::ostringstream os;
  os << "kv-sink-fetch:namespace=" << ns << ";region=" << region << ";sinks=";
  for (size_t i = 0; i < sinks.size(); ++i) {
    if (i > 0) {
      os << ',';
    }
    os << sinks[i].digest_hex << '@' << sinks[i].offset << ':'
       << sinks[i].length;
  }
  return os.str();
}

std::string find_info_field(const std::string& reply, const std::string& key) {
  // Match only at a field boundary so that "qpn=" does not match inside
  // "peer-qpn=".
  const std::string needle = key + "=";
  size_t pos = 0;
  while ((pos = reply.find(needle, pos)) != std::string::npos) {
    const bool at_boundary = pos == 0 || reply[pos - 1] == ';' ||
                             reply[pos - 1] == ':' || reply[pos - 1] == '\t';
    if (!at_boundary) {
      pos += needle.size();
      continue;
    }
    const size_t start = pos + needle.size();
    size_t end = reply.find(';', start);
    if (end == std::string::npos) {
      end = reply.find('\n', start);
    }
    return end == std::string::npos ? reply.substr(start)
                                    : reply.substr(start, end - start);
  }
  return std::string();
}

NodeRegistration parse_register_reply(const std::string& node_name,
                                      const std::string& reply) {
  NodeRegistration registration;
  registration.node_name = node_name;
  registration.region = require_u64(reply, "region", "kv-sink-register");
  registration.peer.qpn =
      static_cast<uint32_t>(require_u64(reply, "qpn", "kv-sink-register"));

  registration.peer.gid_hex = find_info_field(reply, "gid");
  if (registration.peer.gid_hex.empty()) {
    throw std::runtime_error(
        "kv-sink-register: reply is missing 'gid'; without the peer GID we "
        "cannot create the address handle, and the server's write would fail "
        "with UNKNOWN_PEER. Reply was '" +
        reply + "'");
  }

  // psn is optional: SRD does not compare sequence numbers, so the server may
  // omit it.
  const std::string psn = find_info_field(reply, "psn");
  if (!psn.empty()) {
    registration.peer.psn =
        static_cast<uint32_t>(require_u64(reply, "psn", "kv-sink-register"));
  }

  registration.valid = true;
  return registration;
}

FetchReply parse_fetch_reply(const std::string& reply) {
  FetchReply parsed;
  parsed.requested =
      static_cast<uint32_t>(require_u64(reply, "n", "kv-sink-fetch"));
  parsed.ok = static_cast<uint32_t>(require_u64(reply, "ok", "kv-sink-fetch"));

  // bytes and in-place are informational; absence is not fatal.
  const std::string bytes = find_info_field(reply, "bytes");
  if (!bytes.empty()) {
    parsed.bytes = require_u64(reply, "bytes", "kv-sink-fetch");
  }
  const std::string in_place = find_info_field(reply, "in-place");
  if (!in_place.empty()) {
    parsed.in_place =
        static_cast<uint32_t>(require_u64(reply, "in-place", "kv-sink-fetch"));
  }
  parsed.results = split_csv(find_info_field(reply, "results"));
  return parsed;
}

void NodeRegistry::set(const NodeRegistration& registration) {
  by_node_[registration.node_name] = registration;
}

uint64_t NodeRegistry::region_for(const std::string& node_name) const {
  const auto it = by_node_.find(node_name);
  if (it == by_node_.end() || !it->second.valid) {
    throw std::runtime_error(
        "no valid kv-sink region for node '" + node_name +
        "'; the node was never registered, or its registration was "
        "invalidated by a slab re-registration or node restart");
  }
  return it->second.region;
}

void NodeRegistry::invalidate_all() { by_node_.clear(); }

size_t NodeRegistry::valid_count() const {
  size_t count = 0;
  for (const auto& entry : by_node_) {
    if (entry.second.valid) {
      ++count;
    }
  }
  return count;
}

}  // namespace rdma
}  // namespace connector
}  // namespace lmcache
