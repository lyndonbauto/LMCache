# SPDX-License-Identifier: Apache-2.0
#
# Builds the RDMA byte-equivalence harness for the Aerospike reception path.
#
#   make            # build
#   make test       # build and run (exits 77 / "skip" with no RDMA device)
#
# Needs libibverbs development headers (package rdma-core / libibverbs-dev).
# If they live outside the default prefix, point RDMA_CORE_INCLUDE_DIR and
# RDMA_CORE_LIBRARY_DIR at them.

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -g -Wall -Wextra

CONNECTOR_DIR := ../../../../csrc/storage_backends/aerospike
SRC_DIR := csrc
BUILD_DIR := build
BIN := $(BUILD_DIR)/rdma_equivalence_test
PIPELINE_BIN := $(BUILD_DIR)/rdma_pipeline_test
SHARD_BIN := $(BUILD_DIR)/shard_plan_test
PLAN_BIN := $(BUILD_DIR)/request_plan_test
PLANNER_BIN := $(BUILD_DIR)/slot_planner_test
CODEC_BIN := $(BUILD_DIR)/pipelined_fetch_test
SESSION_BIN := $(BUILD_DIR)/pipelined_fetch_session_test
ISSUE_BIN := $(BUILD_DIR)/pipelined_fetch_issue_test
POOL_BIN := $(BUILD_DIR)/pipelined_fetch_pool_test
DEPTH_BIN := $(BUILD_DIR)/notification_depth_test
DUMP_BIN := $(BUILD_DIR)/slot_plan_dump

INCLUDES := -I$(SRC_DIR) -I$(CONNECTOR_DIR)
ifneq ($(RDMA_CORE_INCLUDE_DIR),)
INCLUDES += -isystem $(RDMA_CORE_INCLUDE_DIR)
endif

LDFLAGS :=
ifneq ($(RDMA_CORE_LIBRARY_DIR),)
LDFLAGS += -L$(RDMA_CORE_LIBRARY_DIR)
endif
LDLIBS := -libverbs

# The harness deliberately reuses the production verbs context and codec
# rather than reimplementing them, so a regression there fails this test.
COMMON_SOURCES := \
	$(SRC_DIR)/kv_sink_mock_writer.cpp \
	$(CONNECTOR_DIR)/rdma_context.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp

SOURCES := $(SRC_DIR)/rdma_equivalence_test.cpp $(COMMON_SOURCES)
PIPELINE_SOURCES := $(SRC_DIR)/rdma_pipeline_test.cpp $(COMMON_SOURCES) \
	$(CONNECTOR_DIR)/pipelined_fetch_pool.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_issue.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_session.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp

# These cover arithmetic, bookkeeping and string codecs rather than the data
# path, so they link neither libibverbs nor the Aerospike client and run
# anywhere.
# Grouped as `logic` so they stay reachable on a box with no RDMA toolchain.
SHARD_SOURCES := \
	$(SRC_DIR)/shard_plan_test.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
PLAN_SOURCES := \
	$(SRC_DIR)/request_plan_test.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp
PLANNER_SOURCES := \
	$(SRC_DIR)/slot_planner_test.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
# kv_sink_client.cpp includes rdma_context.h for its endpoint types but calls
# no verbs function, so the codec links here without libibverbs.
CODEC_SOURCES := \
	$(SRC_DIR)/pipelined_fetch_test.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp
SESSION_SOURCES := \
	$(SRC_DIR)/pipelined_fetch_session_test.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_session.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_issue.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_pool.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
ISSUE_SOURCES := \
	$(SRC_DIR)/pipelined_fetch_issue_test.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_issue.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_pool.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_session.cpp \
	$(CONNECTOR_DIR)/memory_layout_conversion.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
POOL_SOURCES := \
	$(SRC_DIR)/pipelined_fetch_pool_test.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_pool.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_issue.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_session.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
DEPTH_SOURCES := \
	$(SRC_DIR)/notification_depth_test.cpp \
	$(CONNECTOR_DIR)/notification_depth.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_session.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
# Takes a fixture path and prints plans instead of asserting, so it is built
# with the logic harnesses but driven by test_slot_plan_parity.py rather than
# run bare by `logic-test`.
DUMP_SOURCES := \
	$(SRC_DIR)/slot_plan_dump.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
LOGIC_BINS := $(SHARD_BIN) $(PLAN_BIN) $(PLANNER_BIN) $(CODEC_BIN) $(SESSION_BIN) $(ISSUE_BIN) $(POOL_BIN) $(DEPTH_BIN) $(DUMP_BIN)

# Acceptance A7: does a write-with-immediate consume a posted receive?
# Standalone, libibverbs only. EFA=1 adds the SRD path (needs efadv.h and
# libefa) and builds a separately named binary. The unsolicited write-receive
# mode is compiled only when efadv.h declares it (newer rdma-core).
PROBE_SOURCES := $(SRC_DIR)/efa_imm_probe.cpp
PROBE_FLAGS :=
PROBE_LIBS := -libverbs
ifeq ($(EFA),1)
PROBE_BIN := $(BUILD_DIR)/efa_imm_probe_efa
PROBE_FLAGS += -DLMCACHE_AEROSPIKE_EFA
PROBE_LIBS += -lefa
HASH := \#
EFA_UNSOLICITED := $(shell printf '$(HASH)include <infiniband/efadv.h>\nint f(efadv_cq* c) { return EFADV_QP_FLAGS_UNSOLICITED_WRITE_RECV + EFADV_WC_EX_WITH_IS_UNSOLICITED + efadv_wc_is_unsolicited(c); }\n' | $(CXX) $(INCLUDES) -fsyntax-only -x c++ - 2>/dev/null && echo 1)
ifeq ($(EFA_UNSOLICITED),1)
PROBE_FLAGS += -DLMCACHE_EFA_UNSOLICITED
endif
else
PROBE_BIN := $(BUILD_DIR)/efa_imm_probe
endif

# Test-only Python module over the real session, driven by the layerwise
# conformance suite (tests/v1/layerwise/aerospike_harness.py). Needs pybind11
# in the interpreter named by PYTHON; built only by `make pyharness`.
PYTHON ?= python3
PYHARNESS_SOURCES := \
	$(SRC_DIR)/fabric_free_session_pybind.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_session.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_issue.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_pool.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp

.PHONY: all test logic logic-test pyharness efa-probe clean

pyharness: $(PYHARNESS_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -shared -fPIC $(INCLUDES) \
		$$($(PYTHON) -m pybind11 --includes) $(PYHARNESS_SOURCES) \
		-o $(BUILD_DIR)/fabric_free_session$$($(PYTHON) -c \
		"import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")
all: $(BIN) $(PIPELINE_BIN) $(PROBE_BIN) $(LOGIC_BINS)

# Build the A7 probe. On EFA: make efa-probe EFA=1, then
#   build/efa_imm_probe_efa <device> 0 --transport srd
efa-probe: $(PROBE_BIN)

$(PROBE_BIN): $(PROBE_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(PROBE_FLAGS) $(INCLUDES) $(PROBE_SOURCES) $(LDFLAGS) $(PROBE_LIBS) -o $@

# Build the device-independent harnesses without running them.
logic: $(LOGIC_BINS)

# Build and run only the device-independent checks.
logic-test: $(LOGIC_BINS)
	$(SHARD_BIN)
	$(PLAN_BIN)
	$(PLANNER_BIN)
	$(CODEC_BIN)
	$(SESSION_BIN)
	$(ISSUE_BIN)
	$(POOL_BIN)
	$(DEPTH_BIN)

$(BIN): $(SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(SOURCES) $(LDFLAGS) $(LDLIBS) -o $@

$(PIPELINE_BIN): $(PIPELINE_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(PIPELINE_SOURCES) $(LDFLAGS) $(LDLIBS) -o $@

$(SHARD_BIN): $(SHARD_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(SHARD_SOURCES) -o $@

$(PLAN_BIN): $(PLAN_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(PLAN_SOURCES) -o $@

$(PLANNER_BIN): $(PLANNER_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(PLANNER_SOURCES) -o $@

$(CODEC_BIN): $(CODEC_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(CODEC_SOURCES) -o $@

$(SESSION_BIN): $(SESSION_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(SESSION_SOURCES) -o $@

$(ISSUE_BIN): $(ISSUE_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(ISSUE_SOURCES) -o $@

$(POOL_BIN): $(POOL_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(POOL_SOURCES) -o $@

$(DEPTH_BIN): $(DEPTH_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(DEPTH_SOURCES) -o $@

$(DUMP_BIN): $(DUMP_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(DUMP_SOURCES) -o $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# RDMA_DEVICE optionally names the device, e.g. RDMA_DEVICE=rxe0.
# RDMA_GID_INDEX optionally selects the port GID index, and requires
# RDMA_DEVICE to be set too since the two are positional. Soft-RoCE on `lo`
# needs the IPv4-mapped entry, usually index 1:
#   make test RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
test: $(BIN) $(PIPELINE_BIN) $(PROBE_BIN) $(LOGIC_BINS)
	$(SHARD_BIN)
	$(PLAN_BIN)
	$(PLANNER_BIN)
	$(CODEC_BIN)
	$(SESSION_BIN)
	$(ISSUE_BIN)
	$(POOL_BIN)
	$(DEPTH_BIN)
	$(BIN) $(RDMA_DEVICE) $(RDMA_GID_INDEX)
	$(PIPELINE_BIN) $(RDMA_DEVICE) $(RDMA_GID_INDEX)
	$(PROBE_BIN) $(RDMA_DEVICE) $(RDMA_GID_INDEX) --expect-consumes

clean:
	rm -rf $(BUILD_DIR)
