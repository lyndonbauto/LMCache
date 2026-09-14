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
SOURCES := \
	$(SRC_DIR)/rdma_equivalence_test.cpp \
	$(SRC_DIR)/kv_sink_mock_writer.cpp \
	$(CONNECTOR_DIR)/rdma_context.cpp \
	$(CONNECTOR_DIR)/kv_sink_client.cpp

.PHONY: all test clean
all: $(BIN)

$(BIN): $(SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(SOURCES) $(LDFLAGS) $(LDLIBS) -o $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# RDMA_DEVICE optionally names the device, e.g. RDMA_DEVICE=rxe0.
test: $(BIN)
	$(BIN) $(RDMA_DEVICE)

clean:
	rm -rf $(BUILD_DIR)
