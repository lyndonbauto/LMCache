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

# The shard plan is pure arithmetic, so this one links neither libibverbs nor
# the Aerospike client and runs anywhere. Kept as its own target so it is
# still reachable on a box with no RDMA toolchain at all.
SHARD_SOURCES := \
	$(SRC_DIR)/shard_plan_test.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp

.PHONY: all test shard clean
all: $(BIN) $(PIPELINE_BIN) $(SHARD_BIN)

# Build and run only the device-independent checks.
shard: $(SHARD_BIN)
	$(SHARD_BIN)

$(BIN): $(SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(SOURCES) $(LDFLAGS) $(LDLIBS) -o $@

$(PIPELINE_BIN): $(PIPELINE_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(PIPELINE_SOURCES) $(LDFLAGS) $(LDLIBS) -o $@

$(SHARD_BIN): $(SHARD_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(SHARD_SOURCES) -o $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# RDMA_DEVICE optionally names the device, e.g. RDMA_DEVICE=rxe0.
# RDMA_GID_INDEX optionally selects the port GID index, and requires
# RDMA_DEVICE to be set too since the two are positional. Soft-RoCE on `lo`
# needs the IPv4-mapped entry, usually index 1:
#   make test RDMA_DEVICE=rxe0 RDMA_GID_INDEX=1
test: $(BIN) $(PIPELINE_BIN) $(SHARD_BIN)
	$(SHARD_BIN)
	$(BIN) $(RDMA_DEVICE) $(RDMA_GID_INDEX)
	$(PIPELINE_BIN) $(RDMA_DEVICE) $(RDMA_GID_INDEX)

clean:
	rm -rf $(BUILD_DIR)
