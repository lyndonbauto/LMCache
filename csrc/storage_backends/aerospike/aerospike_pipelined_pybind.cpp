// SPDX-License-Identifier: Apache-2.0

#ifdef LMCACHE_AEROSPIKE_RDMA

  #include "aerospike_pipelined_pybind.h"

  #include <pybind11/stl.h>

  #include <cstdint>
  #include <map>
  #include <stdexcept>
  #include <string>
  #include <tuple>
  #include <vector>

namespace lmcache {
namespace connector {
namespace aerospike_pipelined_pybind {
namespace {

std::map<uint32_t, rdma::ObjectGroupLayoutInput> parse_object_group_layouts(
    const py::dict& groups) {
  std::map<uint32_t, rdma::ObjectGroupLayoutInput> parsed;
  for (const auto& item : groups) {
    const uint32_t group_id = item.first.cast<uint32_t>();
    const py::dict group = item.second.cast<py::dict>();
    rdma::ObjectGroupLayoutInput layout_input;

    const py::list shapes = group["shapes"].cast<py::list>();
    const py::list dtypes = group["dtypes"].cast<py::list>();
    if (shapes.size() != dtypes.size()) {
      throw std::invalid_argument(
          "object group layout: shapes and dtypes length mismatch");
    }

    py::list layer_indices_list;
    if (group.contains("layer_indices")) {
      layer_indices_list = group["layer_indices"].cast<py::list>();
      if (layer_indices_list.size() != shapes.size()) {
        throw std::invalid_argument(
            "object group layout: layer_indices length must match shapes");
      }
    }

    for (py::ssize_t i = 0; i < shapes.size(); ++i) {
      rdma::KernelGroupLayoutInput kernel_input;
      const py::tuple shape_tuple = shapes[i].cast<py::tuple>();
      for (py::ssize_t dim = 0; dim < shape_tuple.size(); ++dim) {
        kernel_input.shape.push_back(shape_tuple[dim].cast<int64_t>());
      }
      kernel_input.dtype = dtypes[i].cast<std::string>();
      if (!layer_indices_list.empty()) {
        const py::list indices = layer_indices_list[i].cast<py::list>();
        for (py::ssize_t j = 0; j < indices.size(); ++j) {
          kernel_input.layer_indices.push_back(indices[j].cast<uint32_t>());
        }
      }
      layout_input.kernel_groups.push_back(std::move(kernel_input));
    }
    parsed.emplace(group_id, std::move(layout_input));
  }
  return parsed;
}

void set_object_group_layouts(AerospikeNativeConnector& connector,
                              const py::dict& groups) {
  connector.set_object_group_layouts(parse_object_group_layouts(groups));
}

// Tuple shapes mirror lmcache.v1.layerwise.native_fetch.PipelinedFetchArguments
// so Python needs none of the RDMA-only classes bound below.
uint16_t issue_pipelined_fetch_by_keys(
    AerospikeNativeConnector& connector,
    const std::vector<std::tuple<uint32_t, uint32_t, size_t>>& placements,
    const std::vector<std::tuple<uint32_t, std::string>>& chunk_nodes,
    const std::vector<std::tuple<uint32_t, uint32_t, uint32_t, uint32_t,
                                 std::string>>& slot_record_keys) {
  std::vector<rdma::ChunkPlacement> native_placements;
  native_placements.reserve(placements.size());
  for (const auto& [chunk_id, object_group_id, dest_offset] : placements) {
    native_placements.push_back({chunk_id, object_group_id, dest_offset});
  }
  std::vector<rdma::ChunkNodeBinding> native_chunk_nodes;
  native_chunk_nodes.reserve(chunk_nodes.size());
  for (const auto& [chunk_id, node_name] : chunk_nodes) {
    native_chunk_nodes.push_back({chunk_id, node_name});
  }
  std::vector<SlotRecordKey> native_slots;
  native_slots.reserve(slot_record_keys.size());
  for (const auto& [chunk_id, layer_id, plane, piece, record_key] :
       slot_record_keys) {
    native_slots.push_back({chunk_id, layer_id, plane, piece, record_key});
  }
  py::gil_scoped_release release;
  return connector.issue_pipelined_fetch_by_keys(
      native_placements, native_chunk_nodes, native_slots);
}

}  // namespace

void bind_pipelined_fetch(py::module& module,
                          py::class_<AerospikeNativeConnector>& connector) {
  py::class_<rdma::ChunkPlacement>(module, "PipelinedChunkPlacement")
      .def(py::init<>())
      .def_readwrite("chunk_id", &rdma::ChunkPlacement::chunk_id)
      .def_readwrite("object_group_id", &rdma::ChunkPlacement::object_group_id)
      .def_readwrite("dest_offset", &rdma::ChunkPlacement::dest_offset);

  py::class_<rdma::ChunkNodeBinding>(module, "PipelinedChunkNodeBinding")
      .def(py::init<>())
      .def_readwrite("chunk_id", &rdma::ChunkNodeBinding::chunk_id)
      .def_readwrite("node_name", &rdma::ChunkNodeBinding::node_name);

  py::class_<rdma::SlotDigest>(module, "PipelinedSlotDigest")
      .def(py::init<>())
      .def_readwrite("chunk_id", &rdma::SlotDigest::chunk_id)
      .def_readwrite("layer_id", &rdma::SlotDigest::layer_id)
      .def_readwrite("plane", &rdma::SlotDigest::plane)
      .def_readwrite("piece", &rdma::SlotDigest::piece)
      .def_readwrite("digest_hex", &rdma::SlotDigest::digest_hex);

  connector
      .def("pipelined_fetch_init_error",
           &AerospikeNativeConnector::pipelined_fetch_init_error)
      .def("set_object_group_layouts", &set_object_group_layouts,
           py::arg("group_layouts"))
      .def("issue_pipelined_fetch",
           &AerospikeNativeConnector::issue_pipelined_fetch,
           py::arg("placements"), py::arg("chunk_nodes"),
           py::arg("slot_digests"))
      .def("issue_pipelined_fetch_by_keys", &issue_pipelined_fetch_by_keys,
           py::arg("placements"), py::arg("chunk_nodes"),
           py::arg("slot_record_keys"))
      .def("finish_pipelined_fetch",
           &AerospikeNativeConnector::finish_pipelined_fetch)
      .def("abandon_pipelined_fetch",
           &AerospikeNativeConnector::abandon_pipelined_fetch);
}

}  // namespace aerospike_pipelined_pybind
}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_AEROSPIKE_RDMA
