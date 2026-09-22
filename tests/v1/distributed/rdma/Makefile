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
DEPTH_BIN := $(BUILD_DIR)/notification_depth_test

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
PIPELINE_SOURCES := $(SRC_DIR)/rdma_pipeline_test.cpp $(COMMON_SOURCES)

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
	$(CONNECTOR_DIR)/kv_sink_client.cpp \
	$(CONNECTOR_DIR)/layer_pipeline.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
ISSUE_SOURCES := \
	$(SRC_DIR)/pipelined_fetch_issue_test.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_issue.cpp \
	$(CONNECTOR_DIR)/pipelined_fetch_session.cpp \
	$(CONNECTOR_DIR)/memory_layout_conversion.cpp \
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
LOGIC_BINS := $(SHARD_BIN) $(PLAN_BIN) $(PLANNER_BIN) $(CODEC_BIN) $(SESSION_BIN) $(ISSUE_BIN) $(DEPTH_BIN)

.PHONY: all test logic logic-test clean
all: $(BIN) $(PIPELINE_BIN) $(LOGIC_BINS)

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

$(DEPTH_BIN): $(DEPTH_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(DEPTH_SOURCES) -o $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# RDMA_DEVICE optionally names the device, e.g. RDMA_DEVICE=rxe0.
# RDMA_GID_INDEX optionally selects the port GID index, and requires
# RDMA_DEVICE to be set too since the two are positional. Soft-RoCE on `lo`
# needs the IPv4-mapped entry, usually index 1:
#   make test RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
test: $(BIN) $(PIPELINE_BIN) $(LOGIC_BINS)
	$(SHARD_BIN)
	$(PLAN_BIN)
	$(PLANNER_BIN)
	$(CODEC_BIN)
	$(SESSION_BIN)
	$(ISSUE_BIN)
	$(DEPTH_BIN)
	$(BIN) $(RDMA_DEVICE) $(RDMA_GID_INDEX)
	$(PIPELINE_BIN) $(RDMA_DEVICE) $(RDMA_GID_INDEX)

clean:
	rm -rf $(BUILD_DIR)
