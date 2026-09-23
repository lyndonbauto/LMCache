// SPDX-License-Identifier: Apache-2.0
//
// Prints the production SlotPlanner's output for every case in a shared
// fixture, in a canonical form the Python planner can be compared against.
//
// This asserts nothing on its own. slot_planner_test.cpp already checks that
// the C++ planner is correct, and tests/v1/layerwise/ checks the Python one.
// What neither can see is the two drifting apart, and a drift is not a
// crash: the plan would still tile the payload, but a slot would name a
// record the other side never produced. So this reduces a plan to text and
// lets test_slot_plan_parity.py diff it.
//
// Usage: slot_plan_dump <fixture-path>
//
// Output, one case at a time:
//
//   case <name>
//   slot <index> <layer_id> <chunk_id> <offset> <length>
//   ...
//
// Digests and nodes are deliberately absent. They are joined onto the plan
// later, by pipelined_fetch_session on this side, so they are not something
// the two planners could disagree about.

#include "slot_planner.h"

#include <cstdint>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using lmcache::connector::rdma::ChunkPlacement;
using lmcache::connector::rdma::KernelGroupLayout;
using lmcache::connector::rdma::ObjectGroupLayout;
using lmcache::connector::rdma::RequestPlan;
using lmcache::connector::rdma::SlotPlanner;

// Generation is not part of the slot layout, so any non-zero value does.
constexpr uint16_t kGeneration = 1;

// One fixture case, accumulated directive by directive.
struct Case {
  std::string name;
  // Ordered by object group id so both sides build the planner the same way.
  // Group order cannot move an offset -- each group's payload is based at
  // zero -- but keeping it fixed means a future change that does would show
  // up here rather than hiding.
  std::map<uint32_t, ObjectGroupLayout> groups;
  std::vector<ChunkPlacement> placements;
  size_t max_record_bytes = 0;
  size_t max_write_bytes = 0;
};

// Emit one case's plan, or fail loudly: a case the planner rejects is a
// fixture bug, and silently skipping it would quietly stop testing it.
void dump_case(const Case& current) {
  std::vector<ObjectGroupLayout> layouts;
  layouts.reserve(current.groups.size());
  for (const auto& entry : current.groups) {
    layouts.push_back(entry.second);
  }

  const SlotPlanner planner(layouts);
  const RequestPlan plan =
      planner.plan_request(current.placements, current.max_record_bytes,
                           current.max_write_bytes, kGeneration);

  std::cout << "case " << current.name << "\n";
  for (size_t index = 0; index < plan.slot_count(); ++index) {
    const auto& slot = plan.slot(static_cast<uint16_t>(index));
    std::cout << "slot " << index << ' ' << slot.layer_id << ' '
              << slot.chunk_id << ' ' << slot.offset << ' ' << slot.length
              << "\n";
  }
}

void run(std::istream& fixture) {
  Case current;
  bool in_case = false;
  std::string line;
  size_t line_number = 0;

  while (std::getline(fixture, line)) {
    ++line_number;
    const size_t comment = line.find('#');
    if (comment != std::string::npos) {
      line.erase(comment);
    }
    std::istringstream fields(line);
    std::string directive;
    if (!(fields >> directive)) {
      continue;
    }

    if (directive == "case") {
      current = Case{};
      fields >> current.name;
      in_case = true;
    } else if (!in_case) {
      throw std::runtime_error("directive '" + directive +
                               "' outside a case at line " +
                               std::to_string(line_number));
    } else if (directive == "kernel") {
      uint32_t group_id = 0;
      KernelGroupLayout kernel;
      fields >> group_id >> kernel.kv_size >> kernel.num_slots >>
          kernel.hidden_dim >> kernel.element_size;
      uint32_t layer_id = 0;
      while (fields >> layer_id) {
        kernel.layer_indices.push_back(layer_id);
      }
      ObjectGroupLayout& group = current.groups[group_id];
      group.object_group_id = group_id;
      group.kernel_groups.push_back(kernel);
    } else if (directive == "place") {
      ChunkPlacement placement;
      fields >> placement.chunk_id >> placement.object_group_id >>
          placement.dest_offset;
      current.placements.push_back(placement);
    } else if (directive == "caps") {
      fields >> current.max_record_bytes >> current.max_write_bytes;
    } else if (directive == "end") {
      dump_case(current);
      in_case = false;
    } else {
      throw std::runtime_error("unknown directive '" + directive +
                               "' at line " + std::to_string(line_number));
    }
  }

  if (in_case) {
    throw std::runtime_error("fixture ended inside case '" + current.name +
                             "'");
  }
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: " << argv[0] << " <fixture-path>\n";
    return 2;
  }
  std::ifstream fixture(argv[1]);
  if (!fixture) {
    std::cerr << "cannot open fixture: " << argv[1] << "\n";
    return 2;
  }
  try {
    run(fixture);
  } catch (const std::exception& error) {
    std::cerr << "slot_plan_dump: " << error.what() << "\n";
    return 1;
  }
  return 0;
}
