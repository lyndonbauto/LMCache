// SPDX-License-Identifier: Apache-2.0
#pragma once

// Mock stand-in for the Aerospike server's RDMA writer.
//
// The real server side lives on branch sriram/kv-rdma-poc of the Aerospike
// server repo and is not generally available, so this reproduces the only
// behaviour LMCache depends on:
//
//   * it accepts the same "kv-sink-register" / "kv-sink-fetch" command
//     strings,
//   * it performs real ibv_post_send RDMA writes into the registered windows
//     at the offsets the client asked for,
//   * it fences on its own send completion queue before replying, so that the
//     reply *is* the completion, and
//   * it replies in the same key=value; format.
//
// It is deliberately not a network server: the control channel is a plain
// string in and a string out, so a test can drive it in-process while the
// data path stays a genuine RDMA write across the fabric. That keeps the
// thing under test (the verbs data path) real and the thing being faked (an
// Aerospike info command) trivial.

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace lmcache {
namespace test {

// Payload the mock serves for a given record digest.
struct MockRecord {
  std::string digest_hex;
  std::vector<uint8_t> payload;
};

// A single-node mock of the kv-sink server side.
//
// Not copyable. Methods throw std::runtime_error on verbs failure.
class KvSinkMockWriter {
 public:
  // Open `device_name` (empty selects the first device), allocate a
  // protection domain, and register the source buffer that payloads are
  // written from.
  //
  // `source_bytes` bounds the total size of all records this mock can serve.
  KvSinkMockWriter(const std::string& device_name, uint8_t gid_index,
                   size_t source_bytes);
  ~KvSinkMockWriter();

  KvSinkMockWriter(const KvSinkMockWriter&) = delete;
  KvSinkMockWriter& operator=(const KvSinkMockWriter&) = delete;

  // Make `record` fetchable, copying its payload into the registered source
  // buffer.
  //
  // Throws std::runtime_error if the source buffer has no room left.
  void add_record(const MockRecord& record);

  // Handle a "kv-sink-register" command.
  //
  // Parses the client's gid, qpn, psn, rkey, addr, and size, drives this
  // side's queue pair INIT -> RTR -> RTS toward that client, creates an
  // address handle for it, and returns the reply:
  //
  //   region=<id>;transport=verbs;qp=rc;qpn=<u32>;psn=<u32>;gid=<hex>
  //
  // Throws std::runtime_error if the command is malformed or verbs fails.
  std::string handle_register(const std::string& command);

  // Handle a "kv-sink-fetch" command.
  //
  // For every sink, posts an RDMA write of that record's payload to
  // client_addr + offset using the client's rkey, then polls this side's send
  // CQ until every write has completed. Only then does it build the reply:
  //
  //   n=<count>;ok=<count>;bytes=<total>;in-place=<count>;results=ok,ok,...
  //
  // A sink naming an unknown digest, or one that would overrun the client's
  // registered window, is reported as "err" in `results` and excluded from
  // `ok`, without any write being posted for it.
  //
  // Throws std::runtime_error if the command is malformed, if handle_register
  // has not run, or if a completion reports a verbs error.
  std::string handle_fetch(const std::string& command);

  // Push one piece of one layer with RDMA_WRITE_WITH_IMM, and return as soon
  // as it is *posted*.
  //
  // This is the pipelined primitive, and the contrast with handle_fetch() is
  // the whole point: handle_fetch fences on its own send CQ so that its reply
  // can mean "everything landed", which makes a fetch all-or-nothing. Here
  // each write instead carries `immediate`, raising a receive completion on
  // the client the moment that piece lands. The client learns about layer 0
  // without waiting for layer 31.
  //
  // Nothing is fenced here. There is deliberately no return value saying the
  // write landed, because the client's notification is that signal -- adding
  // a second one would invite code that waits for the wrong thing.
  //
  // Exposed as a per-slot call rather than a command parser so a test can
  // stage delivery and observe a genuinely half-filled buffer. A real server
  // would drive this from a parsed fetch command.
  //
  // Throws std::runtime_error if handle_register has not run, if the digest
  // is unknown, if the length does not match the record, if the write would
  // fall outside the client's registered window, or if ibv_post_send fails.
  void push_slot(const std::string& digest_hex, size_t offset, size_t length,
                 uint32_t immediate);

  // Region id handed out by the most recent successful registration.
  uint64_t region() const { return region_; }

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;

  uint8_t gid_index_;
  size_t source_bytes_;
  size_t source_used_ = 0;
  uint64_t region_ = 0;
  bool connected_ = false;
};

}  // namespace test
}  // namespace lmcache
