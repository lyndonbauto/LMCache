# SPDX-License-Identifier: Apache-2.0
#
# Builds the device-independent harnesses for the Aerospike pipelined fetch.
#
#   make              # build the logic harnesses
#   make logic-test   # build and run them
#   make pyharness    # build the fabric-free Python module
#
# None of these link libibverbs or the Aerospike client: they cover
# arithmetic and bookkeeping rather than the data path, so they run anywhere.
# The data path is covered end to end against a patched server by
# tests/v1/distributed/test_aerospike_pipelined_rdma_integration.py.

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -g -Wall -Wextra

CONNECTOR_DIR := ../../../../csrc/storage_backends/aerospike
SRC_DIR := csrc
BUILD_DIR := build
SHARD_BIN := $(BUILD_DIR)/shard_plan_test
PLANNER_BIN := $(BUILD_DIR)/slot_planner_test
TABLE_BIN := $(BUILD_DIR)/sink_fetch_table_test
LAYOUT_BIN := $(BUILD_DIR)/memory_layout_conversion_test
DUMP_BIN := $(BUILD_DIR)/slot_plan_dump

INCLUDES := -I$(SRC_DIR) -I$(CONNECTOR_DIR)

SHARD_SOURCES := \
	$(SRC_DIR)/shard_plan_test.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
PLANNER_SOURCES := \
	$(SRC_DIR)/slot_planner_test.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp
TABLE_SOURCES := \
	$(SRC_DIR)/sink_fetch_table_test.cpp \
	$(CONNECTOR_DIR)/sink_fetch_table.cpp
LAYOUT_SOURCES := \
	$(SRC_DIR)/memory_layout_conversion_test.cpp \
	$(CONNECTOR_DIR)/memory_layout_conversion.cpp
# Takes a fixture path and prints plans instead of asserting, so it is built
# with the logic harnesses but driven by test_slot_plan_parity.py rather than
# run bare by `logic-test`.
DUMP_SOURCES := \
	$(SRC_DIR)/slot_plan_dump.cpp \
	$(CONNECTOR_DIR)/slot_planner.cpp \
	$(CONNECTOR_DIR)/shard_plan.cpp
LOGIC_BINS := $(SHARD_BIN) $(PLANNER_BIN) $(TABLE_BIN) $(LAYOUT_BIN) $(DUMP_BIN)

# Test-only Python module over the real SinkFetchTable, driven by the
# layerwise conformance suite (tests/v1/layerwise/aerospike_harness.py).
# Needs pybind11 in the interpreter named by PYTHON; built only by
# `make pyharness`.
PYTHON ?= python3
PYHARNESS_SOURCES := \
	$(SRC_DIR)/fabric_free_session_pybind.cpp \
	$(CONNECTOR_DIR)/sink_fetch_table.cpp

.PHONY: all logic logic-test test pyharness clean

all: logic

logic: $(LOGIC_BINS)

logic-test: $(LOGIC_BINS)
	$(SHARD_BIN)
	$(PLANNER_BIN)
	$(TABLE_BIN)
	$(LAYOUT_BIN)

test: logic-test

pyharness: $(PYHARNESS_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -shared -fPIC $(INCLUDES) \
		$$($(PYTHON) -m pybind11 --includes) $(PYHARNESS_SOURCES) \
		-o $(BUILD_DIR)/fabric_free_session$$($(PYTHON) -c \
		"import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")

$(SHARD_BIN): $(SHARD_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(SHARD_SOURCES) -o $@

$(PLANNER_BIN): $(PLANNER_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(PLANNER_SOURCES) -o $@

$(TABLE_BIN): $(TABLE_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(TABLE_SOURCES) -o $@

$(LAYOUT_BIN): $(LAYOUT_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(LAYOUT_SOURCES) -o $@

$(DUMP_BIN): $(DUMP_SOURCES) | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(DUMP_SOURCES) -o $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

clean:
	rm -rf $(BUILD_DIR)
